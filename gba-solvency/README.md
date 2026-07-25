# GBA Client Solvency Service

Per-client solvency / платоспроможність scoring for GBA. Service 4 of the GBA AI family
(8000 reco, 8001 procure, 8002 nba, **8003 solvency**). Consumed by gba-server (.NET) and
the console. Mirrors the hardened infra of gba-reco: read-only SQL login, parameterized SQL,
env-only secrets, structured JSON logging, thread-safe metrics, graceful Redis degradation.

## Production model — `creditscore-v3`

The serving model is an explainable WOE + logistic current-state SEV180 scorecard. It returns
score `0..100` (higher is safer), calibrated current-state PD, A/B/C/D rating and signed
per-feature contributions. The old five-factor `creditscore100-v2` output is retained only as
nullable response fields for compatibility and is not the production score.

All transactional features are clamped to `SOURCE_HISTORY_START_DATE=2025-01-01`. Two
point-in-time state inputs are intentionally exempt from that historical cutoff:

- open debt balances that still exist as of the feature date;
- current agreement credit terms as of the feature date.

A date string in a JSON model is not accepted as lineage. Dataset builders persist the feature
and label dates inside parquet, write a SHA-256 sidecar with counts and effective history windows,
and trainers embed that verified manifest plus the validation gate and a model-payload hash.
Serving rejects artifacts that lack or contradict any part of that chain.

The six-month forward model is currently unavailable because the post-floor cohort contains only
3 unique positive clients versus the production minimum of 30. `/score` therefore preserves the
validated current score but returns `forward_risk=null`,
`forward_risk_status="model_unavailable"` and a reason. `/health` is degraded, while `/ready`
remains ready when infrastructure, source data and the current model are valid.

See [the exact model card](app/risk/artifacts/MODEL_CARD.md) and
[lineage/retrain contract](docs/model-lineage.md).

## Critical data traps (honored in `solvency_repository.py`)

- **NEVER** filter `Deleted=0` on `Sale`/`Order`/`OrderItem` (=1 on 100% of rows → empty
  results). Validity comes from `OrderItem.IsValidForCurrentSale=1` and `SaleReturn.IsCanceled=0`.
- Exclude `ProductID 25422404` ('Ввід боргів з 1С' synthetic line) from turnover/activity, but
  KEEP it in debt/exposure (it is real carried debt).
- Pin the **FX snapshot date** per run (`GetExchangedToEuroValue` revalues at call time →
  non-deterministic). Configured via `FX_SNAPSHOT_DATE` / `as_of_date`.
- `BaseSalePaymentStatus.Amount=0` even when Paid → use the status **ENUM** (count-based), not
  money columns.
- Multi-currency: EUR-normalize via `dbo.GetExchangedToEuroValue`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # fill DB_PASSWORD with the read-only login
.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8003
```

## API

- `POST /score` — `{client_id | client_net_uid, as_of_date?, window_months=12}` → `SolvencyScore`.
- `POST /score/batch` — `{client_ids[], as_of_date?}` → list of `SolvencyScore` (errors isolated).
- `GET /charts/{client_id}?as_of_date=&months=12` → `SolvencyCharts` (live-buildable charts only;
  aging-over-time heatmap = `pending` until Debt sync settles).
- `GET /health`, `GET /ready`, `GET /metrics`.

## Security

- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader only). Never `sa`.
- Secrets only via `.env` (gitignored). No credentials in code.

## Status

Implemented: config (port 8003, Redis db 2, `creditscore-v3`), pooled read-only DB layer,
parameterized solvency repository, lineage-gated current-state scoring, fail-closed forward
status, explainability, charts, domain models, FastAPI shell and tests.
