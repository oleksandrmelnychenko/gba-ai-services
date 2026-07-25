# GBA Products

Read-only product intelligence service for assortment, inventory health, product analytics,
regional demand, margin, returns, and substitutes.

## Runtime

- Port: `8005`
- App: `app.api.main:app`
- Health/readiness: `GET /health`, `GET /ready`
- Product analytics: `GET /product/{product_id}/analytics`
- Portfolio: `GET /assortment/overview`, `GET /assortment/health`

All routes except `/health` require `X-Internal-Api-Key` when `INTERNAL_API_KEY` is configured.

## Source history contract

```env
SOURCE_HISTORY_START_DATE=2025-01-01
```

This is the first date for which historical sales, returns, regional demand, and factual supply
producer signals are considered available. Every rolling SQL interval is:

```text
effective_start = max(requested_start, SOURCE_HISTORY_START_DATE)
```

Dense monthly classification and product-analytics grids start at `effective_start`; the service
never invents zero-demand months before source history exists. Velocity and days-of-cover use the
number of effective factual days rather than the originally requested denominator.

API and readiness responses expose `source_history_start`, `requested_start`, `effective_start`,
`history_complete`, `history_fingerprint`, and per-signal `history_windows`. Explicit `as_of_date`
values before the source floor return HTTP 422.

The source floor is also part of the cache namespace and the default model version is
`products-v5-source-history-floor`, preventing reuse of snapshots built under the old history
contract.

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```
