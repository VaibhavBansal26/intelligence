"""A bad source must not take the other sources of the account down (found with a revoked key against the real apps)."""

from __future__ import annotations

from tests.conftest import Env


def _source(env: Env, kind: str) -> dict:
    return next(s for s in env.client.get("/v1/sources", headers=env.h).json()["items"] if s["kind"] == kind)


def test_revoked_key_of_one_source_is_reported_per_resource_and_the_other_source_still_syncs(fresh_env: Env) -> None:
    env = fresh_env
    env.client.patch(f"/v1/sources/{_source(env, 'funnels')['id']}", json={"api_key": "fa_live_revoked_key"}, headers=env.h)
    job = env.client.post("/v1/sync?wait=true", json={}, headers=env.h).json()
    assert job["status"] == "succeeded"  # one source is healthy
    by_kind = {s["kind"]: s for s in job["result"]["sources"]}
    f_res = by_kind["funnels"]["resources"]
    assert [r["resource"] for r in f_res] == ["funnel_structure", "funnels", "events", "leads", "rollups"]
    assert all(r["error"] and "credentials" in r["error"] for r in f_res)
    assert all(r["error"] is None for r in by_kind["conversations"]["resources"])
    status = env.client.get("/v1/sync/status", headers=env.h).json()["sources"]
    assert any(s["resource"] == "events" and s["last_status"] == "error" and "credentials" in s["error"] for s in status)
    assert any(s["resource"] == "rooms" and s["last_status"] == "ok" for s in status)


def test_when_every_source_is_rejected_the_job_fails_with_the_reason(fresh_env: Env) -> None:
    env = fresh_env
    for kind in ("funnels", "conversations"):
        env.client.patch(f"/v1/sources/{_source(env, kind)['id']}", json={"api_key": "expired_key_xx"}, headers=env.h)
    job = env.client.post("/v1/sync?wait=true", json={}, headers=env.h).json()
    assert job["status"] == "failed" and "credentials" in job["error"]


def test_snapshot_resources_report_their_row_counts_in_sync_status(fresh_env: Env) -> None:
    status = {(s["resource"]): s for s in fresh_env.client.get("/v1/sync/status", headers=fresh_env.h).json()["sources"]}
    assert status["funnels"]["rows_total"] > 0 and status["funnel_structure"]["rows_total"] > 0


def test_backfill_window_never_exceeds_the_apps_366_day_range_limit(fresh_env: Env) -> None:
    from app.ingest.sync import MAX_RANGE_DAYS

    c = fresh_env.app.state.container
    c.settings.backfill_days = 1000  # a misconfiguration the apps would answer with 400 RANGE_TOO_LARGE
    now = int(c.clock())
    assert now - c.ingest._from_ts(None, "events") == MAX_RANGE_DAYS * 86400
    assert now - c.ingest._from_ts(now - 900 * 86400, "events") <= MAX_RANGE_DAYS * 86400
