# Testing

## Strategy
Unit tests never use the network. The two Node apps are replaced by **fake implementations of the contract**
(`app/synthetic/fake_sources.py`, FastAPI apps serving seeded synthetic NDJSON with the same shared pepper, pagination cursors and
fault injection), mounted through `httpx.ASGITransport`. The LLM is a scriptable fake (`tests/fakes.py`). Time is an injected clock fixed at the end of
the synthetic world. Because the world has **planted ground truth**, ML tests assert recovery (the weak step ranks first, high-intent
features top the permutation importance, the injected anomaly day is found with few false alerts) rather than only shape.

| Layer | Where | What it proves |
|---|---|---|
| Unit and property (hypothesis) | `tests/unit`, `tests/ml/test_anomaly.py`, `tests/scale/test_columnar.py` | Wilson, lift-by-decile, PSI, Beta prior, NumberGuard, sanitiser; detector never crashes and is ordered; Parquet refresh equals an in-memory groupby over random incremental appends |
| Contract | `tests/contract` | Synthetic fixtures and the fake apps satisfy the JSON Schemas in `contracts/`; consumer models ignore unknown fields; numeric AI-field encodings accepted |
| Connectors (respx) | `tests/connectors` | pagination by `X-Next-Cursor`, cursor resume, 429 with `Retry-After`, 5xx/timeouts with jittered backoff, malformed lines, truncated stream, stalled cursor, schema drift, auth errors not retried |
| Ingest | `tests/ingest` | rows landed once, idempotent and incremental re-sync, watermarks, mutable-resource lookback, resume after interruption, truncated/malformed pages, bad credentials, `schema_version`, secrets encrypted, pepper mismatch, single-source accounts |
| ML | `tests/ml` | shrinkage (tiny never outranks large), reproducibility by seed, time-ordered split + embargo, calibration, cold-start heuristics, point-in-time features and censoring, drift, registry promote/rollback/integrity, auto-promotion |
| LLM | `tests/llm`, `tests/e2e/test_llm_via_api.py` | valid output accepted; hallucinated numbers, malformed JSON, empty, refusal, timeout, 5xx, unknown ids, injection strings in names, cache, daily cap, input budget, PII redaction, tool loop, step limit, unknown tools, re-rank permutation validation |
| API | `tests/api` | 401 on every `/v1` route, role matrix, scoped keys, JWT, **cross-account 404** on every id-bearing route, error envelope, validation, rate limit, idempotency, ETag, pagination, SSRF via API, admin plane, responses validated against the generated OpenAPI schemas |
| End to end | `tests/e2e` | admin creates account -> sources registered -> sync -> train -> rankings -> insights -> ask, all through HTTP against the fake apps |
| Scale | `tests/scale`, `tests/perf` | Parquet store and bucket fan-out, incremental refresh, batch scoring, >5000 scores/s, model cache hot swap, per-account budgets and fair queue, concurrent workers never double-claim, 2M-event bounded-memory ingest (`-m slow`) |
| Real-app recordings | `tests/connectors/test_real_apps.py`, `tests/fixtures/real/` | responses recorded from the real Funnels and Conversations apps: record models on real shapes (null AI fields, millisecond timestamps, `exit_rate: null`, `{data,next_cursor}` structure), the real 429 and its headers, and sync, training and every endpoint on the recorded exports |
| Integration | `scripts/integration/run.sh` ([INTEGRATION.md](INTEGRATION.md)) | not part of `pytest`: the real apps and this service together, about 106 checks, about 5.5 minutes |
| Docs | `tests/test_docs.py` | relative links and anchors, index completeness, config and env parity, OpenAPI and CONFIGURATION freshness, every route documented, Mermaid structure and real parser, CHANGELOG `Unreleased` |
| Migrations | `tests/test_migrations.py` | Alembic head equals the SQLAlchemy models |

The fake apps could not find what the real ones did (null `ai_intent`, NaN features, a revoked key aborting a sync, pages the rate limiter makes expensive, apps with different peppers), so [INTEGRATION.md](INTEGRATION.md) runs the real apps and
`tests/fixtures/real/` keeps what they answered. Re-record the fixtures with `scripts/integration/run.sh --record` when an app's responses change.

## Current results (2026-10-20)
277 tests pass in the default suite (1 deselected `slow` test: the 2M-event memory proof, last run 2026-10-19: 200k rows peak 109.5 MB, 2M rows peak 117.2 MB; not re-run). Line coverage of `app/` is **93%** (5,441 statements); lowest are
the CLI entry points (`app/worker.py`, the docs/OpenAPI generator mains), covered indirectly by the docs tests. `ruff check`, `ruff format --check` and `mypy app` (65 files) are clean. The integration run ([INTEGRATION.md](INTEGRATION.md)), 106 checks, passed against the real apps on 2026-10-20 (5 min 30 s to 6 min 20 s on an Apple M-series laptop under load).
Mypy runs on `app/` without pandas stubs (they produced hundreds of unactionable errors); strictness is `disallow_untyped_defs` plus `check_untyped_defs`.

## Running
```bash
uv run pytest                              # default suite
uv run pytest --cov=app --cov-report=term-missing
uv run pytest -m slow -s tests/perf        # 2M-row ingest memory proof
npm ci --prefix tools/mermaid_check        # once, enables the real Mermaid parser test
make docs-check
uv run python -m benchmarks.columnar_bench --events 100000000 --segments
```
The default suite takes about 4 minutes on an 8-core laptop when the machine is idle (much longer under load: the shared environment this was
built on had a load average above 30). Most time goes to building environments (sync of the synthetic world) and GBM training.

## Edge-case catalogue
Duplicate contact keys, pepper mismatch and near-zero join coverage, single-source accounts (all-NaN features dropped), immature labels,
converted-but-undated leads, unanswered rooms inside and past the SLA, negative revenue (refunds), keys with the top bit set in the Parquet hash,
re-read windows that must not double count, truncated final NDJSON line, cursor that never advances, `Retry-After` larger than the cap, schema
version newer/older than supported, injection strings in funnel names, LLM that obeys the injection, expired and forged JWTs, role escalation by
altering JWT permissions, idempotency key reuse with a different body, tampered model artifact.

## What is not covered
No real PostgreSQL run (no database or Docker was available), no real Anthropic call, no real Node apps (contract tests only), no browser or
load test beyond the benchmarks in [PERFORMANCE.md](PERFORMANCE.md) and [SCALING.md](SCALING.md). The Docker image was not built here.
The CI file is untested because it was never run on GitHub.
