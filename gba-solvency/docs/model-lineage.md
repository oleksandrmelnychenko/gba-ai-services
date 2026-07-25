# Solvency model lineage and publication

The canonical transactional-history floor is `2025-01-01`. Transactional queries bind it as
`history_start`; they never manufacture activity before that date. Open balances and current
credit terms are point-in-time state and remain explicit exceptions.

## Evidence chain

1. `build_risk_dataset.py` and `build_vintages.py` store feature/label dates in parquet.
2. Each builder writes a `.lineage.json` sidecar containing the parquet SHA-256, row/event/client
   counts, history coverage for every feature date and the two current-state exceptions.
3. A trainer refuses to run if the sidecar, parquet bytes, date columns or counts differ.
4. A production artifact embeds the verified manifest, fixed validation thresholds, observed
   metrics, training run ID and a hash of the predictive payload.
5. The serving loader recomputes and validates those relationships. A legacy artifact with only
   `source_history_start` is rejected.

## Publication gates

Current-state requires at least 1,000 rows, 20 positives, a complete 12-month feature window and
five-fold OOF AUC of at least 0.90. A separate safety invariant requires every current open
SEV180 exposure (at least EUR 100 at 180+ days) to receive PD of at least 16% and rating D.

Forward six-month risk requires at least 30 unique positive clients, OOT AUC of at least 0.75 and
monotone PD bands. Event support is checked before fitting. If it fails under `--commit`, the
trainer writes `forward_model_status.json` as unavailable and removes any stale forward
scorecard. The API then returns no forward PD.

`scripts/retrain.py` rebuilds both datasets, trains current-state, invokes the forward publication
gate, validates current OOF AUC and refreshes the drift baseline. A dry run backs up and restores
all serving artifacts, including the forward status. A failed current-state gate restores the
entire previous set.

## Operational readiness

`/health` reports separate `current_state` and `forward_6m` model readiness. A missing or rejected
current model makes the service not ready. An unavailable forward model makes health degraded,
but `/ready` stays 200 so the independently validated current score remains usable.
