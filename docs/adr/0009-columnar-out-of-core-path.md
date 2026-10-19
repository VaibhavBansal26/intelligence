# ADR 0009: Parquet-bucketed out-of-core path for very large accounts

Status: accepted (2026-10-19)

## Context
The target is 100M+ events per account with bounded memory. SQL landing tables are the system of record and are fine to tens of millions of rows, but pandas aggregation over all events does not fit in memory on a small node, and a 100M-row SQLite file is about 15 GB.

## Decision
Optionally tee every newly seen event chunk (`COLUMNAR_DIR`) into a Parquet store partitioned into `COLUMNAR_BUCKETS` hash buckets by contact key (`contact_h` = first 64 bits of the HMAC key). Contact profiles are refreshed per bucket from a manifest of unseen part files into mergeable partial aggregates (min/max/sum plus a deduplicated contact/step pair table for distinct counts) and rewritten atomically. A bucket is the unit of memory bounding and of worker fan-out (`profiles` jobs). Model training reads profiles through a deterministic hash-based down-sample capped at `ML_MAX_TRAINING_ROWS`. Polars does the chunked/columnar work; pandas stays for the modelling frames.

## Alternatives considered
ClickHouse or DuckDB as the analytic store (more moving parts, a new dependency class); streaming everything through SQL GROUP BY (single database becomes the bottleneck); pandas chunked groupby with manual merges (slower and more code than Polars lazy scans).

## Consequences
- Aggregates over 100M events run in seconds with about 0.5 GB RSS (see [SCALING.md](../SCALING.md)).
- The tee appends only events newer than the previous watermark, so a re-read window is not double counted; events that share the boundary second with the previous watermark are covered by SQL but may be missed by Parquet. Parquet is therefore an optimisation path, SQL remains authoritative.
- Raw contact keys are not stored in Parquet; a 64-bit prefix collides with probability about 1e-5 at 20M contacts, which is acceptable for aggregates.
- The point-in-time lead-feature builder still reads SQL (it needs per-lead history); only profile-level features (segments, volume statistics) use the columnar path. Making lead features out-of-core is future work.
