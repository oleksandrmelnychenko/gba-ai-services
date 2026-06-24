# gba-reco — Deployment & Operations Runbook

Client product recommendation service (Service 1). FastAPI + Redis, read-only over ConcordDb_V5.

## Prerequisites
- Docker + docker compose, OR Python 3.12 + FreeTDS (`apt install freetds-dev`).
- A **read-only** SQL login on ConcordDb_V5 (never `sa`). Dev login already created: `gba_reco_ro`.
- Network reachability to the DB host (dev: container `gba-dev-gba-mssql-1` on `gba-dev_default`).

## 1. Configure
```bash
cp .env.example .env
# edit .env — set at minimum:
#   DB_HOST=...           (dev: gba-dev-gba-mssql-1 if on same docker net, else 127.0.0.1)
#   DB_USER=gba_reco_ro
#   DB_PASSWORD=<the read-only password>
#   REDIS_HOST=redis      (compose) or 127.0.0.1 (local)
```
Secrets live ONLY in `.env` (gitignored). Nothing hardcoded.

## 2a. Run with Docker (recommended)
```bash
docker compose up -d --build      # redis + api on :8000
docker compose logs -f api
```
To reach the dev DB by container name, run the api on the DB's network:
```bash
docker run -d --name gba-reco-api --network gba-dev_default \
  -e DB_HOST=gba-dev-gba-mssql-1 -e DB_USER=gba_reco_ro -e DB_PASSWORD=... \
  -e REDIS_HOST=gba-reco-redis -p 8000:8000 gba-reco:latest
```

## 2b. Run locally
```bash
make install
make dev          # uvicorn :8000
```

## 3. Smoke test
```bash
curl localhost:8000/health
curl -X POST localhost:8000/recommend -H 'Content-Type: application/json' \
  -d '{"customer_id": 411133, "top_n": 10, "as_of_date": "2026-06-01"}'
curl localhost:8000/metrics
```

## 4. Pre-compute cache (worker)
```bash
make worker        # or: python -m app.services.recommendations.worker --top-n 50
```
Schedule weekly via host cron / k8s CronJob. Idempotent & resumable.

## 5. Evaluate / tune (needs representative data)
```bash
make calibration                                      # quick committed gate: --baseline --limit 120
python -m app.services.eval.harness --baseline --k 10 # full audit gate
python -m app.services.eval.harness --compare --k 10        # v3.2 vs copurchase vs naive
python -m app.services.eval.harness --fold-as-of 2025-09-01 # + ALS (trained once)
```
⚠️ On the small dev DB all algos tie / lose to global_popular (84% cold-start clients).
Run these against a representative (prod-like) dataset to pick the winning algorithm.
**Rule:** any algorithm must beat `naive_global_popular` to ship.

## 6. Integration (gba-server)
gba-server (development) calls this via `IProductRecommendationService` →
`RecommendationApi:Url` in appsettings. Endpoints used: `POST /recommend`, `POST /recommend/batch`, `GET /health`.

## Health / monitoring
- `GET /health` → db_connected, redis_connected, model_version.
- `GET /metrics` → requests, error_rate, avg_latency_ms, cache_hit_rate.
- Redis down → service degrades gracefully (uncached, still serves). DB down → /health degraded.

## Rollback
Stateless service; roll back by redeploying the previous image tag. Cache auto-expires (TTL).
