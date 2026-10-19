"""Batch-scoring throughput on the synthetic world: `uv run python -m benchmarks.scoring_bench`."""

from __future__ import annotations

import json
import platform
import tempfile
import time
from pathlib import Path

from app.synthetic.world import WorldConfig, generate_world
from tests.conftest import build_env


def main() -> None:
    env = next(build_env(Path(tempfile.mkdtemp()), generate_world(WorldConfig())))
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
    svc.score_batch(env.account_id, features=rows[:10], contact_keys=None)
    out = {}
    for n in (100, 1000, 5000):
        t = time.perf_counter()
        reps = max(1, 20000 // n)
        for _ in range(reps):
            svc.score_batch(env.account_id, features=rows[:n], contact_keys=None)
        dt = time.perf_counter() - t
        out[f"features_batch_{n}"] = {"scores_per_s": int(n * reps / dt), "ms_per_call": round(1000 * dt / reps, 1)}
    keys = [i["contact_key"] for i in svc.lead_scores(env.account_id, limit=200, explain=False)["items"]] * 25
    t = time.perf_counter()
    for _ in range(5):
        svc.score_batch(env.account_id, features=None, contact_keys=keys[:5000])
    out["contact_keys_batch_5000"] = {"scores_per_s": int(5 * 5000 / (time.perf_counter() - t))}
    t = time.perf_counter()
    http = 0
    for _ in range(20):
        env.client.post("/v1/leads/score/batch", json={"features": rows[:1000]}, headers=env.h)
        http += 1000
    out["http_batch_1000"] = {"scores_per_s": int(http / (time.perf_counter() - t))}
    out["machine"] = platform.machine()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
