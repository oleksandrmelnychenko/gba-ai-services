# GBA Client Recommendation Service

B2B product recommendation service — for each client, top-N products to (re)buy.
Service 1 of the GBA recommendation/procurement initiative. Consumed by gba-server (.NET).

Ground-up rebuild (June 2026) of the `bi-server-concord` V3.2 prototype: same algorithm,
hardened — read-only DB login, parameterized SQL, env-only secrets, typed contracts.

## Algorithm (V3.2 hybrid)
- **Repurchase**: segment-weighted frequency × recency over the client's own history.
- **Discovery**: collaborative filtering (Jaccard-similar clients) for new products.
- **Mix**: 20 repurchase + 5 discovery (configurable), max 3 per product group.
- **Segments**: HEAVY (≥500 orders) / REGULAR-CONSISTENT / REGULAR-EXPLORATORY (100–500) / LIGHT (<100).

## Data
Read-only over ConcordDb_V5: `ClientAgreement → Order → OrderItem`, groups via `ProductProductGroup`.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # fill DB_PASSWORD with the read-only login
.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000
# smoke test:
.venv/bin/python scripts/smoke_test.py
```

## API
- `POST /recommend` — `{customer_id, top_n, as_of_date?, include_discovery}` → recommendations.
- `GET /health`.

## Security
- Uses a dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader only). Never `sa`.
- Secrets only via `.env` (gitignored). No credentials in code (unlike the prototype).

## Status
Phase 1 scaffold complete: config, pooled read-only DB layer, parameterized repository,
V3.2 recommender, FastAPI, smoke-tested on live dev DB. Next: eval harness (time-split
precision@K), Redis cache + worker, gba-server contract lock.
