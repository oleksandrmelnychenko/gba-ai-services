# GBA Procurement / Replenishment Service

Service 2 of the GBA recommendation/procurement initiative. For each producer, suggests
WHAT to order, HOW MUCH, and WHEN — covering forecast demand over the producer's lead time
without over-stocking. Consumed by gba-server (.NET).

## Algorithm (baseline scaffold; pluggable for tuning on real data)
- **Demand forecast** (`services/forecasting/demand.py`): moving average over the effective
  source window, including real zero-days but never fabricated days before 2025-01-01.
  Returns mean+std/day.
  Swap-in target later: Croston/SBA, statsforecast, or LightGBM global model.
- **Lead time** (`services/forecasting/lead_time.py`): per-producer empirical mean+std from
  factual invoice/order dates to factual receipt dates; configurable fallback.
- **Replenishment policy** (`services/replenishment/policy.py`):
  - `reorder_point = mean_daily·LT + z(service_level)·√LT·std_daily`
  - `order_up_to   = reorder_point + horizon·mean_daily`
  - `position      = on_hand − reserved + on_order`
  - `suggested_qty = max(0, order_up_to − position)` when `position ≤ reorder_point`
  - urgency from days-of-cover vs lead time; items ranked critical-first.

## Data (read-only over ConcordDb_V5)
Factual international supply uses `SupplyInvoice/SupplyInvoiceOrderItem/PackingList/
ProductIncome`; domestic supply uses `SupplyOrderUkraine/SupplyOrderUkraineItem/
ProductIncome`. `ProductAvailability` and `ProductReservation` are current point-in-time
inputs. `Order/OrderItem` is the realized demand and revenue source.

### Source-history contract

- `SOURCE_HISTORY_START_DATE=2025-01-01` is the earliest supported 1C fact date.
- `as_of_date < 2025-01-01` is rejected with HTTP 422.
- Rolling demand, cost, sale, revenue and readiness windows are clamped to that floor.
- Lead times, agreement currency, MOQ and on-order facts use all available history in
  `[2025-01-01, as_of_date)`.
- Forecasting and XYZ receive `effective_history_days`; no pre-floor zero-days enter their
  denominator or dense series.
- Every plan/chart and source-readiness response exposes `source_history_start`,
  `effective_start`, `effective_history_days`, and `history_complete`.
- Inventory and reservations are current snapshots, so responses explicitly report them
  in `history_not_applicable`.

## Run
```bash
make install
cp .env.example .env   # fill DB_PASSWORD (read-only login)
make dev               # uvicorn on :8001
```

## API
- `POST /plan/producer` — `{producer_id, as_of_date?, only_needed}` → purchase plan.
- `POST /plan/cart` — canonical cross-producer replenishment cart.
- `POST /plan/charts` — dashboard chart payload.
- `GET /health`, `GET /metrics`.

## Security
- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader). Never `sa`. Secrets only via `.env`.

## Status
Scaffold complete + live-validated (producer 365: full chain, correct ROP/qty math).
Tune on real data later: forecast method, lead-time semantics, batch the N+1 forecast queries.
