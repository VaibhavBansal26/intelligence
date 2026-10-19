# Intelligence

A standalone Python service that consumes the product-analytics APIs of two independent Node apps, **Funnels** and
**Conversations**, and uses ML models plus an optional LLM to **rank** what matters and recommend actions, combining both
apps' data per customer account. It never writes back to the apps; the only coupling is the Analytics API contract
(`ANALYTICS-CONTRACT.md` v1, schemas in [contracts/](contracts/)).

What it ranks:
- **Funnels and steps** by expected revenue uplift if the weakest step reached the benchmark (empirical-Bayes shrinkage, bootstrap intervals, content-aware hints).
- **Leads** by probability of ordering within 14 days (gradient boosting, calibrated, per-prediction factors), using funnel and conversation signals when both apps are linked.
- **Open conversations** by SLA-breach risk blended with explicit rules (waiting time, sentiment, urgency, VIP).
- **Anomalies** (traffic drops, conversion collapses, backlog spikes) and **segments** of contacts.
- A short **action plan** and **Q&A** from an LLM that may only restate computed numbers (checked), with a deterministic offline fallback.

## Quick start (zero config)
```bash
uv sync
ENV=dev ADMIN_API_KEY=demo-admin ALLOW_PRIVATE_SOURCES=true uv run uvicorn app.main:app_factory --factory   # :8000, SQLite in ./data
# in another shell: fake Funnels (:9101) and Conversations (:9102) contract servers with synthetic data
uv run python -m app.synthetic.fake_sources --port 9101
# then run the whole flow with curl
ADMIN_API_KEY=demo-admin bash scripts/demo.sh
```
Open `http://127.0.0.1:8000/docs` for the interactive OpenAPI UI. No API key for the LLM is needed.

## Commands
| Command | Purpose |
|---|---|
| `make dev` | server with reload |
| `make test` | pytest with coverage |
| `make lint` / `make typecheck` | ruff / mypy |
| `make docs` | regenerate `docs/CONFIGURATION.md` and `docs/openapi.json` |
| `make docs-check` | fail on docs drift |
| `make bench` | 2M-event ingest benchmark |

## Configuration summary
Everything is an environment variable ([docs/CONFIGURATION.md](docs/CONFIGURATION.md), generated). In production `ENV=production`
requires `KEK` (Fernet key for secrets at rest) and `ADMIN_API_KEY`. `DATABASE_URL` defaults to SQLite; PostgreSQL via the
`postgres` extra. `ANTHROPIC_API_KEY` enables the LLM layer; `ALLOW_PRIVATE_SOURCES` (dev only) disables the SSRF guard.

## Scale
Accounts with very large event histories use an optional Parquet-bucketed out-of-core path (`COLUMNAR_DIR`): 100M events aggregated into contact profiles in about 11 s with a peak RSS of 638 MB on an 8 GB laptop (measured, synthetic; 300M also measured). Batch scoring, model hot swap, per-account budgets and a standalone worker (`python -m app.worker`) are included. Details, capacity model and what was *not* measured: [docs/SCALING.md](docs/SCALING.md).

## Documentation
Start at [docs/README.md](docs/README.md): architecture, approach, models, AI layer, API, testing, runbook, security, ADRs.

## Honesty note
All model metrics in the docs come from a seeded synthetic world with planted effects. They show the pipeline recovers
known signals; they do not predict real-world accuracy. See [docs/APPROACH.md](docs/APPROACH.md) for the data volumes real
accounts need.
