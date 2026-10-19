from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.db import models as m
from app.db.session import session_scope
from app.synthetic.world import WorldConfig, generate_world
from tests.conftest import Env, build_env


def count(env: Env, model: type) -> int:
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        return int(s.execute(select(func.count()).select_from(model)).scalar() or 0)


def sync(env: Env, **body: object) -> dict:
    r = env.client.post("/v1/sync?wait=true", json=body, headers=env.h)
    assert r.status_code == 202, r.text
    return r.json()


def test_all_rows_landed_exactly_once(fresh_env: Env) -> None:
    w = fresh_env.world
    assert count(fresh_env, m.Event) == len(w.events)
    assert count(fresh_env, m.Lead) == len(w.leads)
    assert count(fresh_env, m.Room) == len(w.rooms)
    assert count(fresh_env, m.MessageMeta) == len(w.messages_meta)


def test_resync_is_idempotent_and_incremental(fresh_env: Env) -> None:
    before = {t: count(fresh_env, t) for t in (m.Event, m.Lead, m.Room, m.MessageMeta, m.Rollup)}
    fresh_env.funnels_app.state.request_log.clear()
    job = sync(fresh_env)
    assert job["status"] == "succeeded"
    assert {t: count(fresh_env, t) for t in before} == before
    event_pages = [p for p in fresh_env.funnels_app.state.request_log if p.endswith("/export/events")]
    assert len(event_pages) <= 2  # watermark: only the tail page, not the full history again
    full = sync(fresh_env, full=True)
    assert full["status"] == "succeeded" and count(fresh_env, m.Event) == before[m.Event]


def test_watermarks_recorded(fresh_env: Env) -> None:
    c = fresh_env.app.state.container
    with session_scope(c.sessions) as s:
        wm = {r.resource: r.watermark for r in s.execute(select(m.SyncState)).scalars()}
    assert wm["events"] == max(
        __import__("app.connectors.records", fromlist=["parse_ts"]).parse_ts(e["ts"]) for e in fresh_env.world.events
    )
    assert wm["rooms"] and wm["leads"]


def test_mutable_resource_updates_are_picked_up(fresh_env: Env) -> None:
    """Leads convert after the first sync: the lookback window re-reads and upserts them."""
    lead = next(ld for ld in fresh_env.world.leads if not ld["converted_to_order"] and ld["created_at"] > "2026-09-25")
    lead["converted_to_order"], lead["order_total"], lead["first_order_at"] = True, 99.0, "2026-09-30T12:00:00Z"
    sync(fresh_env)
    c = fresh_env.app.state.container
    with session_scope(c.sessions) as s:
        row = s.execute(select(m.Lead).where(m.Lead.ext_id == lead["id"])).scalar_one()
    assert row.converted_to_order is True and row.order_total == 99.0


def test_interrupted_sync_resumes_from_cursor(tmp_path: Path) -> None:
    world = generate_world(WorldConfig(seed=3, n_days=30, visitors_per_day=60, support_only_contacts=50))
    gen = build_env(tmp_path, world, train=False, connector_max_attempts=2, connector_page_limit=200, ingest_chunk_rows=200)
    env = next(gen)
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        s.execute(m.Event.__table__.delete())
        s.execute(m.SyncState.__table__.update().where(m.SyncState.resource == "events").values(watermark=None, cursor=None))
    env.funnels_app.state.faults["/export/events"] = ["", "", "503", "503", "503", "503"]
    job = sync(env)
    assert job["status"] == "succeeded" and job["result"]["errors"]
    with session_scope(c.sessions) as s:
        st = s.execute(select(m.SyncState).where(m.SyncState.resource == "events")).scalar_one()
        assert st.cursor is not None and st.last_status == "error" and st.watermark is None
    partial = count(env, m.Event)
    assert 0 < partial < len(world.events)
    env.funnels_app.state.faults.clear()
    job = sync(env)
    assert not job["result"]["errors"]
    assert next(r for r in job["result"]["sources"][0]["resources"] if r["resource"] == "events")["resumed"] is True
    assert count(env, m.Event) == len(world.events)
    gen.close()


def test_truncated_and_malformed_pages_do_not_lose_or_duplicate_rows(tmp_path: Path) -> None:
    world = generate_world(WorldConfig(seed=4, n_days=30, visitors_per_day=60, support_only_contacts=50))
    gen = build_env(tmp_path, world, train=False, connector_page_limit=300)
    env = next(gen)
    c = env.app.state.container
    with session_scope(c.sessions) as s:
        s.execute(m.Room.__table__.delete())
        s.execute(m.SyncState.__table__.update().where(m.SyncState.resource == "rooms").values(watermark=None, cursor=None))
    env.conv_app.state.faults["/export/rooms"] = ["truncate", "bad_line"]
    job = sync(env)
    assert not job["result"]["errors"]
    assert count(env, m.Room) == len(world.rooms)
    res = next(r for s in job["result"]["sources"] for r in s["resources"] if r["resource"] == "rooms")
    assert res["bad_lines"] == 1 and res["retries"] >= 1
    gen.close()


