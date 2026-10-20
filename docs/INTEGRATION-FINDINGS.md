# Integration findings: real Funnels + real Conversations + this service

Result of running the Intelligence service against the **real** Node apps for the first time (2026-10-20, macOS, Node 24.11, Python 3.13, SQLite everywhere, apps in dev mode). How it was run and how to repeat it: [INTEGRATION.md](INTEGRATION.md). Contract: `ANALYTICS-CONTRACT.md` v1 (outside this repo); schemas of the apps: `docs/analytics-api/` in each Node repo; schemas of this service: [`contracts/`](../contracts/README.md).

**Summary.** The service and the two real apps work together end to end: sync (13,000 events, 1,100 leads, 590 rooms, 4,000 message records landed exactly once and match the apps' own exports), training, rankings, lead scores, open-conversation ranking, segments, anomalies, insights and `/v1/ask` all answer on real data, and the key failure modes (revoked, expired, wrong-workspace, read-only keys, 429s, empty workspaces) behave. It did **not** work out of the box: the first sync succeeded, but training and five endpoints crashed on real data, and the cross-app join was impossible because the real apps cannot share an identity pepper. Everything on this side is fixed (section 3); the pepper problem needs a Node-side change and is bridged here meanwhile (section 1, N3).

## 1. Deviations owned by the Node apps (not changed here)

Ordered by impact. "Contract" is `ANALYTICS-CONTRACT.md`.

### N3. The two apps cannot share an identity pepper, so `contact_key` never joins (high)
- **Contract:** the workspace admin copies the pepper "into the Python service so two apps owned by the same customer produce the SAME key and can be joined".
- **Actual:** each app generates its own random 32-byte pepper per workspace and offers only *view* and *rotate-to-a-new-random-value*; there is no way to set or import a value. Funnels: `server/src/services/analyticsapi.ts`, `identityPepper()` (`randomBytes(32)`, stored in `workspaces.identity_pepper`) and `rotatePepper()`; routes `GET /api/v1/analytics/identity-pepper` and `POST .../rotate` (`server/src/routes/analyticsapi.ts:60-61`). Conversations: `server/src/services/analytics.ts`, `getPepper()` line 20 and `rotatePepper()` line 30 (`randomBytes(32)`), routes in `server/src/routes/analytics.ts:24-28`.
- **Observed:** the same e-mails produce different keys per app (verified by recomputing every exported key from the apps' own contact lists: all keys match their own app's pepper, and none match across apps). `GET /v1/join` with both real peppers: 1,122 Funnels contacts, 590 Conversations contacts, 0 joined, although 367 people are in both.
- **Needed on the Node side:** a way to set the pepper (for example `PUT /identity-pepper {pepper}` with the same permission as rotate, audited), so the customer can paste one value into both apps.
- **Mitigation here:** `POST /v1/identity/bridge` (contact list in, key pairs out). With a list of the 760 known addresses the join recovered 367 of 367 people in both apps.

### N3b. Conversations ties the pepper to `JWT_SECRET`; changing the secret breaks every export (high, operational)
- `getPepper()` stores the pepper encrypted with `JWT_SECRET` (`server/src/services/analytics.ts:22`, `decryptSecret(ctx.config.JWT_SECRET, ...)`). In development `JWT_SECRET` is generated at each start, in production it is a normal secret that gets rotated.
- **Reproduced:** copy of a database whose pepper was created under secret A, server started with secret B, `GET /api/analytics/identity-pepper` answers `500` and the log says `Unsupported state or unable to authenticate data` (`Decipheriv.final`). `exportRooms` and `overview` (by-agent keys) call the same function, so the analytics exports fail the same way.
- **Needed:** a separate stored key for the pepper, or re-encryption on secret rotation. Until then rotating `JWT_SECRET` must be treated as destroying the identity pepper. (Funnels stores the pepper in clear in `workspaces`, so it is not affected, but that is also a data-at-rest question.)

### N4. Pepper endpoint differs between the apps and from the contract (medium)
- **Contract:** `GET /api/v1/analytics/identity-pepper`, requires `analytics:export` + `analytics_identity:manage`.
- **Funnels:** `GET /api/v1/analytics/identity-pepper` exists but takes a **user session token**, not an API key ("API keys can never read it", `docs/ANALYTICS-API.md`); response `{ "pepper", "rotatedAt" }`. An API key gets 401/403 (verified).
- **Conversations:** the session endpoint is `GET /api/analytics/identity-pepper` (no `/v1`), response `{ "pepper", "version", "algorithm" }`; `GET /api/v1/analytics/identity-pepper` exists and **does** accept an API key that carries the extra scope `analytics_identity:manage` (verified: 200 with the same pepper). So one app lets a leaked integration key recompute contact keys from e-mails, the other forbids it. Funnels' key scopes (`analytics:read`, `analytics:export`) have no equivalent scope.
- **Needed:** one documented path, one response shape, one stance on API-key access.

### N1. `npm run seed` crashes in Funnels (medium, dev experience)
- `cd funnels && npm run seed` (`tsx src/seed.ts`) fails at import time: `ReferenceError: Cannot access 'ListQuery' before initialization` at `server/src/routes/spec.ts:142`. `src/index.ts` (the server) loads fine, so it is an import-order cycle that only the seed entry point triggers: `seed.ts` imports `ai/service.ts` first, which reaches `routes/spec.ts` before `services/funnels.ts` (`ListQuery`, line 52) has been evaluated.
- Workaround used here: `scripts/integration/seed_funnels.mjs` imports `src/app.ts` first (the order the server uses) and then calls `seedDemo()`.

### N2. `npm run seed` in Conversations never exits (low)
- It prints `Seeded demo workspace "Demo Co"...` and closes the database (`server/src/seed.ts:163`) but the process stays alive (something keeps the event loop open); it ran until killed (about 100 s in a `timeout 100`). The success branch has no `process.exit(0)` (the failure branch has `process.exit(1)`, line 169).
- Workaround: `run.sh` waits for the success line, then stops the process.

### N5. Page-size limits are above what the contract says (informational)
- Contract: lists `limit <= 1000`. Real: Funnels exports default to 5,000 and accept 20,000 (`limit=99999` is `400`), Conversations exports accept up to 5,000 (`400 VALIDATION_ERROR`, `limit: Too big: expected number to be <=5000`). With exports limited to 10 requests per minute the larger pages matter; the connector now uses them (section 3) and falls back to 1,000 on a 400.

### N6. Small shape differences, all tolerated (informational)
| Endpoint | Contract | Real |
|---|---|---|
| `GET /export/funnel-structure` | "funnels with steps", shape unspecified | `{ "data": [ { "id", "name", "status", "steps": [ { "step_id", "name", "order", "path", "blocks": {form,checkout,video,countdown}, "word_count", "has_order_bump", "has_upsell" } ], "has_order_bump", "has_upsell", "word_count" } ], "next_cursor": null }`: funnel key is `id` (not `funnel_id`), steps have no `type` |
| `GET /funnels/{id}/steps` last step | `exit_rate` listed as a number | `exit_rate: null` for the last step (documented by Funnels); `avg_time_on_page_s` always `null` |
| `GET /funnels/{id}/timeseries` | `{ interval, points }` | additionally `metric` and `tz` |
| `GET /export/rooms` | fields as listed | additionally `phone_key` (the contract's prose mentions it); `ai_urgency` is a JSON number 0..1, `ai_sentiment` one of `negative|neutral|positive`, `ai_intent` one of `complaint|other|purchase|question|support`; all three are `null` for rooms the app's AI triage never labelled (every room when `ai_triage_enabled` is off, which is the default) |
| `hour_of_day_created`, `dow_created` | meaning unspecified | UTC (default `tz`), and `dow_created` is JavaScript's `getDay()`: Sunday = 0. The synthetic world of this repo uses Monday = 0; the two are never combined in one model, so nothing breaks, but the contract should say which |
| Funnels `optin` / `order` events | `device` as `desktop|mobile|tablet|unknown` | `unknown` for server-created events (documented by Funnels); page views carry the real device |

### N7. Rate-limit observations (informational)
- 10 export requests per minute per key confirmed on a fresh key in both apps (10 answers, then `429`); the `429` carries an integer `Retry-After: 6`, the body `{error:{code:"RATE_LIMITED",message,retry_after_s,policy:"analytics.export"}}`, and IETF `RateLimit: "analytics.export";r=0;t=60` / `RateLimit-Policy: "analytics.export";q=10;w=60` (plus the legacy `X-RateLimit-*`). `Retry-After` (6 s) is the refill of one token (window / quota), **not** the 60 s of `t` / `X-RateLimit-Reset`.
- On keys that had made 2-3 read calls just before, only 7-8 export requests were answered before the first `429` (10 on a fresh key). The cause was not isolated (reads may share a budget with exports, or the per-workspace `analytics.workspace` quota counted them). Not verified further.
- The escalating penalty box of `RATE-LIMIT-SPEC.md` (1 min, 5 min, 30 min) was not triggered: a second immediate `429` still said `Retry-After: 6`. Not verified beyond two consecutive violations.

### N8. Minor
- Funnels refuses `PORT=0` (`PORT: Too small: expected number to be >=1`), Conversations accepts it; random-port test runs need a port picked by the caller.
- Funnels' public APIs always stamp "now" (no `at` for browsers); history can only be built with a shifted clock (as the harness does) or the app's own seed.
- Conversations' team API lists use `pagination.next_cursor`, its analytics API uses top-level `next_cursor` (both documented; the harness had to handle both).

## 2. Contract conformance of the real responses

Validated by `verify.py` against the Node repos' schemas (`docs/analytics-api/*.json`) and this repo's [`contracts/`](../contracts/):

| Check | Result |
|---|---|
| Funnels `funnels`, `steps`, `timeseries` (5 metrics x 4 intervals), `export-event`, `export-lead`, `funnel-structure`, `error` against the app's own schemas | pass |
| Conversations `overview`, `timeseries` (6 metrics), `export-rooms-line`, `export-messages-meta-line`, `error` against the app's own schemas | pass |
| Real events and leads against `contracts/event.schema.json`, `lead.schema.json`; rooms and message meta against `room.schema.json`, `message_meta.schema.json`; timeseries against `timeseries.schema.json` | pass |
| Real `steps` items against `contracts/funnel_step.schema.json` | **failed**: `exit_rate: null` on last steps. Fixed (schema allows null). Owner: this repo's contract file, not the app |
| Real funnel-structure against `contracts/funnel_structure.schema.json` | **failed**: `funnel_id` required but the app sends `id`. Fixed (accepts `id` or `funnel_id`, `step_id` or `id`). Owner: this repo's contract file |
| NDJSON: unique ids, ordered by `(ts, id)` / `(created_at, id)` across pages, `X-Next-Cursor` on every page but the last, `X-Row-Count` (Funnels), replayable cursor, gzip on large pages, content type `application/x-ndjson` | pass |
| `GET /schema` returns an integer `schema_version` (1) in both apps | pass |
| `tz` only buckets (New York days start 04:00Z), range over 366 days is `400`, ids of another workspace `404`, revoked / expired / malformed key `401` with the contract error body, read-only key on an export `403`, user session token on the analytics API rejected | pass |
| `rooms_opened` in `/overview` equals the number of exported rooms (591 = 591) | pass |
| Pepper algorithm: every exported `contact_key` equals `HMAC-SHA256(pepper, lower(trim(email)))` for an e-mail read from the app's own API, including addresses typed with capitals or padding | pass (Funnels 1,122 leads and all event keys; Conversations 590 contacts) |

Not deviations but different from the contract text: N3 to N8 above.

## 3. What was wrong on the Intelligence side, and is fixed

Each item has a regression test; the ones marked R use responses recorded from the real apps (`tests/fixtures/real/`).

| # | Symptom on real data | Cause | Fix |
|---|---|---|---|
| I1 | `POST /v1/models/train` failed; `/v1/leads/scores`, `/v1/segments`, `/v1/insights` answered 500 | `ai_intent` is `null` for rooms the app's triage did not label; `(col.astype("string") == "purchase").astype(int)` on a pandas string column yields `<NA>` and crashed (`features/leads.py`, `features/contacts.py`). The synthetic world labels every room | `norm.flag_eq` (null counts as "not equal"); R `test_recorded_responses_sync_train_and_every_endpoint_answers`, `tests/unit/test_null_encodings.py` |
| I2 | `GET /v1/rankings/conversations/open` answered 500: `Out of range float values are not JSON compliant: nan` | NaN features reached the JSON encoder | `_finite()` in `api/v1.py` turns NaN/Inf into `null`; same R test (strict-JSON check) |
| I3 | A revoked or expired key for one source failed the whole sync job, so the healthy source was not synced either | `_check_schema` raised outside the per-resource handler | rejected source: error recorded per resource, other sources continue; all sources rejected: job fails with the reason. `tests/ingest/test_source_failures.py` |
| I4 | Rate limits (10 export requests/min/key): 1,000-row pages needed 11 requests for 10,000 events and each `429` was handled only after it happened | page size capped at the contract's 1,000 for exports; no use of the `RateLimit` headers | exports ask for 5,000 rows (`CONNECTOR_EXPORT_PAGE_LIMIT`), fall back to 1,000 on a `400`; the connector reads `RateLimit`/`RateLimit-Policy`, and when a bucket is empty waits one refill interval (window / quota) instead of provoking a `429`; `Retry-After` honoured up to `CONNECTOR_MAX_RETRY_AFTER_S` (120 s), beyond that the resource fails with its cursor kept. A sync with a pre-exhausted key took 38 s and landed everything without retries. Unit tests in `tests/connectors/test_client.py`, R `test_real_apps.py` (recorded 429) |
| I5 | Contract schemas rejected real responses | `exit_rate: null`, funnel `id`, no step `type` | `contracts/funnel_step.schema.json`, `contracts/funnel_structure.schema.json` |
| I6 | Anomaly detector missed a planted 70% traffic collapse of three days on a funnel with 45 daily page views | ESD and CUSUM scale by the residual MAD; real daily noise (CV about 27%, vs Poisson in the synthetic world) put the collapse at z = -2.7 | collapse rule (3 consecutive days at or below 40% of the seasonal baseline, baseline >= 10). `tests/ml/test_anomaly.py` uses the real series |
| I7 | Funnel ranking put thank-you pages first (uplift 226 and 138, confidence low) | for the last step "passers" were its orders; confirmation pages have none, so a 0% pass rate against a benchmark | last-step outcome is orders + opt-ins; a last page with no form or checkout block and no conversion is left out, and the page before it becomes the funnel's final step (so the "final" benchmark group is the offer / upsell pages, not the thank-you pages). `tests/ml/test_funnel_rank.py` |
| I8 | `GET /v1/sync/status` showed `rows_total: 0` for `funnels` and `funnel_structure` | snapshot resources never updated the counter | counter set to the current snapshot size |
| I9 | Join empty with the real peppers (N3) | see N3 | identity bridge: `POST/GET /v1/identity/bridge`, `contact_links`, `rooms.contact_key_raw`, migration 0002, ingest-time re-keying, join report says `identity_bridge_active`. `tests/ingest/test_identity_bridge.py` |

## 4. What was verified and how (numbers from the last run)

Funnels: 7 live funnels (4 from the app's seed and templates plus 3 created through its API), 15 steps; 8,180 page views, 519 opt-ins, 278 purchases, 29 upsells over 45 days (plus the app's own 14 days of seed traffic) = 13,012 exported events, 1,122 leads. Conversations: 585 contacts, 969 widget room calls (591 distinct rooms: the widget returns a visitor's existing open room, `services/visitorChat.ts:67`), 1,992 visitor messages, 1,322 agent replies, 818 closes, 287 CSAT answers, 126 priority changes, 51 snoozes = 591 rooms, 4,031 message records. 367 people exist in both apps with the same e-mail. Sync of everything: about 9 s. Training: about 2 s.

- Landed rows equal the apps' exports (events, leads, rooms, messages, funnels, steps, structure); a second sync adds nothing; 12 new page views created afterwards are picked up by the next sync, exactly.
- Timestamps (ISO with milliseconds and `Z`) land as the same UTC second; room hour/day fields are kept as exported.
- `schema_version` 1 recorded per source; unknown fields ignored (`phone_key`).
- Empty workspaces (a second tenant in each app): sync succeeds with zero rows, training succeeds, every read endpoint and `/v1/ask` answer 200.
- Funnel ranking on real funnel shapes: after leaving out the confirmation pages the "final" group has 2 steps (the upsell and launch pages), below the 3 peers a benchmark needs, and the entry group has a benchmark but the lead-magnet, webinar, newsletter and booking funnels carry no revenue, so every `expected_uplift` is 0 and the response says why in `notes`. That is honest but not actionable: revenue is only credited to the funnel where the order happens, so a funnel whose leads buy elsewhere shows no uplift. Crediting leads with their later orders would fix it; not done.
- Models on real data: `lead_conversion` trains as `gbm` but on only 94 rows after a 14-day embargo (AUC 0.51: no skill, as expected from so few matured leads and a weak planted effect; the model says so in its metrics), `sla_breach` gbm AUC 0.45 on 413 rooms (same remark), `segments` kmeans on 1,712 contacts. These numbers say nothing about real accuracy.

## 5. Not verified, or only partly

- **Redis and PostgreSQL** were not used by any of the three programs (SQLite, in-memory limiters, DB queue). The apps' Redis limiter, the Postgres path, the Cloudflare edge and multi-node behaviour are untested here.
- **Dev mode only**; the apps' production mode (required `JWT_SECRET`, HTTPS, trusted proxies) was not exercised. Public traffic was generated with the apps' rate limits relaxed and a shifted clock (phase A), then everything was restarted with defaults for the verification (phase B); the history therefore never passed through the real public rate limiters.
- **The LLM layer** ran offline (no API key): templates and deterministic answers only. The apps' own AI triage used its offline heuristic provider, so `ai_intent`, `ai_sentiment` and `ai_urgency` come from keyword rules, not a model.
- **SMS and e-mail channels** of Conversations were only exercised as outbound agent replies through the console providers; inbound provider webhooks (Twilio, SendGrid), Socket.IO and the widget's websockets were not.
- **Webhook push** (`analytics.daily_rollup_ready`) is optional in the contract and not consumed by this service.
- **Longer histories**: 45 days. Rooms that stay open longer than the 30-day re-read window and are closed later are not refreshed by the incremental sync (`LOOKBACK_DAYS`); leads that convert after the 14-day label horizon do not matter for training. A very long backfill (hundreds of thousands of events) was measured only on the synthetic path ([PERFORMANCE.md](PERFORMANCE.md)); against the real apps it is bounded by their export rate limit (10 pages per minute of up to 20,000 rows).
- **Penalty box** escalation of the apps (N7) was not reached.
- **Detector sensitivity**: the collapse rule catches a 3-day drop of 60% or more on counts of at least 10 a day; a drop of 30-40% for a few days on noisy traffic is still not reported (by design, to keep false alerts low).
