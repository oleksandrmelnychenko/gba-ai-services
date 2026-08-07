# GBA AI Services

AI/ML microservices for the GBA (Concord) ecosystem. Each is a self-contained FastAPI service
(Python 3.12, Pydantic v2, read-only SQLAlchemy over ConcordDb_V5, env-only secrets, Docker).

## Services

| Service | Default port | Operational endpoints |
| --- | ---: | --- |
| gba-reco | 8000 | `/health`, `/ready` |
| gba-procure | 8001 | `/health`, `/ready` |
| gba-solvency | 8003 | `/health`, `/ready` |
| gba-pricing | 8004 | `/health`, `/ready` |
| gba-products | 8005 | `/health`, `/ready` |
| gba-forecast | 8006 | `/health`, `/ready` |

> **gba-nba (порт 8002)** живе в окремому репозиторії
> [oleksandrmelnychenko/gba-nba](https://github.com/oleksandrmelnychenko/gba-nba) — AI Sales Cockpit /
> Next-Best-Action engine. Тут його більше немає, щоб не тримати два джерела для одного сервісу.

- **gba-reco** — client product recommendations (V3.2 hybrid: repurchase + co-purchase discovery),
  with an offline leave-last-basket eval harness and Redis caching.
- **gba-procure** — per-producer procurement / reorder-point purchase plans.
- **gba-solvency** — supervised credit-risk scoring (WOE scorecard + GBM challenger, SEV180 label) with a
  6-month forward early-warning, calibrated PD bands, drift monitoring and a gated retrain harness.
- **gba-pricing** — per-product price/discount recommendations from peer/segment price bands.
- **gba-products** — per-SKU assortment & inventory-health intelligence (lifecycle, ABC/XYZ, margin,
  returns, dead-stock, regional demand lens).
- **gba-forecast** — per-client/product sales demand forecasting (rolling-origin backtest, per-segment
  method selection: EWMA / SBA / moving-average).

Each service has its own README, pyproject.toml, Dockerfile, app/, tests/, docs/.
Secrets come from the environment only (see each service's `.env.example`); never commit `.env`.

## Source-history contract

All seven services use `SOURCE_HISTORY_START_DATE=2025-01-01`. Their `/health` and `/ready`
responses expose `source_history_start` and `source_history_contract_ready`; a configured date
other than `2025-01-01` makes business readiness fail closed. Nested source-readiness payloads also
repeat the date so the fleet release gate can detect partial or stale deployments.

## Integration
Orchestrated by gba-server (.NET), which proxies these services, injects the authenticated user from
the session, and surfaces them in the GBA Console (React).
