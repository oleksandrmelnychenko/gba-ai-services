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
  -e REDIS_HOST=gba-reco-redis -p 127.0.0.1:8000:8000 gba-reco:latest
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
make worker        # or: python -m app.services.recommendations.worker [--engine legacy]
```
In the fleet this runs daily through `refresh_ai_service.py reco --as-of <day> --token <run>`;
a re-run with the same token reuses every payload already written. Idempotent and resumable.

- `REFRESH_ENGINE=snapshot` (default): set-based load, in-memory compute, about two minutes.
- `REFRESH_ENGINE=legacy`: per-context SQL loop, about 50 minutes; fallback for one release.
- `SERVE_LATEST_FALLBACK=true`: serve the latest warmed payload while the day's refresh has
  not run, marked `stale: true`, for at most `LATEST_FALLBACK_MAX_AGE_DAYS` (2) days. Needs a
  gba-server build that accepts an `as_of_date` older than requested.
- After a model-version change run the reco stage, then `systemctl restart gba-reco`.
  Payloads of older versions and dates can be removed with `cache.purge_dated_before(as_of)`;
  it never touches `reco:neg:*`.

## 5. Evaluate / tune (needs representative data)
```bash
python -m app.services.eval.holdout --origins 2026-05-13,2026-06-13,2026-07-13,2026-08-13,2026-09-13,2026-10-13
python -m app.services.eval.holdout --origins <dates> --write-measured   # refresh precision_estimate
python -m app.services.eval.harness --compare --k 10        # older leave-last-basket harness
```
Choose on the earlier origins, confirm on the last two (`--confirm 2`), and record the tables
in `docs/eval-<date>.md`. `measured.json` must carry the served model version; a guard test
fails otherwise.
Truth is what a client ordered and was invoiced for inside the window, net of returns;
orders already open at the origin are excluded from both the list and the truth.

## 6. Integration (gba-server)
gba-server (development) calls this via `IProductRecommendationService` →
`RecommendationApi:Url` in appsettings. Endpoints used: `POST /recommend`, `POST /recommend/batch`, `GET /health`.

## Health / monitoring
- `GET /health` → db_connected, redis_connected, model_version.
- `GET /metrics` → requests, error_rate, avg_latency_ms, cache_hit_rate.
- Redis down → every request is calculated from the database (seconds instead of
  milliseconds); `/health` reports `redis_connected: false`.
- Database slow or down → cached lists are served with `degraded: true` and a
  `degraded_reason`; anything not cached answers 503 `source_unavailable` with the reason
  (`database_timeout` after `QUERY_TIMEOUT` seconds, `database_unavailable` otherwise).
- Measured latency and memory: `docs/eval-2026-11-13.md`.

## Rollback
Stateless service; roll back by redeploying the previous image tag. Cache auto-expires (TTL).
