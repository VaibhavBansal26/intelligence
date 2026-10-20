# Runbook

## Deploy
```bash
docker build -t intelligence .
docker run -d --name intelligence -p 8000:8000 -v intelligence-data:/data \
  -e KEK=... -e ADMIN_API_KEY=... -e JWT_SECRET=... -e METRICS_TOKEN=... intelligence
curl -s localhost:8000/v1/ready
```
The image runs as a non-root user, applies Alembic migrations at start (`AUTO_MIGRATE=true`) and runs the worker (`WORKER_ENABLED=true`).
For PostgreSQL install the `postgres` extra and set `DATABASE_URL=postgresql+psycopg://...`. Run exactly one instance with the worker unless you accept per-instance rate limits (claims are atomic, so extra workers are safe but redundant). Docker was not available while this repository was built, so the image build is verified only by the CI definition.

First account:
```bash
curl -s -X POST localhost:8000/admin/accounts -H "X-Admin-Key: $ADMIN_API_KEY" -H 'content-type: application/json' -d '{"name":"Acme"}'
# returns the first admin API key once
```

## Rollback
- Code: redeploy the previous image. Migrations are additive in 0.x; check `alembic downgrade` before relying on it.
- Model: `POST /v1/models/types/{type}/rollback` (admin key) re-activates the previous archived version immediately; or `POST /v1/models/{version_id}/promote` for a specific one. Verify with `GET /v1/models`.

## Backups and restore
Back up the database and the model volume together. SQLite: `sqlite3 intelligence.db ".backup backup.db"` (WAL safe). PostgreSQL: `pg_dump`. Models can be retrained from landed data (`POST /v1/models/train`), landed data can be re-synced from the sources (`POST /v1/sync` with `{"full": true}`), but **source credentials and peppers live only in the database encrypted with the KEK: keep the KEK backed up separately**.

## Migrations
`AUTO_MIGRATE=true` runs Alembic `upgrade head` at start. To run it as a release step set `AUTO_MIGRATE=false` and call `python -c "from app.main import run_migrations; run_migrations('<DATABASE_URL>')"`.

## Monitoring and alerts to set
| Signal | Source | Alert |
|---|---|---|
| Readiness | `GET /v1/ready` | not ready for 2 min |
| Failed jobs | `intelligence_jobs_total{status="failed"}`, `GET /admin/jobs?status=failed` | any in 1 h |
| Sync freshness | `GET /v1/sync/status` (`last_run_at`) | older than 2x `SYNC_INTERVAL_S` |
| 5xx / latency | `intelligence_http_requests_total`, `intelligence_http_request_seconds` | 5xx ratio above 1% |
| Drift | `GET /v1/models/types/{type}/drift` | status `alert` |
| LLM spend | `/admin/accounts/{id}/usage` | calls near the daily cap |
Suggested SLOs (targets, not measurements): 99.5% of `/v1` GETs under 2 s excluding the first call after a sync; sync data no older than 2 sync intervals.

## Incident playbooks
**Sync failures.** `GET /v1/sync/status` shows `last_status`, `error`, `rows_total`, `bad_lines_total` per resource; the job result lists per-resource errors.
- `source rejected credentials (HTTP 401/403)`: the source API key was rotated or lacks `analytics:export`. `PATCH /v1/sources/{id}` with a new `api_key`, then sync.
- `ConnectorRateLimited`: the job is re-queued after `Retry-After`; if it persists lower `SYNC_INTERVAL_S` frequency or `CONNECTOR_PAGE_LIMIT` pressure.
- `ConnectorUnavailable`/timeouts: retried 5 times with jittered backoff; the resume cursor is kept, so the next sync continues where it stopped (`resumable_cursor: true`).
- `schema_version` older than supported: the source is too old; newer versions only warn.
- Many `bad_lines`: the source emits malformed NDJSON; above 5% a page fails with a schema error. Check the source's export.
- Stuck `running` jobs after a crash are re-queued at the next worker start (`recover_stuck`).

