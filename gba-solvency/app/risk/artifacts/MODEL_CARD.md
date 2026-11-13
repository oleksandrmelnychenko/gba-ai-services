# Solvency v3 model card

Snapshot: 2026-07-25. Source history begins 2025-01-01.

## Current-state SEV180 scorecard — published

- Training run: `current-20260725T131604+0000-bee58752534e`
- Feature date: 2026-04-26; label date: 2026-07-25
- Requested/effective 12-month start: 2025-04-26 (complete)
- Dataset: 4,180 unique buyers, 24 positives (0.57%)
- Dataset SHA-256:
  `bee58752534eaf4fe227e41f0bf5ad6e6d458e57d63cbb1d8ae05d57f3503f5a`
- Production WOE scorecard, five-fold stratified CV: mean AUC 0.98808, OOF AUC 0.98615,
  mean KS 0.97955, mean Brier 0.02988
- Gate: 4,180 ≥ 1,000 rows; 24 ≥ 20 positives; OOF AUC 0.98615 ≥ 0.90 — pass
- Current-state safety gate: all 24 open-SEV180 rows receive PD ≥ 16% and rating D — pass

Target-defining `overdue_eur_180plus` and `pct_debt_180plus` are excluded. This is still a
current-state model: other debt-aging/exposure features share the debt source with SEV180 and
carry strong mechanical signal. The GBM diagnostic AUC is 0.7158 using non-debt-table features
alone, 0.7224 with debt trajectory but without aging buckets, and 0.9998 with the full primary
feature set. A transparent current-state policy floors PD at 16% (rating D) whenever current
open 180+ exposure is at least EUR 100. Do not describe the current PD as an independent
six-month forecast.

Open balances and current agreement terms are retained as point-in-time state. Transactional
trajectory, sales/RFM and returns are bounded by the source-history floor.

## Six-month forward SEV180 — unavailable

- Vintage feature dates: 2025-09-10 through 2025-12-17; labels: 2026-03-10 through 2026-06-17
- Dataset SHA-256:
  `238ada59132861e8c9421760eb47e2aa304e6ea18f0b420416f4169148f4a749`
- Full pool: 33,267 rows and 19 positive rows
- At-risk-with-debt population: 1,159 rows, 148 unique clients, 19 positive rows
- Independent event ceiling: 3 unique positive clients
- Gate: 3 < 30 unique positive clients — fail before fitting

No forward artifact is published and no legacy forward PD is served. The API returns the valid
current score with `forward_risk=null`, `forward_risk_status="model_unavailable"` and the support
reason. `forward_model_status.json` is the deployable evidence for this degraded state.

<!-- quality:start (written by scripts/retrain.py) -->
## Quality where the scorecard has to rank

- Training run: `current-20261109T014047+0000-dfb89fb2ed42`
- All rows, five-fold OOF AUC: 0.9786 (most buyers carry no debt, so this mostly measures has-debt / has-no-debt)
- Buyers with open debt at the feature date: 177 rows, 25 positives (5.4% of the dataset)
  - out-of-fold: AUC 0.6254, KS 0.3061, Brier 0.6033 (class-balanced, uncalibrated fold models)
  - as served (calibrated PD with the SEV180 floor, in-sample): AUC 0.7709, KS 0.6639, Brier 0.1093
- Live buyers scored exactly 100: 1299 of 1814 scored (71.6%, as of 2026-11-12)
<!-- quality:end -->
