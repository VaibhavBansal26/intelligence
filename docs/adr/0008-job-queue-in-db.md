# ADR 0008: DB-backed job queue behind an interface

Status: accepted (2026-10-19)

## Context
Sync and training can take minutes; running a broker is operational weight for a first deployment.

## Decision
A jobs table with atomic claim (UPDATE ... WHERE status='queued'), retries with delay for retryable connector errors, stuck-job recovery, and an asyncio runner started in the app lifespan. The JobQueue protocol is the seam for Celery/Redis.

## Alternatives considered
Celery + Redis from day one; APScheduler (no durability).

## Consequences
One logical worker per database is expected; Celery adapter is deferred, not built. The periodic scheduler is a simple loop (SYNC_INTERVAL_S).
