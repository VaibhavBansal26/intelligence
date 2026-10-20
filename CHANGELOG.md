# Changelog

All notable changes are recorded here ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/), semantic versioning).

## [Unreleased]
### Added
- Out-of-core path: hash-bucketed Parquet event store, incremental per-bucket contact-profile refresh, `profiles` fan-out jobs, standalone worker (`python -m app.worker`).
- `POST /v1/leads/score/batch`, model LRU cache with live hot swap on promote/rollback, per-data-version feature-frame cache.
- Per-account budgets: concurrent jobs with fair scheduling, `ACCOUNT_MAX_EVENT_ROWS`, `ML_MAX_TRAINING_ROWS`.
- `docs/SCALING.md`, `docs/PERFORMANCE.md`, ADR 0009; benchmarks for 100M/300M events, scoring and training.

### Fixed
- Training and `/v1/leads/scores`, `/v1/segments`, `/v1/insights` crashed on real data: rooms the apps' AI triage did not label have a null `ai_intent` (pandas `<NA>` in an integer cast).
- `/v1/rankings/conversations/open` answered 500 when a feature was NaN (not valid JSON); NaN and infinities are now `null` in every `/v1` JSON response.

## [0.1.0] - 2026-10-19
### Added
- Multi-tenant FastAPI service: accounts, hashed scoped API keys, optional JWT, RBAC (`admin|analyst|viewer`), operator `/admin` API.
- Connectors for the Funnels and Conversations analytics contract (retries with jitter, 429 handling, NDJSON resume, schema tolerance, SSRF guard) and idempotent incremental sync with a DB-backed job queue.
- Cross-app contact profiles joined by shared-pepper HMAC keys, with join coverage and pepper-mismatch diagnostics.
- Models: funnel/step ranking (empirical Bayes + bootstrap), lead conversion GBM, SLA breach GBM with business-rule conversation prioritisation, seasonal anomaly detection, segment discovery; versioned registry with promote, rollback and PSI drift checks.
- LLM layer: grounded narratives (NumberGuard), tool-use Q&A, optional re-ranking, cost controls, deterministic offline fallback.
- Synthetic world generator with planted ground truth and fake contract apps; unit, property, contract, API, ML-recovery and end-to-end tests; 2M-event bounded-memory benchmark.
- Docs set, generated configuration reference and OpenAPI export with drift checks, ADRs, CI workflow, Dockerfile.
