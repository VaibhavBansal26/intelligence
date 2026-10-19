"""Incremental, bucket-parallel contact-profile refresh over the Parquet event store.

Each bucket keeps a mergeable partial profile (min/max/sum aggregates) plus a deduplicated
(contact, funnel|step) pair table for distinct counts. A refresh only reads parts that were not seen
before (manifest), merges them into the stored partial, and rewrites that bucket atomically. Memory is
bounded by one bucket; buckets can be refreshed by different workers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from app.columnar.store import ParquetEventStore

AGG_COLS = ["f_first_seen", "f_last_seen", "f_events", "f_page_views", "f_optins", "f_orders", "f_revenue"]


@dataclass
class RefreshStats:
    buckets_refreshed: int = 0
    buckets_skipped: int = 0
    parts_read: int = 0
    rows_read: int = 0
    contacts: int = 0
    refreshed: list[int] = field(default_factory=list)


def _manifest_path(store: ParquetEventStore, account_id: str, bucket: int) -> Path:
    return store.profile_dir(account_id, bucket) / "manifest.json"


def _atomic_write(df: pl.DataFrame, path: Path) -> None:
    tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
    df.write_parquet(tmp, compression="zstd", statistics=False)
    tmp.rename(path)


def refresh_bucket(store: ParquetEventStore, account_id: str, bucket: int) -> tuple[int, int, int]:
    """Returns (parts_read, rows_read, contacts_in_bucket). Idempotent: unchanged buckets are no-ops."""
    pdir = store.profile_dir(account_id, bucket)
    pdir.mkdir(parents=True, exist_ok=True)
    mpath = _manifest_path(store, account_id, bucket)
    seen: set[str] = set(json.loads(mpath.read_text())["parts"]) if mpath.exists() else set()
    new = [p for p in store.parts(account_id, bucket) if p.name not in seen]
    if not new:
        return 0, 0, 0
    lf = pl.concat([pl.scan_parquet(p) for p in new])
    partial = (
        lf.group_by("contact_h")
        .agg(
            pl.col("ts").min().alias("f_first_seen"),
            pl.col("ts").max().alias("f_last_seen"),
            pl.len().cast(pl.Int64).alias("f_events"),
            (pl.col("type") == "page_view").sum().cast(pl.Int64).alias("f_page_views"),
            (pl.col("type") == "optin").sum().cast(pl.Int64).alias("f_optins"),
            (pl.col("type") == "order").sum().cast(pl.Int64).alias("f_orders"),
            pl.when(pl.col("type") == "order").then(pl.col("amount")).otherwise(0.0).sum().cast(pl.Float64).alias("f_revenue"),
        )
        .collect()
    )
    new_pairs = (
        lf.filter(pl.col("step_id").is_not_null())
        .select("contact_h", pl.concat_str(["funnel_id", "step_id"], separator="|").hash().cast(pl.UInt64).alias("step_h"))
        .unique()
        .collect()
    )
    ppath, pairpath = pdir / "profile.parquet", pdir / "pairs.parquet"
    if ppath.exists():
        old = pl.read_parquet(ppath).drop("f_steps_reached")
        partial = (
            pl.concat([old, partial])
            .group_by("contact_h")
            .agg(
                pl.col("f_first_seen").min(),
                pl.col("f_last_seen").max(),
                pl.col("f_events").sum(),
                pl.col("f_page_views").sum(),
                pl.col("f_optins").sum(),
                pl.col("f_orders").sum(),
                pl.col("f_revenue").sum(),
            )
        )
        new_pairs = pl.concat([pl.read_parquet(pairpath), new_pairs]).unique()
    steps = (
        new_pairs.group_by("contact_h")
        .len()
        .rename({"len": "f_steps_reached"})
        .with_columns(pl.col("f_steps_reached").cast(pl.Int64))
    )
    profile = partial.join(steps, on="contact_h", how="left").with_columns(pl.col("f_steps_reached").fill_null(0))
    _atomic_write(new_pairs, pairpath)
    _atomic_write(profile, ppath)
    rows = int(lf.select(pl.len()).collect().item())
    mpath.write_text(json.dumps({"parts": sorted(seen | {p.name for p in new})}))
    return len(new), rows, profile.height


def refresh_profiles(store: ParquetEventStore, account_id: str, buckets: range | list[int] | None = None) -> RefreshStats:
    stats = RefreshStats()
    for b in buckets if buckets is not None else store.all_buckets():
        parts, rows, contacts = refresh_bucket(store, account_id, b)
        if parts:
            stats.buckets_refreshed += 1
            stats.parts_read += parts
            stats.rows_read += rows
            stats.contacts += contacts
            stats.refreshed.append(b)
        else:
            stats.buckets_skipped += 1
    return stats


def profiles_lazy(store: ParquetEventStore, account_id: str) -> pl.LazyFrame:
    """All bucket profiles as one lazy frame (never materialised unless the caller collects it)."""
    files = sorted((store.root / account_id / "profiles").glob("bucket=*/profile.parquet"))
    if not files:
        return pl.DataFrame(
            schema={"contact_h": pl.UInt64, **dict.fromkeys(AGG_COLS, pl.Float64), "f_steps_reached": pl.Int64}
        ).lazy()
    return pl.scan_parquet(files)
