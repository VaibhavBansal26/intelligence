from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.columnar.profiles import profiles_lazy, refresh_profiles
from app.columnar.store import ParquetEventStore, contact_hash_expr
from tests.conftest import build_env


def events(n: int, seed: int, n_contacts: int = 50, t0: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    typ = rng.choice(["page_view", "optin", "order"], n, p=[0.7, 0.2, 0.1])
    return pl.DataFrame(
        {
            "ts": t0 + rng.integers(0, 1000, n),
            "type": typ,
            "funnel_id": rng.choice(["f0", "f1"], n),
            "step_id": rng.choice(["a", "b", "c", ""], n),
            "contact_h": rng.integers(1, n_contacts + 1, n).astype(np.uint64),
            "amount": np.where(typ == "order", rng.integers(10, 100, n), 0).astype(np.float32),
        }
    ).with_columns(pl.when(pl.col("step_id") == "").then(None).otherwise(pl.col("step_id")).alias("step_id"))


def expected(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.group_by("contact_h")
        .agg(
            pl.col("ts").min().alias("f_first_seen"),
            pl.col("ts").max().alias("f_last_seen"),
            pl.len().alias("f_events"),
            (pl.col("type") == "order").sum().alias("f_orders"),
            pl.when(pl.col("type") == "order").then(pl.col("amount")).otherwise(0.0).sum().alias("f_revenue"),
            pl.concat_str(["funnel_id", "step_id"], separator="|").drop_nulls().n_unique().alias("steps"),
        )
        .sort("contact_h")
    )


def test_append_routes_by_bucket_skips_anonymous_and_leaves_no_temp_files(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path, buckets=4)
    df = events(100, 1).with_columns(pl.when(pl.col("ts") % 10 == 0).then(None).otherwise(pl.col("contact_h")).alias("contact_h"))
    kept = store.append("a", df)
    assert kept == df.filter(pl.col("contact_h").is_not_null()).height
    for b in range(4):
        for p in store.parts("a", b):
            assert (pl.read_parquet(p)["contact_h"] % 4 == b).all()
    assert not list(tmp_path.rglob(".tmp-*"))


@settings(max_examples=15, deadline=None)
@given(st.integers(1, 3), st.integers(0, 5))
def test_refresh_equals_in_memory_groupby_across_incremental_appends(n_batches: int, seed: int) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = ParquetEventStore(d, buckets=4)
        frames = []
        for i in range(n_batches):
            df = events(300, seed * 10 + i, t0=i * 1000)
            frames.append(df)
            store.append("a", df)
            refresh_profiles(store, "a")  # refresh after every batch: incremental merging
        got = profiles_lazy(store, "a").sort("contact_h").collect()
        exp = expected(pl.concat(frames))
        assert got["contact_h"].to_list() == exp["contact_h"].to_list()
        for g, e in (
            ("f_events", "f_events"),
            ("f_orders", "f_orders"),
            ("f_first_seen", "f_first_seen"),
            ("f_last_seen", "f_last_seen"),
        ):
            assert got[g].to_list() == exp[e].to_list()
        assert np.allclose(got["f_revenue"].to_numpy(), exp["f_revenue"].to_numpy())
        assert got["f_steps_reached"].to_list() == exp["steps"].to_list()


def test_incremental_refresh_reads_only_new_parts_and_is_idempotent(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path, buckets=4)
    store.append("a", events(1000, 1))
    s1 = refresh_profiles(store, "a")
    assert s1.rows_read > 0 and s1.parts_read >= 1
    assert refresh_profiles(store, "a").parts_read == 0  # nothing new: no-op
    store.append("a", events(10, 2, t0=5000))
    s3 = refresh_profiles(store, "a")
    assert 0 < s3.rows_read <= 10 and s3.buckets_refreshed >= 1


def test_bucket_fanout_matches_single_worker_and_compaction_preserves_result(tmp_path: Path) -> None:
    df = events(5000, 3, n_contacts=400)
    a, b = ParquetEventStore(tmp_path / "a", 8), ParquetEventStore(tmp_path / "b", 8)
    for store in (a, b):
        for chunk in df.iter_slices(1000):
            store.append("x", chunk)
    refresh_profiles(a, "x")
    for shard in ([0, 2, 4, 6], [1, 3, 5, 7]):  # two "workers" with disjoint bucket sets
        refresh_profiles(b, "x", shard)
    assert profiles_lazy(a, "x").sort("contact_h").collect().equals(profiles_lazy(b, "x").sort("contact_h").collect())
    assert sum(b.compact("x", i) for i in range(8)) > 8
    assert all(len(b.parts("x", i)) <= 1 for i in range(8))


def test_contact_hash_is_stable_and_null_safe() -> None:
    df = pl.DataFrame({"contact_key": ["00000000000000ff" + "a" * 48, None, "zz"]})
    h = df.select(contact_hash_expr())["contact_key"].to_list()
    assert h[0] == 255 and h[1] is None and h[2] is None
    big = pl.DataFrame({"contact_key": ["ffffffffffffffff" + "0" * 48]}).select(contact_hash_expr())["contact_key"][0]
    assert big == 2**64 - 1  # keys with the top bit set must not overflow to null


@pytest.fixture(scope="module")
def tee_env(small_world, tmp_path_factory):  # type: ignore[no-untyped-def]
    d = tmp_path_factory.mktemp("tee")
    yield from ((e, d) for e in build_env(d, small_world, train=False, columnar_dir=str(d / "col"), columnar_buckets=8))


def test_sync_tees_events_into_parquet_without_double_counting(tee_env) -> None:  # type: ignore[no-untyped-def]
    env, d = tee_env
    store = ParquetEventStore(d / "col", 8)
    refresh_profiles(store, env.account_id)
    prof = profiles_lazy(store, env.account_id).select(pl.col("f_events").sum()).collect().item()
    with_contact = sum(1 for e in env.world.events if e["contact_key"])
    assert prof == with_contact
    env.client.post("/v1/sync?wait=true", json={}, headers=env.h)  # re-sync: watermark window re-read must not duplicate
    refresh_profiles(store, env.account_id)
    again = profiles_lazy(store, env.account_id).select(pl.col("f_events").sum()).collect().item()
    assert again - prof <= 5  # at most boundary-second ties


def test_admin_fanout_jobs_refresh_profiles(tee_env) -> None:  # type: ignore[no-untyped-def]
    env, _ = tee_env
    r = env.client.post(f"/admin/accounts/{env.account_id}/refresh-profiles?shards=3", headers=env.admin)
    assert r.status_code == 202 and r.json()["shards"] == 3
    jobs = env.client.get("/admin/jobs?status=queued", headers=env.admin).json()
    assert sum(1 for j in jobs if j["type"] == "profiles") == 3
    assert env.client.post("/admin/accounts/nope/refresh-profiles", headers=env.admin).status_code == 404
