# ADR 0007: Idempotency keys and data-version ETags

Status: accepted (2026-10-19)

## Context
Sync and train triggers are retried by clients; ranking endpoints are heavy and change only when data or models change.

## Decision
POST endpoints that create things accept Idempotency-Key and store the response per (account, key) with a request hash (reuse with a different body is 409). Each account has a data_version bumped by sync, training, promotion and settings changes; heavy GETs derive their ETag from it plus the exact request and answer If-None-Match with 304 before computing.

## Alternatives considered
Server-side result caches (stale-data bugs); time-based ETags.

## Consequences
ETags change on every sync even if the visible result is identical. The anomalies and insights ETags also include the hour.
