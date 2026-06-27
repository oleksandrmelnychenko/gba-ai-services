# GBA Client Solvency Service

Per-client solvency / платоспроможність scoring for GBA. Service 4 of the GBA AI family
(8000 reco, 8001 procure, 8002 nba, **8003 solvency**). Consumed by gba-server (.NET) and
the console. Mirrors the hardened infra of gba-reco: read-only SQL login, parameterized SQL,
env-only secrets, structured JSON logging, thread-safe metrics, graceful Redis degradation.

## Algorithm — CreditScore-100 (`creditscore100-v2`)

Per client, aggregated over the client's agreements, trailing `window_months` (default 12) by
`Sale.Created`:

```
Score = 100 * (0.35*PaymentDiscipline + 0.25*DebtLoad + 0.20*Activity
               + 0.10*Tenure + 0.10*ReturnQuality)
```

Each sub-factor is normalized to 0..1 (1 = best / lowest risk):

1. **PaymentDiscipline** = `(paid + overpaid + 0.5*partial) / (paid + overpaid + partial + notpaid)`
   over the client's sales. Status comes from `BaseSalePaymentStatus.SalePaymentStatusType`
   (`NotPaid=0, Paid=1, Overpaid=2, PartialPaid=3, Refund=4`). Paid+Overpaid = good,
   PartialPaid = 0.5, NotPaid = bad, **Refund=4 excluded** from the ratio. Retail sales use a
   DIFFERENT enum `RetailPaymentStatusType` (`PartialPaid=3, Paid=4`) — handled by a separate
   mapping layer, never conflated.
2. **DebtLoad** — *sync-aware*. If the `Debt` table is quiesced (rows with `Deleted=0` and sane
   `Created`): `clamp(1 - overdue_eur / turnover_eur, 0, 1)`, where `overdue` = `SUM(Debt.Total->EUR)`
   for debts older than the agreement grace (`Agreement.NumberDaysDebt`). Otherwise (Debt
   sync-blocked, all `Deleted=1`): live proxy `clamp(1 - open_unpaid_count / total_sales, 0, 1)`.
   The service detects which source is live (`debt_sync_is_live()`) and records it in
   `debt_load_source`.
3. **Activity** = `0.5*recency + 0.5*frequency`; `recency = clamp(1 - recency_days/90, 0, 1)`,
   `frequency = clamp(order_count/24, 0, 1)`.
4. **Tenure** = `clamp(tenure_months/24, 0, 1)`.
5. **ReturnQuality** = `clamp(1 - return_qty_rate*2, 0, 1)`.

**Caps** (credit policy, applied AFTER the weighted sum): controlled agreements
(`Agreement.IsControlAmountDebt=1, AmountDebt>0`) — if `limit_utilization = CurrentAmount/AmountDebt > 1.0`
hard-cap Score at **40**; if `> 0.9` cap at **60**. If `Client.IsBlocked=1` multiply Score by **0.5**.
Then round to int 0..100.

**Rating bands**: A = 80-100, B = 65-79, C = 45-64, D = 0-44.

**Explainability**: every sub-factor returns its raw 0..1 value AND its weighted points
contribution, plus which caps fired — so the UI renders contribution bars + the reason.

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
- `GET /health`, `GET /metrics`.

## Security

- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader only). Never `sa`.
- Secrets only via `.env` (gitignored). No credentials in code.

## Status

Implemented: config (port 8003, Redis db 2, `creditscore100-v2`), pooled read-only DB layer,
parameterized solvency repository, scoring engine, caps, ratings, explainability, charts,
domain models, FastAPI shell, tests.
