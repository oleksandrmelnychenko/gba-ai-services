# gba-products — Product Intelligence Service (SPEC, draft v1)

Per-SKU product intelligence for **assortment & inventory-health** decisions.
6th AI microservice. Port **8005**. Python/FastAPI, read-only ConcordDb_V5, internal-key auth — same
stack as gba-reco/procure/nba/solvency/pricing.

## 1. Purpose & consumer
Primary consumer: **purchasing / category managers**. The service answers, per product:
*what is the state/health of this SKU, and what assortment action does it imply* — keep, push,
discount/reallocate, dead-stock review, substitute, reorder/stop-reorder.

**Value anchor (decided):** *inventory-health by days-of-cover* (dead + slow + overstock +
understock/stockout-risk). EUR frozen-capital is **one ranking axis**, not the headline — it is small
on the dev mirror (€19.5k total / €401 truly-dead) but the engine scales: on a deeper (prod)
inventory the € axis amplifies on its own. Value does not depend on stock depth.

## 2. Boundary (no overlap with the existing 5)
| Service | Lens |
|---|---|
| reco | client → which products (this service may *consume* item-item co-purchase for substitution) |
| procure | supplier → how much to reorder (this service may *consume* its forecast; never recomputes it) |
| nba | manager → tasks |
| solvency | client → credit |
| **products** | **product → state / health / assortment action** |

products is **diagnostic / decision-support**, not replenishment-quantity. It owns: classification,
health-score, demand-score, margin-score, manager-facing action labels, inventory-health bands,
margin, returns, substitution ranking.

## 3. Data foundation (verified on ConcordDb_V5, read-only)

### 3.1 Canonical on-hand stock + EUR cost (the linchpin — resolved)
```sql
WITH OnHand AS (
    SELECT ci.ProductID,
           rsa.RemainingQty                                          AS qty,
           ci.Price                                                   AS eur_unit_cost,
           rsa.RemainingQty * ci.Price                                AS eur_value
    FROM dbo.ReSaleAvailability    rsa
    JOIN dbo.ConsignmentItem       ci ON ci.ID = rsa.ConsignmentItemID      -- authoritative ProductID
    JOIN dbo.ProductAvailability   pa ON pa.ID = rsa.ProductAvailabilityID  -- StorageID
    JOIN dbo.Storage               s  ON s.ID  = pa.StorageID
    LEFT JOIN dbo.Consignment      c  ON c.ID  = ci.ConsignmentID
    LEFT JOIN dbo.ProductIncome    pi ON pi.ID = c.ProductIncomeID
    WHERE rsa.Deleted = 0 AND rsa.RemainingQty > 0
      AND s.Deleted = 0 AND s.ForDefective = 0
      AND (s.AvailableForReSale = 1 OR s.IsResale = 1)
      AND (pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type)
)
```
- **Grain:** one RSA row = one cost layer of one ProductAvailability. Sum `RemainingQty` directly; do
  NOT also join `OrderItemID` / `ProductTransferItemID` / `ProductReservationID` (partial movement
  refs → row multiplication). `ConsignmentItem.ProductID` and `ProductAvailability.ProductID` agree on
  100% of on-hand rows.
- **EUR unit cost = `ConsignmentItem.Price` directly.** `ci.Price` is already EUR and matches the
  sibling pricing service; `RSA.PricePerItem` is UAH and dividing `ci.Price` by `ExchangeRate`
  under-scales by ~50x.
- **1C debt-import lots are excluded.** `ProductIncome.SourceDocumentType = 1` carries inflated
  balance-import costs, so stock quantity/value and derived margin exclude those lots.
- **Warehouse scope:** `Deleted=0 AND ForDefective=0 AND (AvailableForReSale=1 OR IsResale=1)` →
  the two real resale warehouses (2197 "СКЛАД -3", 2191 "СКЛАД -1"); excludes markdown/restoration/БРАК.

### 3.2 CRITICAL time-window rule (applies to every lens)
Use **`Order.Created`** for all sales/time windows. **`OrderItem.Created` is truncated** (spans only
~3 days) and must never be used for velocity/recency/trend. (The other services already use Order.Created.)

