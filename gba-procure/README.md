# GBA Procurement / Replenishment Service

Service 2 of the GBA recommendation/procurement initiative. For each producer, suggests
WHAT to order, HOW MUCH, and WHEN — covering forecast demand over the producer's lead time
without over-stocking. Consumed by gba-server (.NET).

## Algorithm (baseline scaffold; pluggable for tuning on real data)
- **Demand forecast** (`services/forecasting/demand.py`): moving average over the effective
  source window, including real zero-days but never fabricated days before 2025-01-01.
  Returns mean+std/day.
  Swap-in target later: Croston/SBA, statsforecast, or LightGBM global model.
- **Lead time** (`services/forecasting/lead_time.py`): per-producer empirical mean+std from
  factual invoice/order dates to factual receipt dates; configurable fallback.
- **Replenishment policy** (`services/replenishment/policy.py`):
  - `reorder_point = mean_daily·LT + z(service_level)·√LT·std_daily`
  - `order_up_to   = reorder_point + horizon·mean_daily`
  - `position      = on_hand − reserved + on_order`
  - `suggested_qty = max(0, order_up_to − position)` when `position ≤ reorder_point`
  - urgency from days-of-cover vs lead time; items ranked critical-first.

## Data (read-only over ConcordDb_V5)
Factual international supply uses `SupplyInvoice/SupplyInvoiceOrderItem/PackingList/
ProductIncome`; domestic supply uses `SupplyOrderUkraine/SupplyOrderUkraineItem/
ProductIncome`. `ProductAvailability` and `ProductReservation` are current point-in-time
inputs. `Order/OrderItem` is the realized demand and revenue source.

### Source-history contract

- `SOURCE_HISTORY_START_DATE=2025-01-01` is the earliest supported 1C fact date.
- `as_of_date < 2025-01-01` is rejected with HTTP 422.
- Rolling demand, recorded sale, revenue and readiness windows are clamped to that floor.
  Purchase-cost observation scope is declared separately below.
- Lead times, agreement currency, MOQ and on-order facts use all available history in
  `[2025-01-01, as_of_date)`.
- Forecasting and XYZ receive `effective_history_days`; no pre-floor zero-days enter their
  denominator or dense series.
- Every plan/chart and source-readiness response exposes `source_history_start`,
  `effective_start`, `effective_history_days`, and `history_complete`.
- Inventory and reservations are current snapshots. Purchase-cost observations have a
  separate scope; all three inputs appear in `history_not_applicable` for demand coverage.

## Run
```bash
make install
cp .env.example .env   # fill DB_PASSWORD (read-only login)
make dev               # uvicorn on :8001
```

## API
- `POST /plan/producer` — `{producer_id, as_of_date?, only_needed}` → purchase plan.
- `POST /plan/cart` — canonical cross-producer replenishment cart.
- `POST /plan/charts` — dashboard chart payload.
- `GET /health`, `GET /metrics`.

## Security
- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader). Never `sa`. Secrets only via `.env`.

## Status
Forecast, replenishment and verified-cost paths have focused offline regression coverage.
A release additionally requires the independent schema2 reconciliation and live readiness
gates; a passing unit suite is not a claim of complete source-report or financial parity.

## Verified supplier purchase costs (contract v1)

Procurement costs come from the authenticated, read-only Analytics endpoint
`POST /internal/procure/purchase-cost-observations/v1`. Configure
`NATIVE_PURCHASE_COST_BASE_URL` and the dedicated `NATIVE_PURCHASE_COST_API_KEY`
(32–128 printable ASCII characters); it is separate from the public service key.
There is no invoice-price currency UDF fallback.

Each build reads the complete product union once, in disjoint batches of at most
1,000 products. The worker reuses that in-memory observation cohort for every producer,
cart and alternative. Each batch is a separate native snapshot; the manifest preserves
its actual product/date scope, read timestamps, publications and financial fingerprint.
An actual rebuild always revalidates immutable receipt payloads, including when a broad
source watermark is unchanged. Cached plans describe their captured input, not a fresh
proof read. The request queue has one worker and eight pending keys; concurrent requests
for the same exact key share work. Cold requests return HTTP 503/not_ready. Budget-only
variants use a complete validated captured cart and retain its manifest. API budget zero
continues to mean unbounded; the pure selector also supports an explicit zero ceiling.

The statistic is one observation per current posted receipt item, with an exact median
rounded HALF_UP to four decimal places. Scope is `[max(2025-01-01, as_of_date-540 days),
as_of_date)`; it is a current posted-record scope, not historical state reconstruction.
Prices are EUR net goods excluding VAT, delivery and customs. They are historical
supplier costs, **not a current offer or a customer-contract price**. Current product
units and all contributing exact supplier client-agreement IDs remain in per-line
`cost_provenance`. A partial median is visible with coverage reasons but excluded from
budget selection. Unknown and unsupported numeric precision remain unavailable.
Confirmed zero unit cost is valid for a positive suggested quantity; zero-demand rows
never become free orders. Manual buyer values retain their accepted input semantics
and remain tax-unverified, with no receipt proof inherited.

When masters are enabled, producer profiles and product terms are required reads.
Readiness checks collection access as well as Mongo connectivity; every actual build
and independent override read also fails closed if its read fails, including a cursor
interrupted after returning some rows. A valid empty collection or missing profile is
allowed. A missing configured store is unavailable when required; disabling masters
still explicitly omits those inputs. A failed read cannot remove a buyer override,
turn it into receipt-certified budget eligibility, or publish a new producer/cart plan.
The API keeps its existing `not_ready`/503 behavior and successful captured plans retain
the v11 cache contract. These checks prove successful reads, not a joint snapshot with
native receipt observations or immutability of buyer-maintained data.

Historical recorded sale prices lack a uniformly proven comparable tax basis, so
margin, value density and captured EUR profit are null. Service level uses the existing
ABC fallback and optional producer floor. Budget selection maximizes the explicitly
dimensionless `urgency_weighted_lines` objective using weights 1, 0.7, 0.4 and 0.1;
its declared cost basis is net goods, not a cash-payment obligation. Alternatives require
a usable receipt-history base and their own usable receipt-history cost, at least 2%
lower by exact canonical arithmetic. No comparison is made against a buyer override or
unknown base.

Producer, cart and charts expose `history_scope="demand"`; inventory, reservations and
purchase_costs are explicitly outside that demand-history coverage. Chart cost metadata
refers to its underlying full plan, not the top-items slice. Unbudgeted limited carts
retain full pre-limit totals and certification metadata. Budget carts are untruncated.

`python scripts/procure_reconcile.py` emits schema2. Inventory checks remain independent
SQL; cost medians, coverage, supplier agreements and eligibility are independently
recomputed from raw verified observations. Component fingerprint changes return
`source_not_ready` (`S004`); required transport or masters-read failures return `S003`. Stable plan
comparison excludes only observation/validation read timestamps, retaining exact
financial fingerprints, query scopes and immutable publication identity. Explicitly
unknown cost does not fail stock-example coverage checks. Partial/unknown costs still
cannot enter a budget.
