# GBA Pricing Service

B2B price/discount optimization for GBA. Service 5 of the GBA AI family
(8000 reco, 8001 procure, 8002 nba, 8003 solvency, **8004 pricing**). Consumed by gba-server
(.NET) and the console. Mirrors the hardened infra of gba-solvency / gba-reco: read-only SQL
login, parameterized SQL, env-only secrets, structured JSON logging, thread-safe metrics,
graceful Redis degradation.

## Model — A+B (`pricing-ab-v2`)

Per product `p` × client-agreement `a`, recommend a price/discount that **protects margin** and
stays **within peer norms**, by **adjusting the existing price engine's DiscountRate lever** —
never replacing the engine.

### The engine is the baseline (never replaced)

```
baseline_price = dbo.GetCalculatedProductPriceWithSharesAndVat(
    @ProductNetId=Product.NetUID, @ClientAgreementNetId=ClientAgreement.NetUID,
    @Culture, @WithVat, @OrderItemId=NULL) -> decimal(30,14)
```

Engine formula (reference only; we do not re-implement it):
```
marked_up = ROUND(P + P*ExtraCharge/100, 14)              # P = ProductPricing.Price at
                                                          #   dbo.GetBasePricingId(Agreement.PricingID)
                                                          # ExtraCharge = Pricing.CalculatedExtraCharge
baseline_price = marked_up * (1 - DiscountRate/100) * (1 - OneTimeDiscount/100)
                                                          # DiscountRate = ProductGroupDiscount (IsActive=1)
```

### A — margin floor + peer band

```
unit_cost_eur  = robust per-product cost = MEDIAN of ConsignmentItem.AccountingPrice over on-hand
                 lots (Deleted=0, AccountingPrice>0, RemainingQty>0, ProductID<>25422404);
                 fallback = latest-lot TOP 1 AccountingPrice ORDER BY ID DESC. AccountingPrice is
                 EUR-base. No cost lot -> unit_cost_eur=null, confidence low, skip the floor.
price_floor    = unit_cost_eur * (1 + target_margin_pct/100)   # config default 12; never below
peer_band      = P25/P50/P75 + n of realized EUR unit price over distinct client-agreements for p
                 in the trailing window (EUR_price = Agreement.CurrencyID=2 ? OrderItem.PricePerItem
                 : GetExchangedToEuroValue(PricePerItem, Agreement.CurrencyID, Sale.Created); UoM
                 outliers decile-trimmed).
recommended_price = clamp( max(price_floor, peer_P50), lower=price_floor, upper=baseline_price )
                 # never above the engine's current price; if floor>baseline -> LOSS FLAG
                 #   (recommended=price_floor, rationale='below-margin-loss-flag').
```

### B — discount discipline

```
suggested_discount_pct = (1 - recommended_price / ROUND(P + P*ExtraCharge/100, 14)) * 100
                 # the DiscountRate that reproduces recommended_price THROUGH the engine,
                 # capped at peer P75 (hard-capped at P90) of ProductGroupDiscount.DiscountRate
                 # within the segment = (ProductGroupID × base-tier-family via GetBasePricingId × Culture).
discount_band  = { min_pct: floor-implied discount, target_pct: suggested, max_pct: peer P90 cap }
```

`confidence` = `high` if cost lots>=3 AND peer n>=10; `low` if no cost OR peer n<3; else `medium`.
`margin_pct_at_recommended` = `(recommended_price - unit_cost_eur)/recommended_price*100`.
`rationale` names the binding constraint (`margin-floor`, `peer-median`, `discount-cap`,
`below-margin-loss-flag`, `at-baseline`).

The optimizer emits its result as an **adjustment to the engine's DiscountRate lever**, so the live
`GetCalculatedProductPriceWithSharesAndVat` remains the single source of truth.

## Critical data traps (honored in `pricing_repository.py`)

- **NEVER** filter `Deleted=0` on `Sale`/`Order`/`OrderItem` (=1 on 100% of rows). Validity comes
  from `OrderItem.IsValidForCurrentSale=1`.
- Exclude `ProductID 25422404` ('Ввід боргів з 1С' synthetic line) from cost lots, peer band and
  the discount distribution — it contaminates cost and realized price.
- Cost via `ConsignmentItem.AccountingPrice` (Deleted=0, AccountingPrice>0, RemainingQty>0) using
  **MEDIAN** to guard against debt/correction lots (~800-1160 on cheap SKUs); fallback = latest lot.
  `AccountingPrice` is **EUR-base** (FX baked in at income) — no `GetExchangedToEuroValue` applied.
- Pin the **FX snapshot date** per run (`GetExchangedToEuroValue` revalues at call time): revenue
  pinned to `Sale.Created`. Configured via `FX_SNAPSHOT_DATE` / `as_of_date`.
- `OrderItem.DiscountAmount` is a **line-total** money figure (not per-unit) → not used; discount
  discipline comes from `ProductGroupDiscount.DiscountRate` (the engine-native lever).
- `ProductPricing` has 23x soft-deleted bloat → always filter `Deleted=0` on `ProductPricing`.
- UoM piece-vs-box outliers → peer percentiles decile-trim.
- **No win/loss / offer-conversion data** (offers empty) → NO elasticity / win-rate in A+B.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # fill DB_PASSWORD with the read-only login
.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8004
```

## API

- `POST /price` — `{product_id | product_net_uid, client_agreement_net_uid, culture?='uk',
  with_vat?=true, target_margin_pct?}` → `PriceRecommendation`.
- `POST /price/batch` — `{items:[{product_net_uid, client_agreement_net_uid}], ...}` → list of
  `PriceRecommendation` (errors isolated).
- `GET /health`, `GET /metrics`.
- `DELETE /cache/{product}/{client_agreement_net_uid}`.

`PriceRecommendation` = `{product_id, client_agreement_netuid, currency:'EUR', baseline_price,
recommended_price, price_floor, unit_cost_eur, suggested_discount_pct,
discount_band:{min_pct,target_pct,max_pct}, peer_band:{p25,p50,p75,n}, confidence,
margin_pct_at_recommended, rationale, as_of_date, model_version:'pricing-ab-v2'}`.

## Security

- Dedicated **read-only** SQL login (`gba_reco_ro`, db_datareader + EXECUTE on the price fns).
  Never `sa`.
- Secrets only via `.env` (gitignored). No credentials in code.

## Status

Implemented: config (port 8004, Redis db 3, `pricing-ab-v2`, target_margin_pct=12), pooled
read-only DB layer, parameterized pricing repository, A+B optimizer, optional gated elasticity
signal, domain models, FastAPI shell, tests.
