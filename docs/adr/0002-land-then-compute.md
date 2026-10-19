# ADR 0002: Land source data locally, then compute

Status: accepted (2026-10-19)

## Context
Models need history and cross-app joins; source APIs are rate limited (60 req/min, exports 10 req/min) and return pages, not query access.

## Decision
Sync NDJSON exports and rollups into local landing tables (events, leads, rooms, messages_meta, rollups, funnel snapshots) idempotently with watermarks, then build features with SQL aggregation and pandas.

## Alternatives considered
Query the source APIs on demand (slow, rate limited, no joins); push webhooks only (the contract makes them optional).

## Consequences
Data is as fresh as the last sync. Storage grows with source volume; chunked streaming keeps memory flat (2M events: 43 s, about 108 MB peak RSS). Re-sync and crash recovery must be idempotent, which drove insert-or-ignore/upsert keys and persisted resume cursors.