def test_bad_credentials_fail_the_job_and_stop(fresh_env: Env) -> None:
    src = fresh_env.client.get("/v1/sources", headers=fresh_env.h).json()["items"][0]
    fresh_env.client.patch(f"/v1/sources/{src['id']}", json={"api_key": "wrong-key-123456"}, headers=fresh_env.h)
    job = sync(fresh_env, source_id=src["id"])
    assert job["status"] == "failed" and "rejected credentials" in job["error"]


def test_schema_version_newer_warns_older_fails(fresh_env: Env) -> None:
    fresh_env.funnels_app.state.schema_version = 7
    src = next(s for s in fresh_env.client.get("/v1/sources", headers=fresh_env.h).json()["items"] if s["kind"] == "funnels")
    job = sync(fresh_env, source_id=src["id"])
    assert job["status"] == "succeeded" and "newer" in job["result"]["sources"][0]["warnings"][0]
    fresh_env.funnels_app.state.schema_version = 0
    job = sync(fresh_env, source_id=src["id"])
    assert (job["status"] == "failed" and "older" in job["error"]) or job["result"]["errors"]
    fresh_env.funnels_app.state.schema_version = 1


def test_secrets_encrypted_at_rest(fresh_env: Env) -> None:
    c = fresh_env.app.state.container
    with session_scope(c.sessions) as s:
        for src in s.execute(select(m.Source)).scalars():
            assert "fa_test_key" not in src.api_key_enc and "ca_test_key" not in src.api_key_enc
            assert fresh_env.world.config.pepper not in src.pepper_enc
    assert "api_key" not in fresh_env.client.get("/v1/sources", headers=fresh_env.h).text


def test_pepper_mismatch_is_detected(tmp_path: Path) -> None:
    world = generate_world(
        WorldConfig(seed=5, n_days=45, visitors_per_day=60, support_only_contacts=150, conversations_pepper="a-different-pepper")
    )
    gen = build_env(tmp_path, world, train=False)
    env = next(gen)
    # funnels source got world pepper; give the conversations source the pepper the conversation app really uses
    srcs = env.client.get("/v1/sources", headers=env.h).json()["items"]
    conv = next(s for s in srcs if s["kind"] == "conversations")
    env.client.patch(f"/v1/sources/{conv['id']}", json={"identity_pepper": "a-different-pepper"}, headers=env.h)
    j = env.client.get("/v1/join", headers=env.h).json()
    codes = " ".join(j["warnings"])
    assert "pepper_fingerprint_mismatch" in codes and "pepper_mismatch_suspected" in codes
    assert (j["coverage_of_smaller_side"] or 0) < 0.01
    gen.close()


def test_single_source_account_still_works(tmp_path: Path) -> None:
    world = generate_world(WorldConfig(seed=6, n_days=60, visitors_per_day=90, include_conversations=False))
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.synthetic.fake_sources import create_fake_funnels_app
    from tests.conftest import ADMIN_KEY, FUNNELS_URL, fake_http_factory, make_settings

    app = create_app(
        make_settings(tmp_path),
        http_factory=fake_http_factory(create_fake_funnels_app(world), None),
        clock=lambda: float(world.end_ts),
        use_env_llm=False,
    )
    with TestClient(app) as client:
        r = client.post("/admin/accounts", json={"name": "solo"}, headers={"X-Admin-Key": ADMIN_KEY}).json()
        h = {"Authorization": f"Bearer {r['api_key']}"}
        client.post(
            "/v1/sources",
            json={
                "kind": "funnels",
                "name": "f",
                "base_url": FUNNELS_URL,
                "api_key": "fa_test_key",
                "identity_pepper": world.config.pepper,
            },
            headers=h,
        )
        assert client.post("/v1/sync?wait=true", json={}, headers=h).json()["status"] == "succeeded"
        assert client.post("/v1/models/train?wait=true", json={}, headers=h).json()["status"] == "succeeded"
        scores = client.get("/v1/leads/scores?limit=3", headers=h).json()
        assert scores["conversation_features_used"] is False and scores["items"]
        conv = client.get("/v1/rankings/conversations/open", headers=h).json()
        assert conv["items"] == [] and conv["total_open"] == 0
        assert client.get("/v1/join", headers=h).json()["single_source"] is True
        assert client.get("/v1/rankings/steps", headers=h).json()["steps"]
