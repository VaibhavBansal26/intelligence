"""Out-of-core benchmark: write N synthetic events to the Parquet store, refresh contact profiles, report time/RSS/disk.

    uv run python -m benchmarks.columnar_bench --events 100000000 --workers 1
    uv run python -m benchmarks.columnar_bench --events 20000000 --workers 4   # bucket fan-out across processes

Generation is chunked (no dataset is ever held in memory) and deterministic (seeded).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import resource
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np
import polars as pl

from app.columnar.profiles import profiles_lazy, refresh_profiles
from app.columnar.store import ParquetEventStore

TYPES = np.array(["page_view", "page_view", "page_view", "page_view", "optin", "order"])
STEPS = np.array(["s0", "s1", "s2", "s3"])
FUNNELS = np.array(["f0", "f1", "f2"])


def rss_mb(who: int = resource.RUSAGE_SELF) -> float:
    peak = resource.getrusage(who).ru_maxrss
    return peak / (1024 * 1024) if platform.system() == "Darwin" else peak / 1024


def gen_chunk(rng: np.random.Generator, n: int, t0: int, n_contacts: int) -> pl.DataFrame:
    # Zipf-ish contact popularity: a few contacts have many events, most few
    cid = (rng.pareto(1.6, n) * n_contacts / 12).astype(np.int64) % n_contacts
    typ = TYPES[rng.integers(0, len(TYPES), n)]
    return pl.DataFrame(
        {
            "ts": t0 + np.sort(rng.integers(0, 3600, n)),
            "type": typ,
            "funnel_id": FUNNELS[rng.integers(0, 3, n)],
            "step_id": STEPS[rng.integers(0, 4, n)],
            "contact_h": (cid.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)),
            "amount": np.where(typ == "order", rng.normal(80, 20, n), 0.0).astype(np.float32),
        }
    )


def _refresh_worker(args: tuple[str, str, int, list[int]]) -> dict[str, float]:
    root, account, buckets, subset = args
    t = time.monotonic()
    st = refresh_profiles(ParquetEventStore(root, buckets), account, subset)
    return {"seconds": time.monotonic() - t, "rows": st.rows_read, "rss_mb": rss_mb()}


def run(events: int, workers: int, buckets: int, chunk: int, keep: bool, segments: bool = False) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="colbench-"))
    store = ParquetEventStore(root, buckets)
    rng = np.random.default_rng(7)
    n_contacts = max(events // 5, 1000)
    t0 = time.monotonic()
    done = 0
    while done < events:
        n = min(chunk, events - done)
        store.append("acct", gen_chunk(rng, n, 1_790_000_000 + done // 1000, n_contacts))
        done += n
    write_s = time.monotonic() - t0
    rss_after_write = rss_mb()
    t1 = time.monotonic()
    if workers <= 1:
        stats = refresh_profiles(store, "acct")
        rows, child_rss = stats.rows_read, 0.0
    else:
        shards = [list(range(i, buckets, workers)) for i in range(workers)]
        with mp.get_context("spawn").Pool(workers) as pool:
            res = pool.map(_refresh_worker, [(str(root), "acct", buckets, s) for s in shards])
        rows, child_rss = int(sum(r["rows"] for r in res)), max(r["rss_mb"] for r in res)
    refresh_s = time.monotonic() - t1
    t2 = time.monotonic()
    prof = (
        profiles_lazy(store, "acct")
        .select(pl.len().alias("contacts"), pl.col("f_events").sum().alias("events"), pl.col("f_revenue").sum().alias("revenue"))
        .collect()
    )
    scan_s = time.monotonic() - t2
    seg: dict[str, object] = {}
    if segments:
        from app.columnar.profiles import profiles_lazy as _pl
        from app.features.contacts import columnar_funnel_profile
        from app.models.segments import fit_segments

        ts_ = time.monotonic()

        prof_df = columnar_funnel_profile(_pl(store, "acct"), 500_000)
        for col in ("c_rooms", "c_open_rooms", "c_purchase_intent_rooms", "c_reopens", "c_avg_sentiment", "c_last_room_at"):
            prof_df[col] = float("nan") if col == "c_last_room_at" else 0.0
        prof_df["in_funnels"], prof_df["in_conversations"] = True, False
        _, info = fit_segments(prof_df, 1_790_100_000, 7, 100, k_range=(3, 4))
        seg = {
            "segments_sample_rows": len(prof_df),
            "segments_seconds": round(time.monotonic() - ts_, 1),
            "segments_k": info.get("k"),
        }
    t3 = time.monotonic()  # incremental no-op refresh
    refresh_profiles(store, "acct")
    noop_s = time.monotonic() - t3
    out = {
        "events": events,
        "buckets": buckets,
        "workers": workers,
        "write_seconds": round(write_s, 1),
        "write_events_per_s": int(events / write_s),
        "refresh_seconds": round(refresh_s, 1),
        "refresh_events_per_s": int(rows / max(refresh_s, 1e-9)),
        "profile_scan_seconds": round(scan_s, 2),
        "noop_refresh_seconds": round(noop_s, 2),
        "contacts": int(prof["contacts"][0]),
        "events_in_profiles": int(prof["events"][0]),
        "peak_rss_main_mb": round(rss_mb(), 1),
        "rss_after_write_mb": round(rss_after_write, 1),
        "peak_rss_worker_mb": round(child_rss, 1),
        **seg,
        "disk_mb": round(store.disk_bytes("acct") / 1e6, 1),
        "machine": platform.machine(),
        "os": platform.platform(),
        "python": platform.python_version(),
    }
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=5_000_000)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--buckets", type=int, default=32)
    ap.add_argument("--chunk", type=int, default=2_000_000)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--segments", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.events, a.workers, a.buckets, a.chunk, a.keep, a.segments)))