### 3.3 Per-lens sources
| Lens | Sources |
|---|---|
| Velocity / trend / recency | `OrderItem` × `Order.Created` |
| Regional demand | `OrderItem` × `Order.Created` × `ClientAgreement.ClientID → Client.RegionID` |
| Stock / days-of-cover / € | §3.1 canonical query |
| Margin | sale price `OrderItem.PricePerItem` (already EUR) − EUR unit cost (§3.1) |
| Returns | `SaleReturnItem.OrderItemID → OrderItem → Product`; reasons `SaleReturnItemStatus`; `IsMoneyReturned` |
| Substitution | `Product.HasAnalogue`, `ProductCarBrand`/`CarBrand` (fitment), `ProductProductGroup`/`ProductGroup`, reco co-purchase |

### 3.4 Scale (dev)
Catalog 373,741 (Deleted=0) · sold last 12mo 12,935 · **on-hand stock 4,634 SKUs / 50,436 units /
€19,505** · dead (0 sales 12mo, on-hand) 131 / €401 · slow (1–5/yr) 1,316 · healthy (6+) 3,187.

## 4. The four lenses (MVP)

### Lens 1 — Health-score (0–100) + classification (spine)
- **ABC** by cumulative trailing revenue contribution (A=top 80%, B=next 15%, C=last 5%). This is
  deliberately revenue-based: purchase cost only exists for stocked SKUs, so a margin-ABC would drop
  every order-to-demand SKU.
- **XYZ** by demand cadence variability over the trailing monthly grid.
- **Lifecycle**: new (<90d since first sale) / growing / mature / declining / dead — from trend + recency + age.
- **Health-score** = weighted triage blend (weights env-tunable, A/B-versioned like the siblings):
  stock-balance + lifecycle trend + margin + demand-stability + return-rate penalty + ABC/trailing
  revenue. `products-v2-abc` adds the ABC component because 2025-06/09/12 real-data snapshots showed
  trailing revenue was the strongest repeat-demand signal. It is still a triage composite, not a
  calibrated probability.
- **Demand-score** separates future-sales potential from the generic health blend. It weights ABC /
  trailing revenue, demand stability, lifecycle trend, and stock state; the committed baseline shows
  Spearman demand→future revenue `0.4716`, units `0.3865`.
- **Margin-score** separates profitability / quality from demand. It weights margin%, return-rate
  drag, and a small ABC prior; the committed baseline shows Spearman margin-score→future margin
  `0.5313`.
- **Action-label** is deterministic and manager-facing. It emits review/action buckets such as
  `fix_margin`, `quality_review`, `dead_stock_review`, `discount_or_redistribute`, `reorder_check`,
  `to_order_candidate`, `margin_review`, `keep_push`, `monitor_decline`, and `monitor`. Dead stock is
  reviewed, not auto-discontinued; order-to-demand candidates are not treated as margin-validated push
  tasks when cost is unknown.
- **Regional demand lens** is an overlay, not a separate model score. It uses the oblast-level
  `Client.RegionID` through `Order.ClientAgreementID → ClientAgreement.ClientID`, never
  `RegionCodeID` (address-code granularity). Use it to scope demand evidence and regional top SKUs.
- Current committed baseline (`docs/product-health-backtest-baseline.json`, as_of `2025-12-01`,
  +180d): Spearman health→future revenue `0.3874`, units `0.3217`, margin `0.4201`; top/bottom
  revenue lift `10.696`.

### Lens 2 — Inventory-health by days-of-cover (the anchor)
- `cover_days = on_hand_qty / daily_demand_rate` (rate from trailing 90/180d via Order.Created).
- Bands: **dead** (0 sales 12mo, on-hand>0; cover→∞), **overstock** (cover > overstock_days),
  **healthy** (target_min ≤ cover ≤ target_max), **understock** (cover < reorder_days),
  **stockout-risk** (on_hand≈0 but active demand), **slow** (low absolute velocity 1–5/yr).
