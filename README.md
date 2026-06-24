# GBA AI Services

AI/ML microservices for the GBA ecosystem. Each service is a self-contained FastAPI app with env-only secrets, typed contracts, tests, Dockerfile, and internal-key protection for non-health endpoints.

## Services

- `gba-reco` - client product recommendations and co-purchase discovery.
- `gba-procure` - procurement and replenishment planning.
- `gba-nba` - Sales Cockpit / next-best-action task engine.
- `gba-solvency` - client solvency scoring and charts.
- `gba-pricing` - price and discount recommendation.

## Integration

`gba-server` proxies these services and passes `X-Internal-Api-Key` from its configuration. In dev, the service ports are:

- `8000` - `gba-reco`
- `8001` - `gba-procure`
- `8002` - `gba-nba`
- `8003` - `gba-solvency`
- `8004` - `gba-pricing`

Use each service's `.env.example` as the configuration template. Never commit `.env`.
