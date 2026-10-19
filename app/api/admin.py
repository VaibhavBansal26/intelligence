"""Platform-operator API (separate admin key). Not tenant scoped by design."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api import schemas as s
from app.api.auth import require_admin
from app.db import models as m
from app.db.session import session_scope
from app.deps import Container, get_container
from app.errors import Conflict

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], tags=["admin"])
ERR: dict[int | str, dict[str, Any]] = {401: {"model": s.ErrorResponse}, 404: {"model": s.ErrorResponse}}


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    settings: dict[str, Any] = Field(default_factory=dict)


class AccountPatchAdmin(BaseModel):
    disabled: bool | None = None
    name: str | None = None
    settings: dict[str, Any] | None = None


@router.post("/accounts", status_code=201, responses=ERR, summary="Create an account and its first admin key (shown once)")
def create_account(body: AccountCreate, c: Container = Depends(get_container)) -> dict[str, Any]:
    acct = c.accounts.create_account(body.name, body.settings)
    key, plaintext = c.accounts.create_key(acct.id, name="initial-admin", role="admin")
    return {"account": {"id": acct.id, "name": acct.name, "created_at": acct.created_at}, "api_key": plaintext, "key_id": key.id}


@router.get("/accounts", responses=ERR)
def list_accounts(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), c: Container = Depends(get_container)
) -> dict[str, Any]:
    rows = c.accounts.list_accounts()
    return {
        "total": len(rows),
        "items": [
            {"id": a.id, "name": a.name, "created_at": a.created_at, "disabled": a.disabled}
            for a in rows[offset : offset + limit]
        ],
    }


@router.patch("/accounts/{account_id}", responses=ERR)
def patch_account(account_id: str, body: AccountPatchAdmin, c: Container = Depends(get_container)) -> dict[str, Any]:
    a = c.accounts.update_account(account_id, name=body.name, settings=body.settings, disabled=body.disabled)
    return {"id": a.id, "name": a.name, "disabled": a.disabled}


@router.get("/accounts/{account_id}/usage", responses=ERR)
def usage(account_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    c.accounts.get_account(account_id)
    with session_scope(c.sessions) as sess:

        def count(model: Any) -> int:
            return int(sess.execute(select(func.count()).select_from(model).where(model.account_id == account_id)).scalar() or 0)

        llm = (
            sess.execute(select(m.LlmUsage).where(m.LlmUsage.account_id == account_id).order_by(m.LlmUsage.day.desc()).limit(30))
            .scalars()
            .all()
        )
        return {
            "account_id": account_id,
            "sources": count(m.Source),
            "events": count(m.Event),
            "leads": count(m.Lead),
            "rooms": count(m.Room),
            "messages_meta": count(m.MessageMeta),
            "rollups": count(m.Rollup),
            "api_keys": count(m.ApiKey),
            "jobs": count(m.Job),
            "budgets": {
                "max_event_rows": c.settings.account_max_event_rows,
                "max_concurrent_jobs": c.settings.account_max_concurrent_jobs,
                "max_training_rows": c.settings.ml_max_training_rows,
                "llm_daily_calls": c.settings.llm_daily_call_cap,
            },
            "llm": [
                {
                    "day": u.day,
                    "calls": u.calls,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_hits": u.cache_hits,
                }
                for u in llm
            ],
        }


@router.post(
    "/accounts/{account_id}/refresh-profiles",
    status_code=202,
    responses=ERR,
    summary="Fan the Parquet profile refresh out over N worker jobs",
)
def refresh_profiles_jobs(
    account_id: str, shards: int = Query(4, ge=1, le=64), c: Container = Depends(get_container)
) -> dict[str, Any]:
    c.accounts.get_account(account_id)
    if not c.settings.columnar_dir:
        raise Conflict("COLUMNAR_DIR is not configured", code="COLUMNAR_DISABLED")
    buckets = c.settings.columnar_buckets
    ids = [
        c.queue.enqueue(account_id, "profiles", {"buckets": list(range(i, buckets, shards))}) for i in range(min(shards, buckets))
    ]
    return {"job_ids": ids, "shards": len(ids)}


@router.get("/models", responses=ERR)
def all_models(account_id: str | None = None, c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    with session_scope(c.sessions) as sess:
        q = select(m.ModelVersion).order_by(m.ModelVersion.account_id, m.ModelVersion.model_type, m.ModelVersion.version.desc())
        if account_id:
            q = q.where(m.ModelVersion.account_id == account_id)
        return [
            {
                "id": r.id,
                "account_id": r.account_id,
                "model_type": r.model_type,
                "version": r.version,
                "status": r.status,
                "method": r.method,
                "auc": r.metrics.get("auc"),
            }
            for r in sess.execute(q.limit(500)).scalars()
        ]


@router.post("/accounts/{account_id}/retrain", status_code=202, responses=ERR)
async def retrain(account_id: str, wait: bool = Query(False), c: Container = Depends(get_container)) -> dict[str, Any]:
    c.accounts.get_account(account_id)
    jid = c.queue.enqueue(account_id, "train", {"model_types": None})
    if wait:
        await c.runner.run_job(jid)
    with session_scope(c.sessions) as sess:
        j = sess.get(m.Job, jid)
        return {"job_id": jid, "status": j.status if j else "unknown", "error": j.error if j else None}


@router.get("/jobs", responses=ERR)
def jobs(
    status: str | None = None, limit: int = Query(50, ge=1, le=500), c: Container = Depends(get_container)
) -> list[dict[str, Any]]:
    with session_scope(c.sessions) as sess:
        q = select(m.Job).order_by(m.Job.created_at.desc()).limit(limit)
        if status:
            q = q.where(m.Job.status == status)
        return [
            {"id": j.id, "account_id": j.account_id, "type": j.type, "status": j.status, "attempts": j.attempts, "error": j.error}
            for j in sess.execute(q).scalars()
        ]
