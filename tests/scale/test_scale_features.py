from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.config import Settings
from app.db import models as m
from app.db.session import create_all, make_engine, make_session_factory, session_scope
from app.ingest.jobs import DbJobQueue, JobRunner
from app.models.gbm import TrainSpec, train_binary
from tests.conftest import Env, build_env


# --- batch scoring ----------------------------------------------------------------------------------------
def test_batch_scoring_by_features_and_keys(env: Env) -> None:
    top = env.client.get("/v1/leads/scores?limit=3", headers=env.h).json()["items"]
    keys = [i["contact_key"] for i in top] + ["unknown-key"]
    r = env.client.post("/v1/leads/score/batch", json={"contact_keys": keys}, headers=env.h).json()
    assert r["count"] == 4 and r["not_found"] == [3] and r["scores"][3] is None
    assert [round(s, 4) for s in r["scores"][:3]] == [i["score"] for i in top]
    feats = {"utm_source": "email", "n_page_views_pre": 5, "prior_intent_purchase": 1}
    f = env.client.post("/v1/leads/score/batch", json={"features": [feats, {"utm_source": "facebook"}]}, headers=env.h).json()
    assert f["method"] == "gbm" and len(f["scores"]) == 2 and f["scores"][0] > f["scores"][1]


def test_batch_validation_and_limit(env: Env) -> None:
    assert env.client.post("/v1/leads/score/batch", json={}, headers=env.h).json()["error"]["code"] == "INVALID_INPUT"
    assert (
        env.client.post("/v1/leads/score/batch", json={"features": [{}], "contact_keys": ["a"]}, headers=env.h).status_code == 422
    )
    big = {"features": [{"utm_source": "email"}] * (env.app.state.container.settings.batch_max_rows + 1)}
    r = env.client.post("/v1/leads/score/batch", json=big, headers=env.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "BATCH_TOO_LARGE"
    assert env.client.post("/v1/leads/score/batch", json={"features": []}, headers=env.h).json()["scores"] == []


def test_inference_throughput_exceeds_5000_scores_per_second(env: Env) -> None:
    svc = env.app.state.container.analytics
    rows = [
        {
            "utm_source": ["email", "facebook", "google"][i % 3],
            "n_page_views_pre": i % 6,
            "prior_intent_purchase": i % 2,
            "device": "mobile",
        }
        for i in range(5000)
    ]
    svc.score_batch(env.account_id, features=rows[:10], contact_keys=None)  # warm model + frame caches
    t = time.perf_counter()
    out = svc.score_batch(env.account_id, features=rows, contact_keys=None)
    rate = 5000 / (time.perf_counter() - t)
    assert out["count"] == 5000 and rate > 5000, f"{rate:.0f} scores/s"


# --- model cache and hot swap -----------------------------------------------------------------------------------
def test_model_cache_hits_and_hot_swap_without_restart(fresh_env: Env) -> None:
    e, c = fresh_env, fresh_env.app.state.container
    e.client.post("/v1/models/train?wait=true", json={"model_types": ["sla_breach"]}, headers=e.h)
    e.client.post("/v1/models/train?wait=true", json={"model_types": ["sla_breach"]}, headers=e.h)
    versions = {v["version"]: v["id"] for v in e.client.get("/v1/models?model_type=sla_breach", headers=e.h).json()}
    assert e.client.get("/v1/rankings/conversations/open", headers=e.h).json()["model_version"] == 2
    loads = c.registry.cache_loads
    for _ in range(3):
        e.client.get("/v1/rankings/conversations/open?limit=5", headers=e.h)
    assert c.registry.cache_loads == loads and c.registry.cache_hits >= 3  # no artifact reload per request
    e.client.post(f"/v1/models/{versions[1]}/promote", headers=e.h)
    assert e.client.get("/v1/rankings/conversations/open?limit=6", headers=e.h).json()["model_version"] == 1  # swapped live
    e.client.post(f"/v1/models/{versions[2]}/promote", headers=e.h)
    assert e.client.get("/v1/rankings/conversations/open?limit=7", headers=e.h).json()["model_version"] == 2


def test_feature_frames_are_cached_until_data_changes(fresh_env: Env) -> None:
    svc = fresh_env.app.state.container.analytics
    a = svc._lead_frame(fresh_env.account_id)
    assert svc._lead_frame(fresh_env.account_id) is a
    fresh_env.client.post("/v1/sync?wait=true", json={}, headers=fresh_env.h)
    assert svc._lead_frame(fresh_env.account_id) is not a


# --- per-account budgets ----------------------------------------------------------------------------------------
def test_event_row_budget_stops_sync_with_clear_error(small_world, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    env = next(build_env(tmp_path, small_world, train=False, check_sync=False, account_max_event_rows=1000))
    job = env.client.get("/v1/sync/status", headers=env.h).json()["jobs"][0]
    assert any("event budget reached (1000 rows)" in e for e in job["result"]["errors"])
    usage = env.client.get(f"/admin/accounts/{env.account_id}/usage", headers=env.admin).json()
    assert usage["budgets"]["max_event_rows"] == 1000 and usage["events"] <= 1000 + 5000  # stops at the first chunk over the cap
    assert any(
        s["resource"] == "rooms" and s["last_status"] == "ok"
        for s in env.client.get("/v1/sync/status", headers=env.h).json()["sources"]
    )


def test_training_row_cap_uses_most_recent_rows() -> None:
    rng = np.random.default_rng(0)
    n = 3000
    df = pd.DataFrame({"t": np.arange(n) * 60.0, "x": rng.normal(size=n), "y": rng.integers(0, 2, n)})
    spec = TrainSpec("t", ["x"], [], "t", "y", 0.0, lambda d, b: np.full(len(d), b), {}, 200, 20, 1, max_rows=1000)
    out = train_binary(df, spec)
    assert out.card["rows"] == 1000 and out.card["training_window"]["from"] == int(df["t"].iloc[-1000])


def make_queue(tmp_path: Path, per_account: int) -> DbJobQueue:
    st = Settings(env="test", database_url=f"sqlite:///{tmp_path}/q.db")
    eng = make_engine(st.database_url)
    create_all(eng)
    return DbJobQueue(make_session_factory(eng), lambda: 1000.0, per_account)


def test_queue_caps_concurrency_per_account_and_schedules_fairly(tmp_path: Path) -> None:
    q = make_queue(tmp_path, per_account=1)
    heavy = [q.enqueue("heavy", "train", {}) for _ in range(4)]
    light = q.enqueue("light", "train", {})
    first, second = q.claim(), q.claim()
    assert first is not None and second is not None
    assert {first.account_id, second.account_id} == {"heavy", "light"}  # fair: the idle account is served before heavy's 2nd job
    assert q.claim() is None  # both accounts at their cap
    q.finish(first.id, {})
    q.finish(second.id, {})
    nxt = q.claim()
    assert nxt is not None and nxt.id != first.id and light and heavy


def test_concurrent_workers_never_double_claim(tmp_path: Path) -> None:
    q = make_queue(tmp_path, per_account=0)
    ids = {q.enqueue(f"acct{i % 3}", "noop", {}) for i in range(30)}
    claimed: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        while (j := q.claim()) is not None:
            with lock:
                claimed.append(j.id)
            q.finish(j.id, {})

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(claimed) == sorted(ids) and len(set(claimed)) == 30


def test_runner_pool_processes_each_job_once(tmp_path: Path) -> None:
    q = make_queue(tmp_path, per_account=0)
    seen: list[str] = []

    async def handler(job: m.Job) -> dict[str, Any]:
        seen.append(job.id)
        await asyncio.sleep(0.01)
        return {}

    ids = [q.enqueue("a", "noop", {}) for _ in range(12)]
    runners = [JobRunner(q, {"noop": handler}) for _ in range(3)]

    async def main() -> None:
        tasks = [asyncio.create_task(r.run_forever(0.01)) for r in runners]
        while len(seen) < 12:
            await asyncio.sleep(0.02)
        for r in runners:
            r.stop()
        await asyncio.gather(*tasks)

    asyncio.run(asyncio.wait_for(main(), 30))
    assert sorted(seen) == sorted(ids)
    with session_scope(q._sessions) as s:
        assert {j.status for j in s.query(m.Job).all()} == {"succeeded"}
