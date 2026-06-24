# GBA Procurement / Replenishment Service

Service 2 of the GBA recommendation/procurement initiative. For each producer, suggests
WHAT to order, HOW MUCH, and WHEN — covering forecast demand over the producer's lead time
without over-stocking. Consumed by gba-server (.NET).

## Algorithm (baseline scaffold; pluggable for tuning on real data)
- **Demand forecast** (`services/forecasting/demand.py`): moving average over the full window
  including zero-days (correct for intermittent B2B spare-parts demand). Returns mean+std/day.
  Swap-in target later: Croston/SBA, statsforecast, or LightGBM global model.
- **Lead time** (`services/forecasting/lead_time.py`): per-producer empirical mean+std of
  `SupplyOrder.Created → OrderArrivedDate`; configurable fallback.
- **Replenishment policy** (`services/replenishment/policy.py`):
  - `reorder_point = mean_daily·LT + z(service_level)·√LT·std_daily`
  - `order_up_to   = reorder_point + horizon·mean_daily`
  - `position      = on_hand − reserved + on_order`
  - `suggested_qty = max(0, order_up_to − position)` when `position ≤ reorder_point`
  - urgency from days-of-cover vs lead time; items ranked critical-first.

## Data (read-only over ConcordDb_V5)
`SupplyOrder/SupplyOrderItem` (orders + lead time), `SupplyOrganization` (producers),
`ProductAvailability` (on-hand), `ProductReservation` (reserved, via ProductAvailabilityID),
`Order/OrderItem` (demand history).

## Run
```bash
make install
cp .env.example .env   # fill DB_PASSWORD (read-only login)
make dev               # uvicorn on :8001
```

## API
- `POST /plan/producer` — `{producer_id, as_of_date?, only_needed}` → purchase plan.
- `GET /health`, `GET /metrics`.

## Security
- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader). Never `sa`. Secrets only via `.env`.

## Status
Scaffold complete + live-validated (producer 365: full chain, correct ROP/qty math).
Tune on real data later: forecast method, lead-time semantics, batch the N+1 forecast queries.
