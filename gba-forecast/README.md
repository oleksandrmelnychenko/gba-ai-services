# GBA Forecast

Read-only sales forecast service for the `gba-server` `/sales/prediction/get` proxy.

## Runtime

- Port: `8006`
- App: `app.api.main:app`
- Health: `GET /health`
- Readiness: `GET /ready`
- Metrics: `GET /metrics`
- Forecast: `GET /forecast/sales?client_net_id=<uuid>&product_net_id=<uuid>&months=6`

`/forecast/sales`, `/metrics`, and `/ready` are protected by `X-Internal-Api-Key` when
`INTERNAL_API_KEY` is set. `/health` stays open for liveness checks.

## Required Release Config

Use a read-only SQL login. Do not run with `sa`.

```env
DB_HOST=127.0.0.1
DB_PORT=1433
DB_NAME=ConcordDb_V5
DB_USER=gba_reco_ro
DB_PASSWORD=...
INTERNAL_API_KEY=...
ALLOW_OPEN_INTERNAL_API=false
FORECAST_HORIZON_MONTHS=6
MAX_FORECAST_HORIZON_MONTHS=24
HISTORY_MONTHS=24
FORECAST_METHOD=auto
MIN_HISTORY_MONTHS=3
```

Local/dev may set `ALLOW_OPEN_INTERNAL_API=true` only when `INTERNAL_API_KEY` is intentionally
empty. Shared environments should fail startup if the internal key is missing.

`FORECAST_METHOD=auto` selects the method per demand segment: smooth series use EWMA, erratic
series use the window mean, intermittent/lumpy series use SBA, and no-demand series stay on the
safe moving average. `MIN_HISTORY_MONTHS=3` suppresses forecasts for keys with fewer than three
non-zero history months.

## Deploy

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[dev]'
sudo cp deploy/gba-forecast.service /etc/systemd/system/gba-forecast.service
sudo systemctl daemon-reload
sudo systemctl enable --now gba-forecast
```

## Verify

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_backtest.py --json
curl -sS http://127.0.0.1:8006/health
curl -sS http://127.0.0.1:8006/ready -H "X-Internal-Api-Key: $INTERNAL_API_KEY"
```

Expected forecast response shape:

```json
{
  "ByClient": [{"SaleAmount": 23.9, "MonthNameUK": "Лип 2026"}],
  "ByProduct": [],
  "ByClientAndProduct": []
}
```
