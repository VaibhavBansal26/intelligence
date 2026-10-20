# Integration run against the real apps

`scripts/integration/run.sh` starts the real Funnels app, the real Conversations app and this service, drives the apps through their HTTP APIs, runs the service against them and asserts the result. Everything it starts is stopped and every temp file removed when it ends (also on failure or Ctrl-C). What it found: [INTEGRATION-FINDINGS.md](INTEGRATION-FINDINGS.md).

## Run it
```bash
scripts/integration/run.sh                 # about 6 minutes on an Apple M-series laptop (2026-10-20); exit 0 = all checks passed
scripts/integration/run.sh --record        # also refresh tests/fixtures/real/ from the live responses
scripts/integration/run.sh --days 21 --scale 0.5     # smaller history (faster, but the lead model stays in cold start)
scripts/integration/run.sh --keep-workdir  # keep the temp dir (logs, SQLite files, verify-report.json) and print its path
scripts/integration/run.sh --reuse DIR     # skip seeding and traffic, reuse a kept workdir (iterate on verify.py)
scripts/integration/run.sh --reuse DIR --hold   # start everything and wait, to poke at the three services by hand
```
Needs Node >= 22, `uv`, `curl` and the three repos side by side (`repos/funnels`, `repos/conversations`, `repos/intelligence`; override with `FUNNELS_DIR`, `CONVERSATIONS_DIR`). If a Node repo has no `server/node_modules`, `npm ci --prefix server` runs once. Nothing else is installed or touched: no Redis, no PostgreSQL (`/Library/PostgreSQL/17` and the Redis on 6379 are never used), SQLite files live in the temp dir, ports are random free ports on 127.0.0.1.

## What it does
| Step | Details |
|---|---|
| 1. Seed | `npm run seed` of each app (both crashed or hung until the fixes N1/N2, see the findings). Demo user `demo@example.test` of each app |
| 2. Phase A | Both apps start with `fakeclock.mjs` (a Node preload that shifts `Date` by an offset read from a file), `RATELIMIT_ENABLED=false` / `RATE_LIMIT_SCALE=1000` for the public paths, `EVENT_BUFFER_MS=0`, a long access-token TTL and a fixed throw-away `JWT_SECRET` |
| 3. Traffic | `generate_traffic.py` (below), then both apps are stopped |
| 4. Phase B | Both apps restart on the same SQLite files with the real clock and **default** rate limits. `verify.py` creates analytics API keys (`POST /api/v1/api-keys`) and reads each app's own random pepper (`GET /api/v1/analytics/identity-pepper`, session) |
| 5. Service | `uvicorn app.main:app_factory` on a temp SQLite, `ALLOW_PRIVATE_SOURCES=true`, no LLM key |
| 6. Verify | `verify.py`: conformance, sync, models, rankings, identity (own peppers + bridge), failure modes, rate limits, then the shared-pepper scenario (table below). Writes `verify-report.json` into the workdir |
| 7. Clean up | TERM then KILL for every process started, `rm -rf` of the workdir (unless `--keep-workdir` / `--reuse`) |

