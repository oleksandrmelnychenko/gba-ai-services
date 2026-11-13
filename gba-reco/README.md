# GBA Client Recommendation Service

B2B product recommendation service — top-N products to (re)buy by client agreement.
Service 1 of the GBA recommendation/procurement initiative. Consumed by gba-server (.NET).

Ground-up rebuild (June 2026) of the `bi-server-concord` V3.2 prototype: same algorithm,
hardened — read-only DB login, parameterized SQL, env-only secrets, typed contracts.

## Algorithm (V3.2 hybrid)
- **Repurchase**: segment-weighted frequency × recency over the selected agreement's history.
- **Discovery**: collaborative filtering (Jaccard-similar clients) for new products.
- **Mix**: 20 repurchase + 5 discovery (configurable), max 12 per product group
  (`MAX_PER_GROUP`; chosen by the holdout eval, see `docs/eval-2026-11-12.md`).
- **Backfill**: short lists are filled from co-purchase, then from in-stock popular products.
- **Segments**: HEAVY (≥500 orders) / REGULAR-CONSISTENT / REGULAR-EXPLORATORY (100–500) / LIGHT (<100).
- **History contract**: transactional signals start at `SOURCE_HISTORY_START_DATE`
  (`2025-01-01`); earlier `as_of_date` requests are rejected.

## What counts as a purchase
One module, `app/data/source_rules.py`, defines it for every query (history, co-purchase,
popularity, "already bought", active clients, evaluation truth). It is gba-server's confirmed
sale: `Sale.Deleted = 0`, `ChangedToInvoice IS NOT NULL`, not an accounting carrier, line
`Deleted = 0`, `IsValidForCurrentSale = 1`, `Qty > 0`, dated by `ChangedToInvoice` on
Europe/Kyiv days, with buyer returns netted on the returned line (a line returned in full is
not a purchase). A guard test fails if any other module names the sales tables.

## What a list may contain
- Live cards only, with stock the context can sell: for an agreement, the Ukrainian sale
  buckets of that agreement; without one, any resale storage. Storages marked not for sale
  (`ForDefective`) never count.
- One card per article. Sibling cards (gba-server's rule: equal meaningful `VendorCode` on at
  most four live cards with the same first word of the name, plus `ProductSiblingLink`) are
  one article; when the bought card has no stock, the sibling that has it is shown instead.
- No services (native service flag, name «Послуг…», unit «послуга») and no synthetic lines.
- Nothing from the client's open orders: sales created in the last `OPEN_ORDER_DAYS` (30) and
  not invoiced yet are the basket, not history.
- «Повторна закупівля» means the client bought the article in a confirmed sale under any of
  its agreements. Every other label means the client never did. Ranking still uses the
  selected agreement's history only.
- A client without history gets the 25 most sold in-stock products (label «Популярний товар»);
  the list is the same for every such client with the same stock buckets.

## Daily refresh
The fleet runs `worker.run(as_of, refresh_token)` once a day. The default engine
(`REFRESH_ENGINE=snapshot`) loads the sales spine, stock, agreements and product groups with a
handful of set-based queries and computes every context in memory (about two minutes for
1,900 contexts). `REFRESH_ENGINE=legacy` runs the old per-context SQL loop (about 50 minutes)
and is kept for one release as a fallback. Both write the same payloads under a dated key and a
dateless `latest` key, with a 36-hour TTL.

With `SERVE_LATEST_FALLBACK=true` a request whose dated key does not exist yet (after midnight,
before the refresh) is answered from the `latest` key with the snapshot's true `as_of_date`
and `stale: true`. A snapshot older than `LATEST_FALLBACK_MAX_AGE_DAYS` (2, the age gba-server
accepts) is not served; the list is calculated on request instead.

When the sales database is slow or unreachable, a cached list is still served, marked
`degraded: true` with `degraded_reason` (`database_timeout` or `database_unavailable`).
Without a cached list the answer is HTTP 503 `{"code": "source_unavailable", "reason": ...}`.
When Redis is down, every request is calculated from the database.

## Evaluation
`python -m app.services.eval.holdout --origins <dates>` scores the served list on a temporal
holdout and compares parameter variants; `--write-measured` stores the measured precision in
`app/services/eval/measured.json`, which every response reports as `precision_estimate`.
Decision records live in `docs/eval-*.md`. Keep `--processes` low on a shared host: each
worker holds one snapshot and every client's candidate lists in memory.

## Data
Read-only over ConcordDb_V5: `Sale → Order → ClientAgreement / OrderItem` with
`SaleReturn(Item)` and `DataSyncAccountingSourceTarget`, stock via `ProductAvailability` and
`Storage`, groups via `ProductProductGroup`.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # fill DB_PASSWORD with the read-only login
.venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# smoke test:
.venv/bin/python scripts/smoke_test.py
# offline regression gate:
.venv/bin/python -m app.services.eval.harness --baseline --k 10
```

## API
- `POST /recommend` — `{customer_id, client_agreement_net_id, top_n, as_of_date?, include_discovery}` → recommendations.
  Responses expose `source_history_start`, `effective_start`, `history_complete`,
  `as_of_date`, `stale`, `degraded` and `degraded_reason`.
- `POST /recommend/copurchase` accepts the same agreement and optional `product_ids` seeds.
- Agreement identity, history, stock, pricing and daily refresh: [contract](docs/agreements.md).
- `GET /health`.

## Security
- Uses a dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader only). Never `sa`.
- Secrets only via `.env` (gitignored). No credentials in code (unlike the prototype).

## Status
Served model version: `v42-confirmed-sales-history-floor-20250101`. The current measurements
are in `docs/eval-2026-11-13.md`; `docs/eval-2026-11-12.md` is the v41 decision record on the
previous purchase definition, and `docs/eval-baseline.md` is the June 2026 leave-last-basket
baseline kept for history.
