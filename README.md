# GBA AI Services

AI/ML microservices for the GBA (Concord) ecosystem. Each is a self-contained FastAPI service
(Python 3.12, Pydantic v2, read-only SQLAlchemy over ConcordDb_V5, env-only secrets, Docker).

## Services
- **gba-nba** — AI Sales Cockpit / Next-Best-Action engine. A prioritized daily task queue per sales
  manager (debt follow-up, reorder, churn win-back, cross-sell), stateful in MongoDB, with a run-rate
  sales-target engine, a daily scheduler (09:00 Europe/Kyiv), and a head-of-sales dashboard.
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

## Integration
Orchestrated by gba-server (.NET), which proxies these services, injects the authenticated user from
the session, and surfaces them in the GBA Console (React).