## Traffic (`generate_traffic.py`)
Only HTTP, never the databases. Funnels pages are GrapesJS project documents (`doc_json = {version: 2, project, forms, products}`); the generator never edits them: it reads the server-derived `forms` and `products` of each published step (`GET /api/funnels/:id/steps/:sid/page`) and posts to the public form and checkout endpoints with those ids, and creates the extra funnels from templates (`POST /api/templates/:id/use`). The same 900 people (e-mail addresses, some typed with capitals or padding to test normalisation) appear in both apps.
- **Funnels** (public API as a browser calls it): `POST /api/public/events`, `/api/public/forms/...`, `/api/public/checkout/...` with UTMs, referrers and user agents. 45 days (`--days`) of sessions over 7 funnels (the app's lead magnet, sales page with bump and upsell, webinar, bakery booking funnel, plus product launch, newsletter signup and book-a-call created from templates through the REST API and published on the demo domain), about 110 sessions a day, source-dependent opt-in rates, a latent buying propensity per person (source, device), returning leads who buy days later, order bumps and one-click upsells, and a planted collapse of one funnel's traffic for the last 3 days.
- **Conversations**: widget sessions with `x-api-key` embed token and `x-device-id`, rooms and messages for six kinds of text (complaint, urgent, support, purchase, question, thanks), the app's own AI triage switched on through `PATCH /api/workspace`, agents assigning, replying (chat and e-mail), prioritising, snoozing, closing; visitors answering CSAT and reopening. About 24 rooms a day, 65% of them from people who are also funnel leads; slow replies planted for the last days.
- **Time**: the apps' public APIs stamp "now", so history needs a clock. `fakeclock.mjs` is a `--import` preload for the app's Node process (nothing in the app repos changes); the generator rewrites an offset file before every request, in one globally time-ordered schedule per app, so each clock only moves forward. At the end the offset is reset. This is why phase A exists.

## What `verify.py` asserts (137 checks; 106 before the page builder and importable pepper)
| Area | Checks |
|---|---|
| Keys | creates read+export keys; read-only, revoked, expiring (3 s), other-workspace keys; reads both peppers (session) and checks them against the apps' `identity-pepper.schema.json`; an API key with `analytics:export` + `analytics_identity:manage` reads the same pepper in both apps, a key without the scope gets 403, no key can `PUT` it |
| Conformance | real responses of every endpoint against the Node repos' `docs/analytics-api/*.json` **and** this repo's `contracts/*.json`; deviations are printed as `[DEVIATION:owner]` and written to the report |
| NDJSON | uniqueness, `(ts,id)` ordering across pages, `X-Next-Cursor` / `X-Row-Count`, replayable cursor, gzip, 700 / 5,000 / 20,000 row pages give identical rows, `/overview` rooms equal exported rooms |
| Failure modes of the apps | revoked / expired / malformed key 401 with the contract body, read-only key 403 on exports, user token rejected, other tenant's funnel 404, range over 366 days 400, a fresh key gets 429 after 10 export requests with `Retry-After` and the `RATE_LIMITED` body |
| Sync | registers both sources, first sync, landed rows equal the apps' exports (read from the service's SQLite), timestamps equal, second sync adds nothing, 12 new events arrive in the next sync exactly |
| Models and API | training, funnel and step ranking (benchmark and non-zero uplift), lead scores, open conversations, segments, anomalies (the planted collapse must be a `drop`), insights, `/v1/ask`, `/v1/join` |
| Identity | every exported `contact_key` equals the HMAC of an e-mail read from the app's own API under that app's pepper; capitals/padding normalised; join with the apps' two different peppers is empty and says why (the old behaviour, kept as its own assertion); after `POST /v1/identity/bridge` it recovers all 367 shared people |
| Shared pepper (runs last: it changes the apps' peppers) | fingerprints differ before; `PUT /api/v1/analytics/identity-pepper` refuses too-short, repeated-character, short-hex, non-ASCII values and a wrong `confirm` (nothing changes); a random 64-hex pepper `PUT` into both apps gives equal fingerprints (= SHA-256(pepper)[:8]), `version` up, re-import is a no-op; exports of both apps carry `HMAC(shared pepper, email)`; a fresh Intelligence account with both sources registered with that pepper (no bridge) joins the shared people (>= 95% of the 367) with no pepper warning, and the count equals the bridge result (+-2%). Reports joined contacts and coverage |
| Intelligence failure modes | revoked and expired Funnels key (other source still syncs), all keys revoked (job fails with the reason), sync with a pre-exhausted key (waits, completes, no errors), empty workspaces (every endpoint answers 200) |

## Fixtures
`--record` writes `tests/fixtures/real/` (about 400 KB): a spread of events, leads, rooms and message records (including every kind of null the real rooms have), the funnels list, steps, structure, time series, overview and the two 429 responses with their headers. They hold no API keys, peppers or e-mail addresses (contact, visitor and agent keys are HMACs of throw-away test addresses under peppers that are not stored). `tests/connectors/test_real_apps.py` replays them: record models, the real 429, and a full sync, training and every endpoint on recorded responses.

## Limits of this setup
See [INTEGRATION-FINDINGS.md](INTEGRATION-FINDINGS.md#5-not-verified-or-only-partly): no Redis or Postgres, dev mode, offline LLM and offline triage, no websockets or provider webhooks, 45 days of history, the lead and SLA models too small to show skill.

## When something fails
- `[FAIL]` lines name the check and print the evidence; `--keep-workdir` keeps `logs/funnels.log`, `logs/conversations.log`, `logs/intelligence.log` and `verify-report.json`.
- Port or process left behind: there should be none; `ps aux | grep -E "tsx|uvicorn app|fakeclock"` shows leftovers of a killed run (`kill -9` on the wrapper skips the cleanup trap).
- A Node app that fails to start: `logs/*.log` in the workdir; the usual cause is missing dependencies (`npm ci --prefix server`).