**Pepper mismatch / low join coverage.** `GET /v1/join` returns `warnings` (`pepper_fingerprint_mismatch`, `pepper_mismatch_suspected`, or `identity_bridge_active`) and coverage. Each source needs the pepper of its own app (Funnels: `GET /api/v1/analytics/identity-pepper` with a user session; Conversations: `GET /api/analytics/identity-pepper`). The real apps generate different peppers and cannot import one, so `joined_contacts: 0` is the normal state: send a contact list to `POST /v1/identity/bridge` ([APPROACH.md](APPROACH.md#combining-the-two-apps)). After rotating a pepper in an app, update the source (`PATCH /v1/sources/{id}`, which drops that source's bridge links) and re-submit the list. Conversations encrypts its pepper under its `JWT_SECRET`: if that secret changes, its exports answer 500 until the original secret is restored.

**A source key was revoked or expired.** `POST /v1/sync` returns `succeeded` with an `errors` entry per resource of that source (`... source rejected credentials (HTTP 401)`) and `GET /v1/sync/status` shows `last_status: error`; the account's other sources still sync. If every source is rejected the job fails with the same message. Create a new key in the app (`analytics:read` + `analytics:export`) and `PATCH /v1/sources/{id}` with `api_key`.

**Source rate limits (429).** The apps allow 10 export requests and 60 other requests per minute per key. The connector asks for large export pages (`CONNECTOR_EXPORT_PAGE_LIMIT`, default 5,000), reads the `RateLimit` headers and waits one refill interval when a bucket is empty instead of provoking a 429, and honours `Retry-After` up to `CONNECTOR_MAX_RETRY_AFTER_S` (120 s). A longer penalty (the apps escalate after repeated violations) fails that resource with its cursor kept (`resumable_cursor: true`); the next sync resumes. A first backfill of 13,000 events took 3 export pages; a sync with a pre-exhausted key waited about 38 s and landed everything instead of failing.

**Model drift.** `GET /v1/models/types/lead_conversion/drift?recent_days=14` lists per-feature PSI. `warn` at 0.1, `alert` at 0.25. Retrain (`POST /v1/models/train`); the new version is auto-promoted unless its validation AUC is more than 0.02 below the active one. If a retrain makes things worse, roll back.

**Cold start / "heuristic" responses.** Not an incident: the account has too little labelled data. The response field `method` is `heuristic` and `cold_start_reason` says what is missing. See the volume table in [APPROACH.md](APPROACH.md).

**Model artifact integrity error (409 `ARTIFACT_INTEGRITY`).** The file under `MODEL_DIR` no longer matches the recorded sha256 (disk corruption or tampering). Roll back to another version or retrain; investigate who can write to the volume.

**Secret rotation.** KEK: prepend a new key to `KEK` (`new,old`), restart; stored secrets still decrypt, and are re-encrypted with the new key when a source is next updated; remove the old key after updating every source. Admin key / JWT secret / metrics token: change and restart (JWTs signed with the old secret stop working). Tenant API keys: create a new key, switch clients, `DELETE /v1/api-keys/{id}`.

**LLM provider outage or runaway cost.** Nothing to do for outages: every feature falls back to templates (`fallback_reason: timeout|llm_error`). For cost, lower `LLM_DAILY_CALL_CAP` or unset `ANTHROPIC_API_KEY` and restart.

**Database down.** `/v1/ready` returns 503; requests fail with 500. Restore the database (see Backups); jobs resume automatically.

**Data retention / deletion request.** `DELETE /v1/sources/{id}` removes the source and every landed row from it. There is no automatic expiry; schedule deletion of old `events`/`messages_meta` in the database if required.

## Capacity incidents (see [SCALING.md](SCALING.md))
**Queue backlog.** `GET /admin/jobs?status=queued` grows. Start more workers (`python -m app.worker --concurrency 4`, `WORKER_ENABLED=false` on API pods). One account hogging the pool is capped by `ACCOUNT_MAX_CONCURRENT_JOBS`; the queue always serves the account with the fewest running jobs first. If jobs are stuck `running` after a crash, restart a worker (they are re-queued after one hour; adjust with `recover_stuck`).

**Account over budget.** A sync reports `account event budget reached (N rows)` in the job `result.errors`; other resources still sync. Raise `ACCOUNT_MAX_EVENT_ROWS`, archive the account's old events, or accept the cap. Training uses at most `ML_MAX_TRAINING_ROWS` most-recent rows by design.

**Parquet store problems.** Profiles are derived data: delete `<COLUMNAR_DIR>/<account>/profiles` to force a full rebuild at the next refresh (events parts are kept). A crashed writer leaves only `.tmp-*` files (safe to delete); published parts and profiles are written atomically. Never refresh one bucket from two workers at once (use the disjoint `shards` fan-out). Memory growth during refresh means buckets are too large: rebuild the store with more `COLUMNAR_BUCKETS` (about events / 3M).

**Slow scoring.** Single-row calls cost about 15 ms fixed; use `POST /v1/leads/score/batch`. If the model keeps reloading, `model_cache_size` is smaller than the number of active models in use.
