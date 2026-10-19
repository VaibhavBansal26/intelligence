# Glossary

| Term | Meaning |
|---|---|
| Account | One customer of Intelligence. Owns sources, keys, data and models. Every row and every query is account scoped. |
| Source | A registered connection to one app: `funnels` or `conversations` (base URL, API key, identity pepper). |
| Contract | `ANALYTICS-CONTRACT.md` v1: the only coupling with the Node apps. |
| Contact key | `HMAC-SHA256(pepper, lower(trim(email)))` hex. Same pepper in both apps gives the same key, which joins them. |
| Pepper | Per-workspace secret used for contact keys. Mismatched peppers mean zero join coverage. |
| Join coverage | Share of the smaller app's contacts that also appear in the other app. |
| Landing tables | Local copies of source data (`events`, `leads`, `rooms`, ...). |
| Watermark | Highest timestamp synced per resource; the next sync starts there (mutable resources re-read a lookback window). |
| Resume cursor | The `X-Next-Cursor` of the last page durably written; an interrupted sync restarts from it. |
| Pass rate | Share of a step's unique visitors that reach the next step (final step: place an order). |
| Shrinkage | Pulling a noisy rate towards its peers with an empirical-Bayes Beta prior in proportion to how little data it has. |
| Benchmark | Median shrunk pass rate among peer steps with the same role (entry, middle, final). |
| Expected uplift | Revenue gained if a step reached the benchmark, with p10/p90 from posterior draws. |
| Mature lead | A lead whose label window (14 days) ended before the data end; only these train the lead model. |
| Embargo | Gap between train and validation windows equal to the label horizon, so labels do not overlap. |
| PSI | Population stability index between training and recent feature distributions (0.1 warn, 0.25 alert). |
| Cold start | Not enough data for a model: a documented heuristic is used and the response says so. |
| NumberGuard | Post-hoc check that every number in LLM text appears in the structured evidence. |
| Template narrator | Deterministic offline narrator used when the LLM is off, fails or is rejected. |
| Finding | A ranked, structured fact (step, anomaly, conversation, ...) the narrator may talk about. |
| Data version | Per-account counter bumped by sync, training and promotion; basis of ETags. |
| Legacy terms | Not applicable: this is a new service (no legacy product mapping). |
