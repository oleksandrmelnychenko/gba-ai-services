# GBA NBA

FastAPI service that generates and manages Next Best Action tasks for the sales cockpit.

## Source-history contract

All source data is considered complete starting at `SOURCE_HISTORY_START_DATE`
(`2025-01-01` by default).

- Rolling signals use `effective_start = max(requested_start, source_history_start)`.
- Factual purchase-history signals use the available interval
  `[source_history_start, as_of)`.
- An `as_of` before the source boundary is rejected with HTTP 422.
- API and readiness responses expose `source_history_start`, `effective_start`, and
  `history_complete`; `history_complete=false` means the requested rolling window extends
  before available source history.
- Training snapshots require a complete 365-day feature window. Cache entries, datasets, and
  model metadata are stamped with the source boundary; artifacts without a matching stamp are
  rejected rather than silently reused.

Changing the boundary requires rebuilding the NBA dataset and passing the existing retraining
quality gates before the new model can be served.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
```

The dev database reconciliation is read-only:

```bash
make integration
```
