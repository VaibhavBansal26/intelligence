# Models

> **Read this first.** Every metric below was measured on the seeded synthetic world (`app/synthetic/world.py`, seed 7,
> 120 days, about 53k events, 7k leads, 8k rooms, 2026-10-19, Apple M-series laptop). The effects were planted, so these
> numbers prove that the pipeline recovers known signal without leaking the future. They are **not** estimates of accuracy on
> real data. Volumes real accounts need are in [APPROACH.md](APPROACH.md#what-real-data-volume-is-needed).

Common to the two gradient-boosting models (`app/models/gbm.py`):
- Estimator: scikit-learn `HistGradientBoostingClassifier` (fixed iterations, no random early stopping, `random_state` = `SEED`); LightGBM if installed and selected.
- Split: time ordered. The first 70% by time is training (its last 20% is used only to fit the calibrator), the last 30% is validation, with an **embargo** equal to the label horizon between them. `ML_MAX_TRAINING_ROWS` keeps only the most recent rows.
- Calibration: isotonic (400+ rows, 20+ of each class), Platt below that, none if unusable.
- Metrics stored with each version: AUC, PR-AUC, Brier (and the base-rate Brier), expected calibration error, lift by decile, permutation importance (5 repeats on validation), training time, row counts.
- Model card JSON (target, features, split, window, seed, library versions, limitations) is stored on the version and returned by `GET /v1/models`.
- Drift: a quantile-bin reference of every numeric training feature (and category shares) is stored; `GET /v1/models/types/{type}/drift` returns per-feature PSI of recent rows.
- Cold start: below `MIN_LEADS_FOR_MODEL` / `MIN_ROOMS_FOR_MODEL` labelled rows, or fewer than `MIN_CLASS_COUNT` of a class, a documented heuristic is registered instead (`method: heuristic`) and every response includes `cold_start_reason`.
- Registry: versions are immutable joblib files with sha256; auto-promotion keeps the new version only if validation AUC is within 0.02 of the active one (or the active is a heuristic); promote and rollback are explicit admin actions.
- Per-prediction factors are **occlusion effects**: the change in probability when one feature is reset to a reference value (training median or mode). They are not Shapley values and interactions are not apportioned.

## 1. Funnel and step ranking (`app/models/funnel_rank.py`)
- **Target**: expected revenue gained if a step's pass rate reached the benchmark. Pass rate = unique visitors reaching the next step / step unique visitors (final step: (orders + opt-ins) / unique visitors). A last page with no form or checkout block and no conversion (a thank-you / confirmation page, as in the real Funnels app) is left out, and the page before it is the funnel's final step; without this, confirmation pages showed a 0% pass rate and topped the ranking on real data. Benchmark = median shrunk rate among steps with the same role (entry, middle, final); with fewer than 3 peers no uplift is claimed.
- **Shrinkage**: per-role Beta prior by method of moments (`fit_beta_prior`), optionally centred on a ridge prediction from funnel-structure content features (log words, form, video, countdown, bump, upsell) blended 50/50 with the role mean when a role has 6+ steps.
- **Uplift** = visitors x max(0, benchmark - shrunk rate) x revenue per passer (downstream gross / max(passers, 5)). p10/p50/p90 from 1000 seeded Beta-posterior draws. Funnels rank by their weakest step.
- **Content hints**: rules over structure (long copy without video, sales step without video, checkout without bump, no urgency element, many forms). Hints are suggestions, not measured causes.
- **Synthetic result**: the planted weak step (webinar "Sales Page", pass rate 0.235 vs benchmark 0.680, 3,139 visitors) ranks 1 with expected uplift 230,111 (p10 224,805, p90 235,172); the next step is worth about 3,003. The tiny 10-visitor funnel never outranks it (unit test: a 0-of-3 step keeps a shrunk rate near the group mean).
- **Limits**: ignores orders not attributed to a step (late/offline orders); one benchmark per role, not per traffic source or device; assumes later steps are only reachable through earlier ones; revenue is only credited to the funnel where the order happens, so a lead-magnet funnel whose leads buy in another funnel shows no uplift; revenue per passer is not adjusted for the uplifted visitors being marginal (probably worth less than average).

## 2. Lead conversion (`app/models/lead_model.py`, `app/features/leads.py`)
- **Target**: order within 14 days of opt-in (`LABEL_HORIZON_DAYS`, per-account override). Only leads whose window closed before the data end are used; converted leads without an order date are excluded.
- **Features (point in time, strictly before opt-in)**: pre-opt-in page views and distinct steps, hours since first seen, opt-in hour and weekday, prior orders, UTM source, funnel, device, and, when a Conversations source is linked, prior rooms, prior purchase intent, sentiment, urgency, first-response and CSAT of rooms already closed. All-missing columns (single-source accounts) are dropped and listed in the metrics.
- **Leakage guards**: label maturity, embargo, point-in-time conversation joins (tests recompute a lead's features with an earlier `as_of` and require identical values).
- **Synthetic result** (validation window of 1,858 leads, base rate 35.4%): AUC 0.756, PR-AUC 0.608, Brier 0.187 vs 0.229 for the base rate, ECE 0.037, top-decile lift 2.13 capturing 21% of buyers. Permutation importance ranks `utm_source` (0.060), `prior_intent_purchase` (0.055), `hours_since_first_seen` (0.035), `n_page_views_pre` (0.020) first, matching the planted drivers; opt-in hour and weekday are near zero.
- **Limits**: opt-in-time information is weak by construction, so AUC is modest; scoring an old lead uses its opt-in-time features, not its later behaviour; no uplift/causal claim ("who to contact" is not "who is persuadable"); needs the volumes in APPROACH.md.

## 3. Conversation prioritisation (`app/models/room_model.py`, `app/models/priority.py`)
- **Model target**: first response slower than the SLA (`SLA_FIRST_RESPONSE_S`, default 3600 s; per-account override). Rooms still unanswered past the SLA are breaches; unanswered rooms still inside the window are censored and excluded.
- **Features (known at creation)**: hour, weekday, priority, AI sentiment, urgency, intent, inbox, channel, contact's earlier rooms and their mean response time (only answered ones), lifetime funnel revenue and orders before the room. Outcome columns are excluded by design (tested).
- **Priority score (0-100)** = 100 x (0.35 P(breach) + 0.20 urgency + 0.15 negative sentiment + 0.15 waiting/SLA (capped at 3x) + 0.10 VIP + 0.05 reopens), floor of 60 for rooms already past the SLA without a reply, +10 for VIP with negative sentiment. Weights are in `PriorityWeights`. Each item returns the components, plain-language reasons and the model's top factors. `churn_risk` is a heuristic combination (negative sentiment, reopens, waiting, VIP), not a trained churn model.
- **Synthetic result** (2,397 validation rooms, base rate 36.5%): AUC 0.688, PR-AUC 0.543, Brier 0.208 vs 0.232, ECE 0.022; importance ranks `hour` (0.085), `inbox_id` (0.050), `dow` (0.031), `channel` (0.024), matching the planted night, billing-inbox, weekend and email effects. In the open-room ranking the four planted VIP/negative/urgent overdue rooms occupy ranks 1-4 and the calm new room ranks below them.
- **Limits**: sentiment/urgency/intent are assumed available shortly after room creation; the rule weights are judgement, not fitted; VIP is defined only by funnel revenue.

## 4. Anomaly and trend detection (`app/models/anomaly.py`)
- Baseline: seasonal-naive median of the same phase over the previous 6 periods (weekday medians for daily series), needing 3+ lags. Residuals are scaled by MAD with a Poisson-like floor for counts.
- Detectors: generalised ESD (robust, MAD-scaled) for point outliers and two-sided CUSUM for sustained shifts. Guards: minimum 28 points, relative deviation >= 25% (30% for rate series), absolute deviation >= 5 for counts, |z| >= 3.5 (5 for rate-like series), CUSUM run >= 3 days, series with more than 30% zero days are skipped, the still-running bucket and leading zero history are dropped.
- Collapse rule (count-like series): a run of at least 3 consecutive days at or below 40% of the seasonal baseline (baseline of at least 10) is reported as a `drop` with detector `collapse`, because on noisy real traffic (daily CV about 27%) neither ESD nor CUSUM can reach its thresholds in three days. Chance of three such days in a row on clean data with 25-30% noise is about 1 in 300,000.
- Episodes of consecutive flagged days merge into one alert with a stable fingerprint (metric, scope, direction, start day) stored per account; repeated runs update instead of re-alerting.
- **Synthetic result** (32 series checked, 5 skipped for sparsity): the planted 3-day webinar traffic collapse is found on `unique_visits`/`visits` for the funnel and the account (severity 4.5-6.0, days 5-8 September); the planted support backlog incident is found on `backlog` (severity 17.5) and `resolution_p50`. 2 further alerts (account `gross` and `messages_in`, severity 3.7 and 4.5) are false alarms. On 60 clean seeded series (level 200/day, weekly seasonality, 0.2%/day trend) the unit test allows at most 8 alerts (observed rate below 0.15 per series); detection power on a 2-day 80% drop at level 200 was 100%, at level 30 about 90%, at level 8 about 1% (by design: too little volume).
- **Limits**: daily-regular seasonal count series only; no holiday calendar; slow drifts below the thresholds are not alerts; very low-volume metrics are silent.

## 5. Segment discovery (`app/models/segments.py`)
- KMeans (k from 3 to 6 chosen by silhouette on up to 3,000 sampled contacts, `n_init=10`, seeded) on standardised log counts of revenue, orders, page views, steps, rooms, open rooms, purchase-intent rooms, reopens, plus sentiment, recency and source flags (negative revenue from refunds is clipped for the log).
- Names are rule-based labels from cluster statistics ("High-value buyers", "Hot prospects (asked about buying)", "Support-heavy contacts", ...), made unique; descriptions carry size, revenue share and averages.
- **Synthetic result**: 9,985 contacts, k = 6, silhouette 0.479; the buyers cluster holds about 97% of revenue.
- Below `MIN_CONTACTS_FOR_SEGMENTS` three rule-based segments are used (customers, leads, support-only).
- **Limits**: names are heuristics over centroids, clusters are not guaranteed stable across retrains, and segments describe rather than predict. For accounts with a Parquet store, profiles are read out-of-core and down-sampled to `ML_MAX_TRAINING_ROWS` contacts ([SCALING.md](SCALING.md)).

## Retraining
`POST /v1/models/train?wait=true` (analyst) or `POST /admin/accounts/{id}/retrain`. Retraining is a full refit on the most recent labelled rows; incremental refresh applies to the feature layer (Parquet profiles), and a retrain is only advised when drift is `warn`/`alert` or labels have matured materially. Use [RUNBOOK.md](RUNBOOK.md) for rollback.