- Ranked by **€ frozen** AND **units** AND **margin-at-risk**; action = discount / redistribute /
  dead-stock review / stop-reorder.
- Thresholds (overstock_days, target band, slow cutoff) = env-tunable, calibrated on real data.
- **Seasonality guard:** look back 24mo; distinguish "never sold" vs "stopped selling"; do not flag a
  seasonal lull as dead.

### Lens 3 — Substitution / analogues
Candidates: `HasAnalogue` + fitment overlap (`ProductCarBrand`) + same `ProductGroup` + reco
co-purchase → filter **in-stock + healthy** → rank by fitment/affinity. Use: OOS or declining SKU.

### Lens 4 — Margin + returns
- Margin% = (avg EUR sale price − EUR unit cost)/sale price; flag negative/thin; leaders/laggards by € contribution.
- Return-rate = returned qty / sold qty (window); reasons; money-returned share; flag problem SKUs.

## 5. API
- `GET /product/{id}` — full 360 profile (all lenses), including `health`, `demand_score`,
  `margin_score`, `action_label`, component breakdowns — the per-SKU card.
- `GET /assortment/health` — ranked list (filters: band, ABC/XYZ, lifecycle; sort:
  `health_asc`, `demand`, `margin`, `frozen_eur`, `revenue`; optional `region_id` overlay with
  `regional_revenue` / `regional_units` sorts) — purchasing dashboard.
- `GET /assortment/overview` — portfolio summary (lifecycle/ABC-XYZ/action distribution, € frozen,
  top decliners/overstock).
- `GET /assortment/regions` — regional demand summary by `Client.RegionID`.
- `GET /product/{id}/regions` — per-SKU regional demand split.
- `GET /product/{id}/substitutes`.
- `GET /health`, `GET /metrics` — standard; `/health` open, rest internal-key gated.

## 6. Architecture
Mirror gba-procure (closest analytically). Layout:
```
app/api/main.py
app/core/{config.py,logging.py,metrics.py}
app/data/{db.py,signals_repository.py,cache.py}
app/domain/models.py
app/services/{classification,stock_health,margin_returns,substitution,health_score,profile}.py
app/clients/{reco_client,procure_client}.py        # optional consume, graceful-degrade
tests/  scripts/  .env.example
```
- Stateless + cache (key `products:{ver}:{kind}:{id}:{as_of}`); MODEL_VERSION env
  (`products-v2-abc`), bump on scoring change.
- Optional scheduler: nightly precompute of `/assortment/*` rankings (warm cache over ~4.6k stocked + classify ~13k active).
- Reuse the siblings' EUR-correct discipline; share the read-only `gba_reco_ro` login.
- Deploy: systemd unit `gba-products.service` (uvicorn `app.api.main:app` :8005), `Restart=always`,
  log `/var/log/gba-products.log` — same pattern as the other 5.

## 7. Build phases
1. **Scaffold** + canonical `signals_repository` (stock query §3.1, velocity, margin, returns) + tests.
2. **MVP** = Lens 1 (health/classification) + Lens 2 (inventory-health) → `/product/{id}`, `/assortment/health`, `/assortment/overview`.
3. Lens 3 (substitution) + Lens 4 (margin/returns full).
4. gba-server proxy routes (GATED — only when user signals) + console assortment dashboard.

## 8. Open calibration / risks
- **Thresholds and weights** (overstock_days, target band, slow cutoff, health weights) — calibrate
  with `scripts/product_health_backtest.py`; no guessing. The backtest snapshots `/assortment`
  at date T and measures future demand / return-adjusted margin over T..T+H.
- **Aging** — confirm `ReSaleAvailability.Created` vs `ConsignmentItem.Created`/`ProductIncomeItem` reflects true goods-receipt date before exposing days-in-stock.
- **dev vs prod depth** — engine is depth-agnostic; € axis is secondary on dev, amplifies on prod.
- **Non-goals:** no replenishment quantities (procure), no client-level recs (reco), no write-backs (read-only).
