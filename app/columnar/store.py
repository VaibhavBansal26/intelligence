"""Hash-bucketed Parquet event store: the out-of-core path for accounts with 10M-1B events.

Layout: ``<root>/<account>/events/bucket=NN/part-<ts>-<n>.parquet``. Rows are routed by
``contact_h % buckets`` so every bucket is an independent unit of work (memory bound = one bucket;
fan-out unit for workers). Raw contact keys are not stored: ``contact_h`` is the first 64 bits of the
HMAC key (collision odds ~1e-5 at 20M contacts), which is all an aggregate needs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from pathlib import Path

import polars as pl

EVENT_SCHEMA = {
    "ts": pl.Int64,
    "type": pl.String,
    "funnel_id": pl.String,
    "step_id": pl.String,
    "contact_h": pl.UInt64,
    "amount": pl.Float32,
}


def contact_hash_expr(col: str = "contact_key") -> pl.Expr:
    """First 16 hex chars of a contact key -> u64 (null stays null)."""
    return pl.col(col).str.slice(0, 16).str.to_integer(base=16, dtype=pl.UInt64, strict=False)


class ParquetEventStore:
    def __init__(self, root: str | Path, buckets: int = 32) -> None:
        self.root = Path(root)
        self.buckets = buckets

    def events_dir(self, account_id: str, bucket: int) -> Path:
        return self.root / account_id / "events" / f"bucket={bucket:03d}"

    def profile_dir(self, account_id: str, bucket: int) -> Path:
        return self.root / account_id / "profiles" / f"bucket={bucket:03d}"

    def parts(self, account_id: str, bucket: int) -> list[Path]:
        d = self.events_dir(account_id, bucket)
        return sorted(d.glob("part-*.parquet")) if d.exists() else []

    def append(self, account_id: str, df: pl.DataFrame) -> int:
        """Append a chunk (columns of EVENT_SCHEMA; anonymous rows with null contact_h are skipped)."""
        df = df.filter(pl.col("contact_h").is_not_null())
        if df.is_empty():
            return 0
        df = df.select([pl.col(c).cast(t) for c, t in EVENT_SCHEMA.items()]).with_columns(
            (pl.col("contact_h") % self.buckets).cast(pl.UInt32).alias("_b")
        )
        stamp = f"{time.time_ns()}-{os.getpid()}"
        for (b,), part in df.partition_by("_b", as_dict=True, include_key=False).items():
            d = self.events_dir(account_id, int(b))
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".tmp-{stamp}.parquet"
            part.write_parquet(tmp, compression="zstd", compression_level=3, statistics=False)
            tmp.rename(d / f"part-{stamp}.parquet")  # atomic publish
        return df.height

    def compact(self, account_id: str, bucket: int) -> int:
        """Merge a bucket's parts into one file (bounded by the bucket size). Returns parts merged."""
        parts = self.parts(account_id, bucket)
        if len(parts) < 2:
            return 0
        merged = pl.concat([pl.scan_parquet(p) for p in parts]).sort("ts").collect()
        d = self.events_dir(account_id, bucket)
        stamp = f"{time.time_ns()}-{os.getpid()}"
        tmp = d / f".tmp-{stamp}.parquet"
        merged.write_parquet(tmp, compression="zstd", compression_level=3, statistics=False)
        tmp.rename(d / f"part-{stamp}.parquet")
        for p in parts:
            p.unlink()
        return len(parts)

    def disk_bytes(self, account_id: str) -> int:
        return sum(f.stat().st_size for f in (self.root / account_id).rglob("*.parquet"))

    def all_buckets(self) -> Iterable[int]:
        return range(self.buckets)
