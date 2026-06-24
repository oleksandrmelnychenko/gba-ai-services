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

## Release Gates

Run static checks from the repository root after each service has its `.venv` installed:

```bash
make static-check
```

`static-check` runs every service's ruff check and non-integration pytest suite.

Run the full release gate only with dev/live dependencies available and DB credentials in env or service `.env` files:

```bash
DB_HOST=127.0.0.1 \
DB_PORT=1433 \
DB_NAME=ConcordDb_V5 \
DB_USER=gba_reco_ro \
DB_PASSWORD=... \
make release-check
```

`release-check` runs `static-check` plus the bounded calibration gates:

- `gba-reco`: offline eval baseline, `--baseline --limit 120`.
- `gba-procure`: procurement backtest sweep.
- `gba-nba`: live-signal census check.

Run extended live checks only when the dev dependencies are available and configured: ConcordDb_V5 read-only SQL login, Redis for reco/procure/solvency/pricing, MongoDB for NBA, and service `.env` files.

```bash
make live-check
```

`live-check` runs integration tests and the services that have smoke scripts (`gba-pricing`, `gba-reco`, `gba-solvency`). For NBA/procure, use their calibration targets as the live readiness signal.

Before tagging a server release, also build `gba-server` and verify the proxy config:

- `RecommendationApi`, `ProcurementApi`, `GbaNbaApi`, `SolvencyApi`, `PricingApi` URLs point to ports `8000`-`8004`.
- The same `X-Internal-Api-Key` is configured in `gba-server` and each AI service.
- `/health` is reachable for every service, and non-health endpoints reject requests without the internal key.
