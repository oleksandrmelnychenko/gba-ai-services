# gba-procure — Deployment & Operations Runbook

Procurement / replenishment service (Service 2). FastAPI + Redis, read-only over ConcordDb_V5.
Suggests per-producer purchase plans (what/how-much/when to order).

## Prerequisites
- Docker + docker compose, OR Python 3.12 + FreeTDS (`apt install freetds-dev`).
- A **read-only** SQL login on ConcordDb_V5 (dev: `gba_reco_ro`, shared with gba-reco).
- DB reachability (dev: `gba-dev-gba-mssql-1` on `gba-dev_default`).

## 1. Configure
```bash
cp .env.example .env
# set DB_HOST / DB_USER=gba_reco_ro / DB_PASSWORD / REDIS_*
# policy knobs: SERVICE_LEVEL (0.95), FORECAST_HORIZON_DAYS (30),
#               HISTORY_DAYS (365), DEFAULT_LEAD_TIME_DAYS (30)
```
Note: this service uses `REDIS_DB=1` (gba-reco uses 0) to share one Redis safely.

## 2. Run with Docker
```bash
docker compose up -d --build      # redis :6380 + api :8001
```
On the DB network:
```bash
docker run -d --name gba-procure-api --network gba-dev_default \
  -e DB_HOST=gba-dev-gba-mssql-1 -e DB_USER=gba_reco_ro -e DB_PASSWORD=... \
  -e REDIS_HOST=gba-procure-redis -p 8001:8001 gba-procure:latest
```

## 3. Smoke test
```bash
curl localhost:8001/health
curl -X POST localhost:8001/plan/producer -H 'Content-Type: application/json' \
  -d '{"producer_id": 365, "as_of_date": "2026-06-01", "only_needed": true}'
```
Returns: per-producer plan {lead_time, items[{product_id, suggested_qty, reorder_point,
safety_stock, days_of_cover, urgency, forecast, inventory}], ...}.

## 4. Pre-compute (worker)
```bash
python -m app.services.replenishment.worker --as-of 2026-06-01
```
Builds plans for all active producers into cache. Schedule daily/weekly.

## 5. Backtest (needs a demand-rich window)
```bash
python -m app.services.eval.backtest --producer 365 --as-of 2025-06-01 --window 90
```
Reports fill_rate, stockout_rate, overstock_rate, coverage with/without policy.
⚠️ Dev DB has almost no demand in any window (1 product of 11k) → backtest is mechanically
correct but statistically empty. Run on representative data to tune SERVICE_LEVEL/horizon.

## 6. Integration (gba-server)
gba-server calls via `IProcurementService` → `ProcurementApi:Url`. Endpoint: `POST /plan/producer`.
Maps to the .NET `SalesForecast`/procurement use case.

## Algorithm knobs to tune on real data
- Demand: swap `services/forecasting/demand.py` (moving-avg) for Croston/SBA on intermittent series.
- Lead time: empirical from SupplyOrder.Created→OrderArrivedDate; verify 209d dev value is real.
- Policy: SERVICE_LEVEL → safety-stock z; horizon → order-up-to level.
- Perf: forecast is currently N+1 per product — batch it before high-volume production use.

## Health / monitoring
- `GET /health` (db + redis), `GET /metrics`. Redis down → degrades gracefully.
