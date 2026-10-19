"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import admin, schemas, v1
from app.clock import Clock
from app.config import Settings, get_settings
from app.db.session import create_all
from app.deps import Container, build_container
from app.errors import AppError
from app.ingest.sync import HttpFactory
from app.llm.client import LLMClient
from app.logging import configure_logging
from app.security.ssrf import Resolver, default_resolver

logger = logging.getLogger("intelligence")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def run_migrations(url: str) -> None:
    from pathlib import Path

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def _err(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def create_app(
    settings: Settings | None = None,
    *,
    http_factory: HttpFactory | None = None,
    llm_client: LLMClient | None = None,
    clock: Clock | None = None,
    resolver: Resolver = default_resolver,
    use_env_llm: bool = True,
    container: Container | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    c = container or build_container(
        settings, http_factory=http_factory, llm_client=llm_client, clock=clock, resolver=resolver, use_env_llm=use_env_llm
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.auto_migrate:
            if settings.env == "test":
                create_all(c.engine)
            else:
                await asyncio.to_thread(run_migrations, settings.database_url)
        if settings.env == "dev" and not settings.admin_api_key:
            logger.warning("ADMIN_API_KEY is not set: /admin is disabled")
        worker: asyncio.Task[None] | None = None
        if settings.worker_enabled:
            worker = asyncio.create_task(c.runner.run_forever(settings.worker_poll_s, tick=lambda: _schedule(c)))
        yield
        c.runner.stop()
        if worker:
            await asyncio.wait_for(worker, timeout=30)
        c.engine.dispose()

    app = FastAPI(
        title="Intelligence",
        version=__version__,
        lifespan=lifespan,
        description="ML + LLM ranking and recommendations over the Funnels and Conversations analytics APIs, per customer account.",
        openapi_tags=[
            {"name": n}
            for n in (
                "auth",
                "account",
                "sources",
                "sync",
                "rankings",
                "leads",
                "segments",
                "anomalies",
                "insights",
                "models",
                "diagnostics",
                "system",
                "admin",
            )
        ],
    )
    app.state.container = c

    @app.middleware("http")
    async def base_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        rid = request.headers.get("x-request-id", "")
        rid = rid if _ID_RE.match(rid) else uuid.uuid4().hex
        request.state.request_id = rid
        ok, wait = c.ip_limiter.check("ip:" + (request.client.host if request.client else "?"))
        if not ok:
            resp: Response = _err(429, "RATE_LIMITED", "too many requests", {"Retry-After": str(int(wait) + 1)})
        elif int(request.headers.get("content-length") or 0) > settings.max_body_bytes:
            resp = _err(413, "PAYLOAD_TOO_LARGE", "request body too large")
        else:
            try:
                resp = await call_next(request)
            except Exception:
                logger.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
                resp = _err(500, "INTERNAL_ERROR", "internal error")
        resp.headers["X-Request-ID"] = rid
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'"
            if not request.url.path.startswith(("/docs", "/redoc"))
            else "default-src 'self' cdn.jsdelivr.net 'unsafe-inline' data:",
        )
        route = request.scope.get("route")
        label = getattr(route, "path", "unmatched")
        c.metrics.requests.labels(request.method, label, str(resp.status_code)).inc()
        return resp

    if settings.cors_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_list,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-None-Match"],
        )

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"Retry-After": "1"} if exc.status_code == 429 else None
        return _err(exc.status_code, exc.code, exc.message, headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _err(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return _err(422, "VALIDATION_ERROR", f"{loc}: {first.get('msg', 'invalid request')}")

    app.include_router(v1.router)
    app.include_router(admin.router)

    @app.get("/v1/health", response_model=schemas.Health, tags=["system"])
    def health() -> schemas.Health:
        return schemas.Health(status="ok", version=__version__)

    @app.get("/v1/ready", response_model=schemas.Ready, tags=["system"], responses={503: {"model": schemas.ErrorResponse}})
    def ready() -> Response:
        checks: dict[str, str] = {}
        try:
            with c.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        checks["llm"] = "configured" if c.gateway.enabled else "template_fallback"
        ok = checks["database"] == "ok"
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=True, tags=["system"], response_class=PlainTextResponse)
    def metrics(request: Request) -> Response:
        token = settings.metrics_token
        if token and request.headers.get("authorization") != f"Bearer {token}":
            return _err(401, "UNAUTHORIZED", "metrics token required")
        return PlainTextResponse(c.metrics.render(), media_type="text/plain; version=0.0.4")

    return app


def _schedule(c: Container) -> None:
    """Enqueue a sync per account whose sources have not synced within SYNC_INTERVAL_S (0 disables)."""
    interval = c.settings.sync_interval_s
    if interval <= 0:
        return
    from sqlalchemy import select

    from app.db import models as m
    from app.db.session import session_scope

    now = int(c.clock())
    with session_scope(c.sessions) as s:
        busy = {
            j.account_id
            for j in s.execute(select(m.Job).where(m.Job.type == "sync", m.Job.status.in_(("queued", "running")))).scalars()
        }
        for acct in s.execute(select(m.Account).where(m.Account.disabled.is_(False))).scalars():
            if acct.id in busy:
                continue
            last = s.execute(
                select(m.SyncState.last_run_at)
                .where(m.SyncState.account_id == acct.id)
                .order_by(m.SyncState.last_run_at.desc())
                .limit(1)
            ).scalar()
            has_sources = s.execute(
                select(m.Source.id).where(m.Source.account_id == acct.id, m.Source.status == "active").limit(1)
            ).first()
            if has_sources and (last is None or now - last >= interval):
                c.queue.enqueue(acct.id, "sync", {"source_id": None, "full": False})


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
