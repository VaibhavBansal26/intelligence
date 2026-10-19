"""Customer-facing /v1 API. Every route declares the permission it needs; data is scoped by the caller's account."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api import schemas as s
from app.api.auth import authenticate, etag_or_304, idempotent, require
from app.db import models as m
from app.db.session import session_scope
from app.deps import Container, get_container
from app.errors import NotFound, Unprocessable
from app.llm.ask import build_tools
from app.llm.rerank import rerank
from app.security import rbac
from app.security.rbac import Principal
from app.services.analytics import MODEL_TYPES
from app.services.insights import insights as build_insights

router = APIRouter(prefix="/v1")
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": s.ErrorResponse},
    403: {"model": s.ErrorResponse},
    404: {"model": s.ErrorResponse},
    422: {"model": s.ErrorResponse},
    429: {"model": s.ErrorResponse},
}


def _source_out(r: m.Source) -> s.SourceOut:
    return s.SourceOut(
        id=r.id,
        kind=r.kind,
        name=r.name,
        base_url=r.base_url,
        status=r.status,
        pepper_fingerprint=r.pepper_fp,
        last_schema_version=r.last_schema_version,
        created_at=r.created_at,
    )


def _key_out(r: m.ApiKey) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "prefix": r.prefix,
        "role": r.role,
        "scopes": r.scopes or [],
        "expires_at": r.expires_at,
        "revoked_at": r.revoked_at,
        "last_used_at": r.last_used_at,
        "created_at": r.created_at,
    }


def _model_out(r: m.ModelVersion) -> s.ModelOut:
    return s.ModelOut(
        id=r.id,
        model_type=r.model_type,
        version=r.version,
        status=r.status,
        method=r.method,
        metrics=r.metrics,
        card=r.card,
        trained_rows=r.trained_rows,
        seed=r.seed,
        created_at=r.created_at,
        promoted_at=r.promoted_at,
    )


def _job_out(j: m.Job) -> s.JobOut:
    return s.JobOut(
        id=j.id,
        type=j.type,
        status=j.status,
        attempts=j.attempts,
        created_at=j.created_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        error=j.error,
        result=j.result,
    )


def _cached(request: Request, c: Container, p: Principal, build: Any, salt: str = "") -> Response:
    tag, not_modified = etag_or_304(request, c, p.account_id, salt)
    if not_modified is not None:
        return not_modified
    return JSONResponse(content=_jsonable(build()), headers={"ETag": tag, "Cache-Control": "private, must-revalidate"})


def _jsonable(obj: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(obj)


# --- auth / account -----------------------------------------------------------------------------------
@router.post(
    "/auth/token", response_model=s.TokenOut, tags=["auth"], responses=ERRORS, summary="Exchange an API key for a short-lived JWT"
)
def token(request: Request, p: Principal = Depends(authenticate), c: Container = Depends(get_container)) -> s.TokenOut:
    if p.via != "api_key":
        raise Unprocessable("exchange an API key, not a token", code="KEY_REQUIRED")
    tok, ttl = c.accounts.issue_jwt(p)
    return s.TokenOut(access_token=tok, expires_in=ttl)


@router.get("/account", response_model=s.AccountOut, tags=["account"], responses=ERRORS)
def get_account(p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)) -> s.AccountOut:
    a = c.accounts.get_account(p.account_id)
    return s.AccountOut(id=a.id, name=a.name, created_at=a.created_at, settings=a.settings or {}, disabled=a.disabled)


@router.get("/accounts/{account_id}", response_model=s.AccountOut, tags=["account"], responses=ERRORS)
def get_account_by_id(
    account_id: str, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)
) -> s.AccountOut:
    if account_id != p.account_id:
        raise NotFound("account not found")
    return get_account(p, c)


@router.patch("/account", response_model=s.AccountOut, tags=["account"], responses=ERRORS)
def patch_account(
    body: s.AccountPatch, p: Principal = Depends(require(rbac.P_ACCOUNT)), c: Container = Depends(get_container)
) -> s.AccountOut:
    a = c.accounts.update_account(p.account_id, name=body.name, settings=body.settings)
    return s.AccountOut(id=a.id, name=a.name, created_at=a.created_at, settings=a.settings or {}, disabled=a.disabled)


@router.get("/api-keys", response_model=list[s.KeyOut], tags=["account"], responses=ERRORS)
def list_keys(p: Principal = Depends(require(rbac.P_KEYS)), c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    return [_key_out(k) for k in c.accounts.list_keys(p.account_id)]


@router.post("/api-keys", response_model=s.KeyCreated, status_code=201, tags=["account"], responses=ERRORS)
def create_key(
    body: s.KeyCreate, p: Principal = Depends(require(rbac.P_KEYS)), c: Container = Depends(get_container)
) -> dict[str, Any]:
    row, plaintext = c.accounts.create_key(
        p.account_id, name=body.name, role=body.role, scopes=body.scopes, expires_at=body.expires_at
    )
    return {**_key_out(row), "api_key": plaintext}


@router.delete("/api-keys/{key_id}", status_code=204, tags=["account"], responses=ERRORS)
def revoke_key(key_id: str, p: Principal = Depends(require(rbac.P_KEYS)), c: Container = Depends(get_container)) -> Response:
    c.accounts.revoke_key(p.account_id, key_id)
    return Response(status_code=204)


# --- sources ----------------------------------------------------------------------------------------------
@router.get("/sources", response_model=s.SourceList, tags=["sources"], responses=ERRORS)
def list_sources(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> s.SourceList:
    rows = c.accounts.list_sources(p.account_id)
    return s.SourceList(
        total=len(rows), limit=limit, offset=offset, items=[_source_out(r) for r in rows[offset : offset + limit]]
    )


@router.post("/sources", response_model=s.SourceOut, status_code=201, tags=["sources"], responses=ERRORS)
def create_source(
    body: s.SourceCreate,
    p: Principal = Depends(require(rbac.P_SOURCES)),
    c: Container = Depends(get_container),
    idempotency_key: str | None = Header(default=None),
) -> Response:
    def run() -> tuple[int, dict[str, Any]]:
        row = c.accounts.create_source(
            p.account_id,
            kind=body.kind,
            name=body.name,
            base_url=body.base_url,
            api_key=body.api_key,
            pepper=body.identity_pepper,
        )
        return 201, _jsonable(_source_out(row))

    status, payload, replay = idempotent(
        c,
        p.account_id,
        idempotency_key,
        body.model_dump(exclude={"api_key", "identity_pepper"}) | {"h": hash((body.api_key, body.identity_pepper)) % 10**9},
        run,
    )
    return JSONResponse(payload, status_code=status, headers={"Idempotent-Replay": "true"} if replay else {})


@router.get("/sources/{source_id}", response_model=s.SourceOut, tags=["sources"], responses=ERRORS)
def get_source(
    source_id: str, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)
) -> s.SourceOut:
    return _source_out(c.accounts.get_source(p.account_id, source_id))


@router.patch("/sources/{source_id}", response_model=s.SourceOut, tags=["sources"], responses=ERRORS)
def patch_source(
    source_id: str, body: s.SourcePatch, p: Principal = Depends(require(rbac.P_SOURCES)), c: Container = Depends(get_container)
) -> s.SourceOut:
    row = c.accounts.update_source(
        p.account_id,
        source_id,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        pepper=body.identity_pepper,
        status=body.status,
    )
    return _source_out(row)


@router.delete("/sources/{source_id}", status_code=204, tags=["sources"], responses=ERRORS)
def delete_source(
    source_id: str, p: Principal = Depends(require(rbac.P_SOURCES)), c: Container = Depends(get_container)
) -> Response:
    c.accounts.delete_source(p.account_id, source_id)
    return Response(status_code=204)


# --- sync -----------------------------------------------------------------------------------------------------
@router.post(
    "/sync",
    response_model=s.JobOut,
    status_code=202,
    tags=["sync"],
    responses=ERRORS,
    summary="Trigger an incremental sync (idempotent with Idempotency-Key)",
)
async def trigger_sync(
    body: s.SyncRequest,
    wait: bool = Query(False, description="Run inline and return the finished job"),
    p: Principal = Depends(require(rbac.P_SYNC)),
    c: Container = Depends(get_container),
    idempotency_key: str | None = Header(default=None),
) -> Response:
    if body.source_id:
        c.accounts.get_source(p.account_id, body.source_id)  # 404 for other accounts' sources

    def run() -> tuple[int, dict[str, Any]]:
        jid = c.queue.enqueue(p.account_id, "sync", {"source_id": body.source_id, "full": body.full})
        return 202, {"job_id": jid}

    status, payload, replay = idempotent(c, p.account_id, idempotency_key, body.model_dump(), run)
    jid = payload["job_id"]
    if wait and not replay:
        await c.runner.run_job(jid)
    with session_scope(c.sessions) as sess:
        job = sess.get(m.Job, jid)
        out = _job_out(job) if job else None
    c.metrics.jobs.labels("sync", out.status if out else "unknown").inc()
    return JSONResponse(_jsonable(out), status_code=status, headers={"Idempotent-Replay": "true"} if replay else {})


@router.get("/sync/status", response_model=s.SyncStatus, tags=["sync"], responses=ERRORS)
def sync_status(p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)) -> s.SyncStatus:
    with session_scope(c.sessions) as sess:
        states = (
            sess.execute(
                select(m.SyncState)
                .where(m.SyncState.account_id == p.account_id)
                .order_by(m.SyncState.source_id, m.SyncState.resource)
            )
            .scalars()
            .all()
        )
        sources = [
            {
                "source_id": st.source_id,
                "resource": st.resource,
                "watermark": st.watermark,
                "last_run_at": st.last_run_at,
                "last_status": st.last_status,
                "rows_total": st.rows_total,
                "bad_lines_total": st.bad_lines_total,
                "resumable_cursor": st.cursor is not None,
                "error": st.error,
            }
            for st in states
        ]
        jobs = (
            sess.execute(select(m.Job).where(m.Job.account_id == p.account_id).order_by(m.Job.created_at.desc()).limit(20))
            .scalars()
            .all()
        )
        return s.SyncStatus(sources=sources, jobs=[_job_out(j) for j in jobs])


@router.get("/jobs/{job_id}", response_model=s.JobOut, tags=["sync"], responses=ERRORS)
def get_job(job_id: str, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)) -> s.JobOut:
    with session_scope(c.sessions) as sess:
        j = sess.get(m.Job, job_id)
        if j is None or j.account_id != p.account_id:
            raise NotFound("job not found")
        return _job_out(j)


# --- rankings --------------------------------------------------------------------------------------------------
def _rerank_steps(c: Container, account_id: str, res: dict[str, Any]) -> None:
    items = [
        {
            "id": f"{r['funnel_id']}:{r['step_id']}",
            "label": f"{r['step_name']}",
            "expected_uplift": r["expected_uplift"],
            "content": r.get("content"),
            "content_hints": [h["code"] for h in r.get("content_hints", [])],
            "row": r,
        }
        for r in res["steps"]
    ]
    ordered, meta = rerank(c.gateway, account_id, items)
    if meta["reranked"]:
        res["steps"] = [i["row"] for i in ordered]
        for n, row in enumerate(res["steps"], 1):
            row["ml_rank"], row["rank"] = row["rank"], n
    res["rerank"] = meta


@router.get("/rankings/funnels", response_model=s.RankingResponse, tags=["rankings"], responses=ERRORS)
def rank_funnels(
    request: Request,
    rerank_llm: bool = Query(False, alias="rerank"),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    def build() -> dict[str, Any]:
        res = c.analytics.funnel_rankings(p.account_id)
        if rerank_llm:
            _rerank_steps(c, p.account_id, res)
        return res

    return _cached(request, c, p, build)


@router.get("/rankings/steps", response_model=s.RankingResponse, tags=["rankings"], responses=ERRORS)
def rank_steps_route(
    request: Request,
    funnel_id: str | None = None,
    rerank_llm: bool = Query(False, alias="rerank"),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    def build() -> dict[str, Any]:
        res = c.analytics.funnel_rankings(p.account_id, funnel_id=funnel_id)
        if rerank_llm:
            _rerank_steps(c, p.account_id, res)
        return res

    return _cached(request, c, p, build)


@router.get("/leads/scores", response_model=s.LeadScoresResponse, tags=["leads"], responses=ERRORS)
def lead_scores(
    request: Request,
    min_score: float = Query(0.0, ge=0, le=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: str = Query("open", pattern="^(open|all)$"),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    return _cached(
        request, c, p, lambda: c.analytics.lead_scores(p.account_id, min_score=min_score, limit=limit, offset=offset, scope=scope)
    )


@router.post(
    "/leads/score",
    tags=["leads"],
    responses=ERRORS,
    summary="Score one contact (by contact_key or email) or a raw feature vector",
)
def score_lead(
    body: s.LeadScoreRequest, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)
) -> dict[str, Any]:
    if sum(x is not None for x in (body.contact_key, body.email, body.features)) != 1:
        raise Unprocessable("provide exactly one of contact_key, email or features", code="INVALID_INPUT")
    return c.analytics.score_lead(p.account_id, contact_key_value=body.contact_key, email=body.email, features=body.features)


@router.post(
    "/leads/score/batch",
    response_model=s.BatchScoreResponse,
    tags=["leads"],
    responses=ERRORS,
    summary="Score up to BATCH_MAX_ROWS leads in one vectorised call",
)
def score_batch(
    body: s.BatchScoreRequest, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)
) -> dict[str, Any]:
    if (body.features is None) == (body.contact_keys is None):
        raise Unprocessable("provide exactly one of features or contact_keys", code="INVALID_INPUT")
    rows = body.features if body.features is not None else body.contact_keys
    if rows is not None and len(rows) > c.settings.batch_max_rows:
        raise Unprocessable(f"at most {c.settings.batch_max_rows} rows per batch", code="BATCH_TOO_LARGE")
    return c.analytics.score_batch(p.account_id, features=body.features, contact_keys=body.contact_keys)


@router.get("/rankings/conversations/open", response_model=s.OpenConversationsResponse, tags=["rankings"], responses=ERRORS)
def open_conversations(
    request: Request,
    limit: int = Query(25, ge=1, le=200),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    return _cached(request, c, p, lambda: c.analytics.open_conversations(p.account_id, limit=limit))


@router.get("/segments", response_model=s.SegmentsResponse, tags=["segments"], responses=ERRORS)
def segments(request: Request, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)) -> Response:
    return _cached(request, c, p, lambda: c.analytics.segments(p.account_id))


@router.get("/anomalies", response_model=s.AnomaliesResponse, tags=["anomalies"], responses=ERRORS)
def anomalies(
    request: Request,
    since_days: int = Query(30, ge=1, le=366),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    def build() -> dict[str, Any]:
        res = c.analytics.detect_anomalies(p.account_id)
        cutoff = int(c.clock()) - since_days * 86400
        res["alerts"] = [a for a in res["alerts"] if a["end_t"] >= cutoff]
        return res

    return _cached(request, c, p, build, salt=f"{int(c.clock()) // 3600}")


@router.get(
    "/join", tags=["diagnostics"], responses=ERRORS, summary="Cross-app identity join coverage and pepper-mismatch warnings"
)
def join(p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.analytics.join(p.account_id)


@router.get("/insights", response_model=s.InsightsResponse, tags=["insights"], responses=ERRORS)
def get_insights(
    request: Request,
    narrate: bool = Query(True, description="false forces the deterministic template narrator"),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> Response:
    def build() -> dict[str, Any]:
        out = build_insights(c.analytics, c.narrator, p.account_id, use_llm=narrate)
        c.metrics.llm.labels(out["narrative"].get("source", "template")).inc()
        return out

    return _cached(request, c, p, build, salt=f"{int(c.clock()) // 3600}|{narrate}")


@router.post(
    "/ask",
    response_model=s.AskResponse,
    tags=["insights"],
    responses=ERRORS,
    summary="Ask a question; answered via read-only analytic tools",
)
def ask(body: s.AskRequest, p: Principal = Depends(require(rbac.P_ASK)), c: Container = Depends(get_container)) -> s.AskResponse:
    r = c.ask.ask(p.account_id, body.question, build_tools(c.analytics, p.account_id))
    return s.AskResponse(
        answer=r.answer, source=r.source, tools_used=r.tools_used, evidence=r.evidence, fallback_reason=r.fallback_reason
    )


# --- models ----------------------------------------------------------------------------------------------------------
@router.get("/models", response_model=list[s.ModelOut], tags=["models"], responses=ERRORS)
def list_models(
    model_type: str | None = None, p: Principal = Depends(require(rbac.P_READ)), c: Container = Depends(get_container)
) -> list[s.ModelOut]:
    return [_model_out(r) for r in c.registry.list(p.account_id, model_type)]


@router.post("/models/train", status_code=202, tags=["models"], responses=ERRORS)
async def train_models(
    body: s.TrainRequest,
    wait: bool = Query(False),
    p: Principal = Depends(require(rbac.P_TRAIN)),
    c: Container = Depends(get_container),
    idempotency_key: str | None = Header(default=None),
) -> Response:
    bad = [t for t in (body.model_types or []) if t not in MODEL_TYPES]
    if bad:
        raise Unprocessable(f"unknown model type(s): {', '.join(bad)}", code="UNKNOWN_MODEL_TYPE")

    def run() -> tuple[int, dict[str, Any]]:
        return 202, {"job_id": c.queue.enqueue(p.account_id, "train", {"model_types": body.model_types})}

    status, payload, replay = idempotent(c, p.account_id, idempotency_key, body.model_dump(), run)
    if wait and not replay:
        await c.runner.run_job(payload["job_id"])
    with session_scope(c.sessions) as sess:
        job = sess.get(m.Job, payload["job_id"])
        out = _job_out(job) if job else None
    return JSONResponse(_jsonable(out), status_code=status, headers={"Idempotent-Replay": "true"} if replay else {})


@router.post("/models/{version_id}/promote", response_model=s.ModelOut, tags=["models"], responses=ERRORS)
def promote(version_id: str, p: Principal = Depends(require(rbac.P_MODELS)), c: Container = Depends(get_container)) -> s.ModelOut:
    c.registry.get(p.account_id, version_id)
    row = c.registry.promote(p.account_id, version_id)
    c.accounts.update_account(p.account_id, name=None, settings=None)  # bumps data_version -> invalidates ETags
    return _model_out(row)


@router.post("/models/types/{model_type}/rollback", response_model=s.ModelOut, tags=["models"], responses=ERRORS)
def rollback(
    model_type: str, p: Principal = Depends(require(rbac.P_MODELS)), c: Container = Depends(get_container)
) -> s.ModelOut:
    if model_type not in MODEL_TYPES:
        raise NotFound("unknown model type")
    row = c.registry.rollback(p.account_id, model_type)
    c.accounts.update_account(p.account_id, name=None, settings=None)
    return _model_out(row)


@router.get(
    "/models/types/{model_type}/drift",
    tags=["models"],
    responses=ERRORS,
    summary="PSI drift of recent data vs the training reference",
)
def drift(
    model_type: str,
    recent_days: int = Query(14, ge=1, le=90),
    p: Principal = Depends(require(rbac.P_READ)),
    c: Container = Depends(get_container),
) -> dict[str, Any]:
    if model_type not in MODEL_TYPES:
        raise NotFound("unknown model type")
    return c.analytics.drift(p.account_id, model_type, recent_days=recent_days)
