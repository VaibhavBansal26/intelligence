"""Composition root: builds every collaborator once and exposes them through FastAPI dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import Engine

from app.clock import Clock, system_clock
from app.config import Settings
from app.connectors.client import ConnectorError
from app.db import models as m
from app.db.session import SessionFactory, make_engine, make_session_factory, session_scope
from app.ingest.jobs import DbJobQueue, JobRunner
from app.ingest.sync import HttpFactory, IngestService
from app.llm.ask import AskService
from app.llm.client import AnthropicClient, LLMClient
from app.llm.gateway import LLMGateway
from app.llm.narrator import LLMNarrator
from app.metrics import Metrics
from app.models.registry import ModelRegistry
from app.security.crypto import SecretBox
from app.security.limits import RateLimiter
from app.security.ssrf import Resolver, default_resolver
from app.services.accounts import AccountService
from app.services.analytics import AnalyticsService


def default_http_factory(settings: Settings) -> HttpFactory:
    def make(_source: m.Source) -> httpx.AsyncClient:
        return httpx.AsyncClient(follow_redirects=False, timeout=settings.connector_timeout_s)

    return make


@dataclass
class Container:
    settings: Settings
    engine: Engine
    sessions: SessionFactory
    secrets: SecretBox
    clock: Clock
    accounts: AccountService
    ingest: IngestService
    queue: DbJobQueue
    runner: JobRunner
    registry: ModelRegistry
    analytics: AnalyticsService
    gateway: LLMGateway
    narrator: LLMNarrator
    ask: AskService
    key_limiter: RateLimiter
    ip_limiter: RateLimiter
    metrics: Metrics


def build_container(
    settings: Settings,
    *,
    http_factory: HttpFactory | None = None,
    llm_client: LLMClient | None = None,
    clock: Clock | None = None,
    resolver: Resolver = default_resolver,
    sleep: Callable[[float], Any] | None = None,
    use_env_llm: bool = True,
) -> Container:
    clock = clock or system_clock
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    secrets = SecretBox(settings.kek_list)
    registry = ModelRegistry(sessions, settings.model_dir, clock, settings.model_cache_size)
    ingest_kwargs: dict[str, Any] = {}
    if sleep is not None:
        ingest_kwargs["sleep"] = sleep
    ingest = IngestService(sessions, settings, secrets, http_factory or default_http_factory(settings), clock, **ingest_kwargs)
    queue = DbJobQueue(sessions, clock, settings.account_max_concurrent_jobs)
    analytics = AnalyticsService(sessions, settings, registry, secrets, clock)
    if llm_client is None and use_env_llm and settings.anthropic_api_key:
        llm_client = AnthropicClient(settings.anthropic_api_key, settings.anthropic_model)
    gateway = LLMGateway(llm_client, settings, sessions, clock)
    metrics = Metrics()

    async def sync_handler(job: m.Job) -> dict[str, Any]:
        payload = job.payload or {}
        with session_scope(sessions) as s:
            from sqlalchemy import select

            q = select(m.Source.id).where(m.Source.account_id == job.account_id, m.Source.status == "active")
            if payload.get("source_id"):
                q = q.where(m.Source.id == payload["source_id"])
            ids = list(s.execute(q).scalars())
        results = [(await ingest.sync_source(job.account_id, sid, full=bool(payload.get("full")))).to_dict() for sid in ids]
        for r in results:
            for res in r["resources"]:
                metrics.sync_rows.labels(res["resource"]).inc(res["rows"])
        errors = [res["error"] for r in results for res in r["resources"] if res["error"]]
        total = sum(len(r["resources"]) for r in results)
        if ids and total and len(errors) == total:
            raise ConnectorError("; ".join(errors[:3]))
        return {"sources": results, "errors": errors}

    async def train_handler(job: m.Job) -> dict[str, Any]:
        types = (job.payload or {}).get("model_types")
        return {"models": await asyncio.to_thread(analytics.train, job.account_id, types)}

    async def profiles_handler(job: m.Job) -> dict[str, Any]:
        from app.columnar.profiles import refresh_profiles
        from app.columnar.store import ParquetEventStore

        if not settings.columnar_dir:
            raise ValueError("COLUMNAR_DIR is not configured")
        store = ParquetEventStore(settings.columnar_dir, settings.columnar_buckets)
        subset = (job.payload or {}).get("buckets")
        st = await asyncio.to_thread(refresh_profiles, store, job.account_id, subset)
        return {
            "buckets_refreshed": st.buckets_refreshed,
            "buckets_skipped": st.buckets_skipped,
            "parts_read": st.parts_read,
            "rows_read": st.rows_read,
        }

    runner = JobRunner(queue, {"sync": sync_handler, "train": train_handler, "profiles": profiles_handler})
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        secrets=secrets,
        clock=clock,
        accounts=AccountService(sessions, settings, secrets, clock, resolver),
        ingest=ingest,
        queue=queue,
        runner=runner,
        registry=registry,
        analytics=analytics,
        gateway=gateway,
        narrator=LLMNarrator(gateway),
        ask=AskService(gateway, settings.llm_max_tool_steps),
        key_limiter=RateLimiter(settings.rate_limit_per_min, settings.rate_limit_burst, clock),
        ip_limiter=RateLimiter(settings.rate_limit_per_min * 2, settings.rate_limit_burst * 2, clock),
        metrics=metrics,
    )


def get_container(request: Request) -> Container:
    c: Container = request.app.state.container
    return c
