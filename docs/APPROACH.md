# Approach, decisions and trade-offs

Decisions with lasting consequences also have an ADR in [adr/](adr/).

## Problem
Two independent products emit product analytics. A customer wants to know what to fix first (funnels), which
leads and conversations deserve attention, and when something broke, combining both apps per customer account.
The service ranks and recommends; it does not mutate the source apps.

## Key decisions
| Decision | Why | Trade-off |
|---|---|---|
| Land raw data locally, then compute ([ADR 0002](adr/0002-land-then-compute.md)) | Models need history and joins across apps; source APIs are rate limited | Storage cost; data is up to one sync interval stale |
| Default model is sklearn HistGradientBoosting; LightGBM is an optional backend | Light install, native NaN and categorical support, deterministic with a seed | LightGBM may be a little faster on very large tables |
| Empirical-Bayes shrinkage for step ranking ([ADR 0004](adr/0004-empirical-bayes-ranking.md)) | A 0/4 step must not outrank a well measured weak step | Small real differences between low-traffic steps are hidden by design |
| Point-in-time features and right-censoring guards ([ADR 0005](adr/0005-leakage-guards.md)) | The classic way these models lie is leaking the future | Fewer, weaker features at opt-in time (honest AUC about 0.76 on synthetic data, not 0.95) |
| Own seasonal-median + ESD + CUSUM detector instead of statsmodels/ruptures | No heavy dependency, easy to reason about false positives | Not a general forecaster; assumes regular, seasonal, count-like series |
| LLM may only restate numbers from evidence ([ADR 0006](adr/0006-numberguard.md)) | Hallucinated figures in a business plan are worse than a dull template | Valid but creatively derived numbers (for example a ratio the model computed) are rejected |
| Sync SQLAlchemy in threadpool, async only for connectors | Simple and debuggable; DB writes in `asyncio.to_thread` | Not maximal throughput |
| DB-backed job table and asyncio worker | No broker to run for a first deployment | Single-node scheduling; Celery adapter deferred |
| API keys for machines, optional JWT exchange | Keys are revocable and hashed; JWT only for short sessions | No user/password login: this is a service API, not a UI |
| Pickle (joblib) artifacts with sha256 in the DB | Simple and fast | Artifacts are trusted code: the model volume must not be writable by anyone else |

## Combining the two apps
Contacts are linked by `contact_key = HMAC-SHA256(pepper, lower(trim(email)))`, produced by each app with the
same per-workspace pepper. The service stores the pepper encrypted only to (a) compare fingerprints between
sources and (b) hash an email supplied in `POST /v1/leads/score`. The join report gives coverage and warns on
pepper-fingerprint differences or near-zero overlap. Single-source accounts work: the missing block is NaN (all-NaN
columns are dropped at training time and recorded in the model metrics).

## Privacy
- No raw email, phone or name is ever requested or stored; only HMAC keys, opaque ids and aggregate counts.
- An email passed to `POST /v1/leads/score` is hashed in memory and never persisted or logged.
- Source API keys and peppers are Fernet-encrypted with a KEK from the environment and never returned or logged
  (the JSON logger redacts fields named key/secret/token/password/pepper).
- LLM prompts contain aggregates and sanitised funnel/step names, never contact keys; emails, phone numbers and
  long hex identifiers are scrubbed again at the gateway (`LLM_REDACT_PII`).
- Retention: landed rows are kept until the source is deleted (`DELETE /v1/sources/{id}` removes its rows). There is
  no automatic expiry yet; set one in the database if your policy needs it ([RUNBOOK](RUNBOOK.md)).

## What real data volume is needed
These are rules of thumb, not guarantees. Metrics on synthetic data prove the pipeline recovers planted signals;
they say nothing about real-world accuracy.
- Step ranking: a few hundred unique visitors per step before a rate is trusted (`confidence` is `low` below 100, `high` from 500).
- Lead model: at least 300 mature labelled leads with 30+ of each class (hard minimum in code), realistically 2,000+
  mature leads and 100+ conversions for stable lift; the label needs 14 days to mature.
- SLA model: 300+ labelled rooms, realistically 2,000+.
- Anomalies: at least 4 weeks of daily history (hard minimum 28 points); better with 8+ weeks.
- Segments: 100+ contacts (rules below that), 1,000+ for meaningful clusters.

## Known limitations
See [MODELS.md](MODELS.md) per model, plus: the contract does not say how `ai_sentiment`/`ai_urgency` are encoded (the
service accepts words or numbers), nor the exact shape of `/export/funnel-structure` (list, envelope and NDJSON are all accepted).
