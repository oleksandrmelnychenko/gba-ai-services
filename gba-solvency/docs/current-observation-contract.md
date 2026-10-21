# Solvency current-observation serving contract — 2026-09-08

The current scorecard was trained with a 12-month feature window. `/score` and `/score/batch` now require exactly integer `window_months=12` (omission defaults to 12). `/charts/{client_id}` keeps its independent `months=1..60` transaction display window. It never rebuilds historical feature states or calls the scorecard for monthly points.

All three serving methods resolve one `Europe/Kyiv` business day before client lookup, cache access or feature SQL. `as_of_date` may be omitted or equal that captured day. A different past/future date is rejected with HTTP 422; an unsupported score window is also 422. A configured FX snapshot that differs from this day produces HTTP 503 before business I/O. It is not silently replaced by another rate. Feature and currency-breakdown inputs use the same resolved day.

## Required response fields

Both score and charts require `state_basis="current_observation"`, `business_timezone="Europe/Kyiv"`, and ISO `fx_date` equal to `as_of_date`. The existing SQL cutoff, feature arithmetic, source-history floor, trained coefficients and operational 90-day rule are unchanged. Current observation means the currently recorded rows read by the existing queries; it does not claim an atomic database snapshot, end-of-day closing balance or immutable state on a previous date. Existing calendar-date SQL cutoffs remain unchanged.

Charts always return `score_sparkline=[]`. For an applicable buyer, including insufficient-data buyers, the required pair is `score_sparkline_status="unavailable"` and `score_sparkline_reason_code="historical_state_not_recorded"`. The user-visible reason is: «Історію оцінки не збережено. Поточні борги та умови договорів не відновлюють стан минулих місяців.»

For a non-buyer, the pair is `not_applicable` / `client_not_buyer`; the reason is «Оцінка не застосовується: клієнт не має ролі покупця.» A present numeric series, missing/contradictory pair or blank reason is invalid. No `available` history status is supported in this contract. A no-data headline retains its existing null score/PD/rating/90-day-risk behavior.

`history_complete` continues to describe transaction-window coverage since the configured source floor. It is not proof that monthly debt states, deleted rows, credit terms or score snapshots were captured. `turnover_vs_exposure` remains historical recorded turnover with a flat current exposure reference, not a historical debt trajectory.

## Cache and consumers

The cache namespace changes to `response-v5`; no live flush is necessary. Hydration requires explicit new fields, the requested client/date/window, current model label, and consistent empty-history metadata. Cached insufficient scores cannot contain numeric scores. Invalid entries are misses and are rebuilt under the current contract.

The .NET consumer must require the new fields, require score window 12, and accept empty buyer history only with the exact unavailable pair. It must reject legacy numeric sparklines. The console validates the same fields and shows the reason. The approved rollout installs the strict .NET consumer first, then Python and the matching console. A brief unavailable response during the transition is accepted; serving an older fabricated history is not a fallback. Check an authenticated current buyer, insufficient buyer and non-buyer only after coordinated deployment; no service restart is part of this commit.

## Historical training is a separate responsibility

`app/risk/dataset.py`, `app/risk/score_current.py`, `app/risk/lineage.py`, monitoring/training writers and all model artifacts are unchanged. Dataset manifests require 12 months, but a manifest does not transform mutable debt/terms into historical snapshots. The research paths `build_dataset`, `features_one/many`, training parquet generation and model evaluation can still accept their own feature dates. They are not the client serving API and their results must not be published as a proved historical client-score trajectory. The old `scripts/benchmark_vs_old.py` fixed-date live API call is now intentionally unsupported; use frozen evidence for historical comparison instead of replacing the requested date with today.

Historical serving would require immutable client features or scores captured at observation time, exact client/model identity, trained window, source/FX provenance, sufficiency proof, capture date and durable retention. The current chart path has no such reader. Redis response caching and aggregate monitoring artifacts are not that proof.

## Verification

Offline tests cover current-day and DST/midnight behavior, one clock read, fixed score window versus chart display window, no-data and non-buyer states, stale caches, FX mismatch, and pre-I/O rejection. All HTTP/database access is denied in the isolated test runner; actual model JSON is read into a private test sandbox without modifying the live files. .NET contract tests cover required JSON fields, exact pairs, empty buyer history, rejection of numeric legacy history and FX/window mismatch. No live SQL, 1C connection, retraining, financial mutation or restart is required for these tests.
