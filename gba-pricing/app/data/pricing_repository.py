"""Parameterized read queries for the gba-pricing A+B engine.

All SQL is parameterized (:name) — no f-string interpolation. Every query honors the
discovery data traps (/tmp/pricing-discovery.json):
  (a) NEVER filter Deleted=0 on Sale/Order/OrderItem (=1 on 100% of rows). Validity comes from
      OrderItem.IsValidForCurrentSale=1.
  (b) Synthetic 1С debt-entry line (ProductID 25422404) is EXCLUDED everywhere (cost lots,
      peer band, discount distribution) — it contaminates cost and realized price.
  (c) FX snapshot date is pinned per run (GetExchangedToEuroValue revalues at call time): the
      revenue date pin = Sale.Created; the engine passes a deterministic fx_date.
  (d) Cost via ConsignmentItem.AccountingPrice (Deleted=0, AccountingPrice>0, RemainingQty>0):
      MEDIAN on-hand lot guards against debt/correction lots (~800-1160 on cheap SKUs);
      fallback = latest-lot TOP 1 AccountingPrice ORDER BY ID DESC. AccountingPrice is EUR-base.
  (e) DiscountAmount is a line-total money figure (not per-unit) -> NOT used; discount discipline
      comes from ProductGroupDiscount.DiscountRate (the engine-native lever).
  (f) ProductPricing has 23x soft-deleted bloat -> always filter Deleted=0 on ProductPricing.
  (g) UoM piece-vs-box outliers -> peer percentiles reject lines by a per-product median/MAD
      modified-z filter (k=3.5), which adapts to the contaminated fraction (a fixed decile trim
      leaked when >10% of lines were the wrong UoM).

The baseline price is computed by the live engine itself
(dbo.GetCalculatedProductPriceWithSharesAndVat), never re-derived here.
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.data.db import query


def resolve_product(product_id: int | None, product_net_uid: str | None) -> dict[str, Any] | None:
    """Resolve a product to {id, net_uid} from either ID or NetUID. The engine functions key on
    NetUID; the cost/peer queries key on ID — so both are always carried.
    """
    if product_id is not None:
        rows = query(
            "SELECT TOP 1 ID, NetUID FROM dbo.Product WHERE ID = :pid",
            {"pid": product_id},
        )
    elif product_net_uid is not None:
        rows = query(
            "SELECT TOP 1 ID, NetUID FROM dbo.Product WHERE NetUID = :uid",
            {"uid": product_net_uid},
        )
    else:
        return None
    if not rows:
        return None
    return {"id": int(rows[0]["ID"]), "net_uid": str(rows[0]["NetUID"])}


def resolve_client_agreement(client_agreement_net_uid: str) -> dict[str, Any] | None:
    """Resolve a ClientAgreement.NetUID to its ID, the parent Agreement.ID, its PricingID and
    CurrencyID. The Agreement context drives base-tier resolution and FX of realized revenue.

    Deleted agreement chains are not valid serving targets for fresh AI recommendations. The
    legacy SQL price function can still calculate for historical NetUIDs, but the API should not
    recommend a new price against a soft-deleted ClientAgreement/Agreement.
    """
    rows = query(
        """
        SELECT TOP 1
            ca.ID AS client_agreement_id,
            ca.NetUID AS client_agreement_netuid,
            ca.AgreementID AS agreement_id,
            a.PricingID AS pricing_id,
            a.CurrencyID AS currency_id
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.NetUID = :uid
              AND ca.Deleted = 0
              AND a.Deleted = 0
        """,
        {"uid": client_agreement_net_uid},
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "client_agreement_id": int(r["client_agreement_id"]),
        "client_agreement_netuid": str(r["client_agreement_netuid"]),
        "agreement_id": int(r["agreement_id"]),
        "pricing_id": int(r["pricing_id"]) if r["pricing_id"] is not None else None,
        "currency_id": int(r["currency_id"]) if r["currency_id"] is not None else None,
    }


def baseline_price(product_net_uid: str, client_agreement_net_uid: str,
                   culture: str, with_vat: bool) -> float | None:
    """THE optimizer baseline — the live engine value, returned UNCHANGED for reference.

    Calls dbo.GetCalculatedProductPriceWithSharesAndVat(@ProductNetId, @ClientAgreementNetId,
    @Culture, @WithVat, @OrderItemId=NULL) -> decimal(30,14). We never re-implement the formula;
    we adjust its DiscountRate lever around this anchor.
    """
    rows = query(
        """
        SELECT dbo.GetCalculatedProductPriceWithSharesAndVat(
            :product_net_uid, :ca_net_uid, :culture, :with_vat, NULL
        ) AS baseline_price
        """,
        {
            "product_net_uid": product_net_uid,
            "ca_net_uid": client_agreement_net_uid,
            "culture": culture,
            "with_vat": 1 if with_vat else 0,
        },
    )
    if not rows or rows[0]["baseline_price"] is None:
        return None
    return float(rows[0]["baseline_price"])


def base_list_price_and_markup(product_id: int, agreement_id: int) -> dict[str, Any] | None:
    """The pre-discount engine leg: P (ProductPricing.Price at the resolved base tier) and the
    tier markup ExtraCharge taken from the AGREEMENT's own pricing tier (Agreement.PricingID).

    Mirrors dbo.GetCalculatedProductPriceWithSharesAndVat exactly:
      P          = ProductPricing.Price WHERE ProductID=@p AND PricingID = dbo.GetBasePricingId(
                   Agreement.PricingID) AND Deleted=0 (trap f) — read at the ROOT base tier.
      ExtraCharge= the engine's @ExtraCharge: Pricing.CalculatedExtraCharge of the AGREEMENT tier
                   (Pricing.ID = Agreement.PricingID), overridden by PricingProductGroupDiscount.
                   CalculatedExtraCharge for that (tier × product-group) when present (Deleted=0).
    The base tier (root) carries 0% markup, so reading ExtraCharge from the base tier dropped the
    agreement's actual margin (e.g. tier 852/847 = +30%). Together they give
    marked_up = ROUND(P + P*ExtraCharge/100, 14) — the denominator the suggested DiscountRate is
    solved against (B leg). marked_up*(1-DiscountRate/100) then reproduces the engine baseline,
    so suggested_discount_pct reproduces recommended_price.

    base_pricing_id / culture describe the BASE tier used for ProductPricing. Discount caps are
    intentionally not keyed on this base family: ЦО2 and ЦО1 share the same base price, but their
    ProductGroupDiscount distributions are different and must be capped by the actual
    Agreement.PricingID.
    """
    rows = query(
        """
        SELECT TOP 1
            pp.Price AS base_price,
            CASE WHEN ppgd.CalculatedExtraCharge IS NOT NULL
                 THEN ppgd.CalculatedExtraCharge
                 ELSE agr_pr.CalculatedExtraCharge
            END AS extra_charge,
            base_pr.ID AS base_pricing_id,
            base_pr.Culture AS culture
        FROM dbo.Agreement a
        CROSS APPLY (SELECT dbo.GetBasePricingId(a.PricingID) AS base_pricing_id) bp
        JOIN dbo.ProductPricing pp
              ON pp.ProductID = :pid
             AND pp.PricingID = bp.base_pricing_id
             AND pp.Deleted = 0
        JOIN dbo.Pricing base_pr ON base_pr.ID = bp.base_pricing_id
        JOIN dbo.Pricing agr_pr ON agr_pr.ID = a.PricingID
        OUTER APPLY (
            SELECT TOP 1 ppg.ProductGroupID
            FROM dbo.ProductProductGroup ppg
            WHERE ppg.ProductID = :pid AND ppg.Deleted = 0
            ORDER BY ppg.ID
        ) pg
        LEFT JOIN dbo.PricingProductGroupDiscount ppgd
              ON ppgd.PricingID = agr_pr.ID
             AND ppgd.ProductGroupID = pg.ProductGroupID
             AND ppgd.Deleted = 0
        WHERE a.ID = :aid
        """,
        {"pid": product_id, "aid": agreement_id},
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "base_price": float(r["base_price"]) if r["base_price"] is not None else None,
        "extra_charge": float(r["extra_charge"] or 0.0),
        "base_pricing_id": int(r["base_pricing_id"]) if r["base_pricing_id"] is not None else None,
        "culture": r["culture"],
    }


def is_promotional(product_id: int, agreement_id: int) -> bool:
    """The engine's promotional branch fires when the product is promotional
    (Product.IsForSale=1 OR IsForZeroSale=1 OR Top IN (N'X9' Latin, N'Х9' Cyrillic)) AND the
    agreement carries a PromotionalPricingID. On that branch GetCalculatedProductPrice* re-sources
    the markup from the promo tier and FORCES the group DiscountRate to 0 — so the engine baseline
    already IS the (promo) list price and the marked_up denominator must use a 0 applied discount.
    """
    rows = query(
        """
        SELECT TOP 1 1 AS hit
        FROM dbo.Product p, dbo.Agreement a
        WHERE p.ID = :pid AND a.ID = :aid
              AND a.PromotionalPricingID IS NOT NULL
              AND (p.IsForSale = 1 OR p.IsForZeroSale = 1
                   OR p.[Top] = N'X9' OR p.[Top] = N'Х9')
        """,
        {"pid": product_id, "aid": agreement_id},
    )
    return bool(rows)


def product_group_id(product_id: int) -> int | None:
    """Resolve a product to its ProductGroup (TOP 1, Deleted=0) — needed for the active group
    discount lookup and the segment discount distribution.
    """
    rows = query(
        """
        SELECT TOP 1 ProductGroupID
        FROM dbo.ProductProductGroup
        WHERE ProductID = :pid AND Deleted = 0
        ORDER BY ID
        """,
        {"pid": product_id},
    )
    return int(rows[0]["ProductGroupID"]) if rows and rows[0]["ProductGroupID"] is not None else None


def active_group_discount(client_agreement_id: int, product_group_id_value: int) -> float | None:
    """The @DiscountRate the live engine consumes: ProductGroupDiscount.DiscountRate for this
    (client-agreement × product-group), IsActive=1, ProductGroupDiscount.Deleted intentionally
    ignored to mirror dbo.GetCalculatedProductPriceWithSharesAndVat. Returned so the engine can
    report the current discount and detect over-discount versus the peer cap.
    """
    rows = query(
        """
        SELECT TOP 1 DiscountRate
        FROM dbo.ProductGroupDiscount
        WHERE ClientAgreementID = :caid
              AND ProductGroupID = :pgid
              AND IsActive = 1
        ORDER BY ID DESC
        """,
        {"caid": client_agreement_id, "pgid": product_group_id_value},
    )
    return float(rows[0]["DiscountRate"]) if rows and rows[0]["DiscountRate"] is not None else None


def unit_cost_eur(product_id: int) -> dict[str, Any]:
    """ROBUST per-product unit cost (EUR) from dbo.ConsignmentItem.AccountingPrice.

    AccountingPrice is EUR-base (FX baked in at income time), so NO GetExchangedToEuroValue is
    applied. Filters (trap d): Deleted=0, AccountingPrice>0, RemainingQty>0,
    ProductID<>synthetic.

    1С debt/balance-import contamination (verified live, container gba-dev-gba-mssql-1):
    EVERY on-hand lot is either IsImportedFromOneC=1 (13453 products, the real migrated inventory,
    avg ~136 EUR) or IsVirtual=1 (2263 products, stock-transfer mirrors). NEITHER Consignment flag
    isolates the contamination: the inflated 800-26683 EUR debt/correction lots span BOTH flags.
    The true vehicle is dbo.ProductIncome.SourceDocumentType = 1 (the 1С debt/balance-import
    document): 2765 such on-hand lots carry 1369 of the >=700 EUR outliers (avg ~1300 EUR), while
    the real supply lots are SourceDocumentType IN (2,3) (avg 11-21 EUR). So we JOIN
    dbo.Consignment c ON c.ID = ci.ConsignmentID and its dbo.ProductIncome and EXCLUDE
    SourceDocumentType=1 (a lot with no ProductIncome — e.g. a pure transfer — is kept). This
    drops the floor to sane (e.g. product 26071517 median 3823.34 -> 103.50) for contaminated
    products and still leaves 13303/13686 products with a usable cost; a product whose ONLY on-hand
    lots are debt-import lots correctly yields NULL cost (no floor, peer-band only) instead of a
    bogus inflated floor. A blanket exclude of IsImportedFromOneC=1 would null 11k+ products and is
    rejected by the data.

    Returns:
      median_cost  = MEDIAN(AccountingPrice) over non-debt on-hand lots (PERCENTILE_CONT 0.5).
      lot_count    = number of non-debt on-hand lots (drives confidence).
      latest_cost  = TOP 1 AccountingPrice ORDER BY ID DESC over the SAME non-debt filter (no
                     RemainingQty>0 requirement) — the fallback when no on-hand lot exists.
    The engine prefers median_cost when lot_count>0 else latest_cost; if both are NULL ->
    unit_cost_eur is null (low confidence, floor skipped, peer-band only).
    """
    s = get_settings()
    rows = query(
        """
        SELECT
            (SELECT TOP 1 PERCENTILE_CONT(0.5)
                          WITHIN GROUP (ORDER BY ci.AccountingPrice) OVER ()
             FROM dbo.ConsignmentItem ci
             JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
             LEFT JOIN dbo.ProductIncome pi ON pi.ID = c.ProductIncomeID
             WHERE ci.ProductID = :pid
                   AND ci.Deleted = 0
                   AND ci.AccountingPrice > 0
                   AND ci.RemainingQty > 0
                   AND ci.ProductID <> :synthetic
                   AND (pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type)) AS median_cost,
            (SELECT COUNT(*)
             FROM dbo.ConsignmentItem ci
             JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
             LEFT JOIN dbo.ProductIncome pi ON pi.ID = c.ProductIncomeID
             WHERE ci.ProductID = :pid
                   AND ci.Deleted = 0
                   AND ci.AccountingPrice > 0
                   AND ci.RemainingQty > 0
                   AND ci.ProductID <> :synthetic
                   AND (pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type)) AS lot_count,
            (SELECT TOP 1 ci.AccountingPrice
             FROM dbo.ConsignmentItem ci
             JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
             LEFT JOIN dbo.ProductIncome pi ON pi.ID = c.ProductIncomeID
             WHERE ci.ProductID = :pid
                   AND ci.Deleted = 0
                   AND ci.AccountingPrice > 0
                   AND ci.ProductID <> :synthetic
                   AND (pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type)
             ORDER BY ci.ID DESC) AS latest_cost
        """,
        {
            "pid": product_id,
            "synthetic": s.synthetic_line_product_id,
            "debt_doc_type": s.debt_import_source_document_type,
        },
    )
    r = rows[0] if rows else {}
    median = r.get("median_cost")
    latest = r.get("latest_cost")
    lot_count = int(r.get("lot_count") or 0)
    if median is not None:
        cost = float(median)
        source = "median_onhand"
    elif latest is not None:
        cost = float(latest)
        source = "latest_lot"
    else:
        cost = None
        source = "none"
    return {
        "unit_cost_eur": cost,
        "lot_count": lot_count,
        "median_cost": float(median) if median is not None else None,
        "latest_cost": float(latest) if latest is not None else None,
        "cost_source": source,
    }


def peer_band(product_id: int, as_of_date: str, window_months: int,
              fx_date: str) -> dict[str, Any]:
    """Peer benchmark: P25/P50/P75 + n of realized EUR unit price over DISTINCT client-agreements
    for product p in the trailing window (by Sale.Created).

    OrderItem.PricePerItem is ALREADY EUR-denominated (the per-line transaction FX is recorded
    separately in OrderItem.ExchangeRateAmount and is NOT a divisor of PricePerItem): for a UAH
    agreement PricePerItem == the engine EUR price 1:1, while GetExchangedToEuroValue(PricePerItem,
    UAH, date) wrongly divides it by the ~52 UAH/EUR rate and collapses it to ~2% of true. So the
    realized unit price IS PricePerItem directly, for every currency — no conversion is applied.

    Join OrderItem->Order->Sale->ClientAgreement->Agreement. Filters (traps a,b): IsValidFor
    CurrentSale=1, ProductID<>synthetic, PricePerItem>0. n = distinct client-agreements.

    UoM piece-vs-box outliers (trap g) are rejected by a per-product median/MAD modified-z filter
    (replaces the old fixed bottom/top-decile trim). PricePerItem mixes piece and box lines for
    the same product with NO per-line UoM/pack discriminator (Qty/UnpackedQty/IsFromOffer are
    identical across both clusters and PackingStandard does not encode the box multiple — verified
    live), so the mixed line cannot be normalized, only rejected. The fixed decile trim drops
    exactly 10% per tail and leaks whenever the contaminated fraction exceeds that (product
    25104373: 27% of lines are piece prices -> decile p75/p25=2.0 still contaminated). The median
    has a 50% breakdown point, so we keep rows with ABS(eur_price - median) <= k*1.4826*MAD where
    MAD = median absolute deviation from the median: it adapts to the contaminated fraction and
    leaves clean products' legitimate spread intact (25300863's real 2.56 spread survives;
    25381012 stays p75/p25=1.11). MAD on the raw price tracks log-MAD here without LOG(0) risk.
    k = peer_band_mad_k (3.5, the Iglewicz-Hoaglin cutoff). The reject is skipped (keep-all) below
    peer_band_mad_min_rows lines and whenever MAD<=0 so a thin/all-tied product is never
    over-trimmed.

    fx_date is accepted for contract symmetry with the cost path but is unused: PricePerItem
    needs no revaluation.
    """
    s = get_settings()
    _ = fx_date
    rows = query(
        """
        WITH priced AS (
            SELECT
                o.ClientAgreementID AS ca_id,
                oi.PricePerItem AS eur_price
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            JOIN dbo.Sale s ON s.OrderID = o.ID
            WHERE oi.ProductID = :pid
                  AND oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synthetic
                  AND oi.PricePerItem > 0
                  AND s.Created <= :asof
                  AND s.Created >= DATEADD(month, :neg_months, :asof)
        ),
        stats AS (
            SELECT
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY eur_price) OVER () AS med,
                COUNT(*) OVER () AS row_n
            FROM priced
        ),
        dev AS (
            SELECT p.eur_price, p.ca_id, st.med, st.row_n,
                   ABS(p.eur_price - st.med) AS abs_dev
            FROM priced p
            CROSS JOIN (SELECT DISTINCT med, row_n FROM stats) st
        ),
        madc AS (
            SELECT eur_price, ca_id, med, row_n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY abs_dev) OVER () AS mad
            FROM dev
        ),
        trimmed AS (
            SELECT eur_price, ca_id
            FROM madc
            WHERE row_n < :min_rows
                  OR mad IS NULL OR mad <= 0
                  OR ABS(eur_price - med) <= :mad_k * 1.4826 * mad
        )
        SELECT DISTINCT
            PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY eur_price) OVER () AS p25,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY eur_price) OVER () AS p50,
            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY eur_price) OVER () AS p75,
            (SELECT COUNT(DISTINCT ca_id) FROM trimmed) AS n
        FROM trimmed
        """,
        {
            "pid": product_id, "asof": as_of_date, "neg_months": -window_months,
            "synthetic": s.synthetic_line_product_id,
            "mad_k": s.peer_band_mad_k,
            "min_rows": s.peer_band_mad_min_rows,
        },
    )
    if not rows:
        return {"p25": None, "p50": None, "p75": None, "n": 0}
    r = rows[0]
    return {
        "p25": float(r["p25"]) if r["p25"] is not None else None,
        "p50": float(r["p50"]) if r["p50"] is not None else None,
        "p75": float(r["p75"]) if r["p75"] is not None else None,
        "n": int(r["n"] or 0),
    }


def segment_discount_distribution(product_group_id_value: int, pricing_id: int,
                                  culture: str) -> dict[str, Any]:
    """B leg: the peer discount cap. Distribution of the engine's own DiscountRate lever within
    the segment = (ProductGroupID × actual Agreement.PricingID × Culture).

    D = { ProductGroupDiscount.DiscountRate : IsActive-only, pgd.Deleted ignored, matching the
          engine/lever, same ProductGroupID, live client-agreements whose Agreement uses the same
          actual PricingID and whose Pricing.Culture matches }.
          Returns P75 (target cap) and P90 (hard cap) + n.

    The cap MUST be computed over the SAME population the lever/engine consume: active_group_discount
    (the applied @DiscountRate) and the live engine honor IsActive=1 and IGNORE Deleted (on this DB
    100% of Sale/Order/OrderItem carry Deleted=1; ProductGroupDiscount is gated by IsActive only).
    Filtering pgd.Deleted=0 here would cap the discount against a smaller, different population than
    the discount being capped. See tests/test_source_guards.py (active_group_discount must not filter
    pgd.Deleted) — this cap obeys the same invariant. ClientAgreement/Agreement Deleted flags are
    different: they define whether a commercial agreement is a live serving peer, and are filtered
    out to avoid stale sync leftovers contaminating current tier norms.

    Do not group by dbo.GetBasePricingId(a.PricingID): ЦО2 and ЦО1/ЦP share the same base product
    price but have materially different discount norms. Pooling them would let the AI recommend a
    ЦО1-style discount on a ЦО2 agreement.
    """
    rows = query(
        """
        WITH seg AS (
            SELECT pgd.DiscountRate AS discount_rate
            FROM dbo.ProductGroupDiscount pgd
            JOIN dbo.ClientAgreement ca ON ca.ID = pgd.ClientAgreementID
            JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            JOIN dbo.Pricing pr ON pr.ID = a.PricingID
            WHERE pgd.ProductGroupID = :pgid
                  AND pgd.IsActive = 1
                  AND ca.Deleted = 0
                  AND a.Deleted = 0
                  AND a.PricingID = :pricing_id
                  AND pr.Culture = :culture
        )
        SELECT DISTINCT
            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY discount_rate) OVER () AS p75,
            PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY discount_rate) OVER () AS p90,
            (SELECT COUNT(*) FROM seg) AS n
        FROM seg
        """,
        {
            "pgid": product_group_id_value,
            "pricing_id": pricing_id,
            "culture": culture,
        },
    )
    if not rows:
        return {"p75": None, "p90": None, "n": 0}
    r = rows[0]
    return {
        "p75": float(r["p75"]) if r["p75"] is not None else None,
        "p90": float(r["p90"]) if r["p90"] is not None else None,
        "n": int(r["n"] or 0),
    }


def product_line_count(product_id: int, as_of_date: str, window_months: int) -> int:
    """Number of VALID sale lines for a product in the trailing window — the estimability band
    discriminant (bands A/B >=100 lines support a per-SKU elasticity; below it pools/falls back).
    Same valid-row + synthetic-exclude filters as the panel."""
    s = get_settings()
    rows = query(
        """
        SELECT COUNT(*) AS n
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.ProductID = :pid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synthetic
              AND oi.PricePerItem > 0
              AND oi.Qty > 0
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        """,
        {"pid": product_id, "asof": as_of_date, "neg_months": -window_months,
         "synthetic": s.synthetic_line_product_id},
    )
    return int(rows[0]["n"] or 0) if rows else 0


def product_panel(product_id: int, as_of_date: str, window_months: int) -> list[dict[str, Any]]:
    """The (agreement x month) demand panel for ONE product, for own-price elasticity.

    Q     = SUM(OrderItem.Qty) by (ClientAgreementID x calendar month of Sale.Created).
    price = volume-weighted OrderItem.PricePerItem in the cell = SUM(Qty*PricePerItem)/SUM(Qty).
            PricePerItem is ALREADY EUR for every agreement currency (verified live; NOT FX-wrapped
            -- a UAH agreement's PricePerItem sits in the same band as EUR, GetExchangedToEuroValue
            would wrongly divide it by ~52). So no currency CASE / GetExchangedToEuroValue here.

    Filters mirror the peer band (traps a,b): IsValidForCurrentSale=1 (NEVER Deleted, =1 on 100% of
    Sale/Order/OrderItem), ProductID<>synthetic 1С line, PricePerItem>0, Qty>0, trailing window by
    Sale.Created. The per-product UoM/price outlier reject and the regression run in Python.
    """
    s = get_settings()
    return query(
        """
        SELECT
            o.ClientAgreementID AS agreement_id,
            FORMAT(s.Created, 'yyyy-MM') AS month,
            SUM(oi.Qty) AS qty,
            SUM(oi.Qty * oi.PricePerItem) / NULLIF(SUM(oi.Qty), 0) AS price
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.ProductID = :pid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synthetic
              AND oi.PricePerItem > 0
              AND oi.Qty > 0
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        GROUP BY o.ClientAgreementID, FORMAT(s.Created, 'yyyy-MM')
        """,
        {
            "pid": product_id, "asof": as_of_date, "neg_months": -window_months,
            "synthetic": s.synthetic_line_product_id,
        },
    )


def group_panel(product_group_id_value: int, as_of_date: str, window_months: int,
                max_products: int = 400) -> list[dict[str, Any]]:
    """The stacked (product x agreement x month) demand panel for a ProductGroup, for the POOLED
    elasticity that sparser SKUs in the group borrow. Same cell/filter rules as product_panel, with
    ProductID carried so the pooled fit can add a product fixed effect.

    Capped at max_products of the highest-volume SKUs in the group (by valid line count) so a giant
    group cannot blow up the design matrix; the cap is generous relative to the bands A/B universe.
    """
    s = get_settings()
    return query(
        """
        WITH grp AS (
            SELECT ProductID
            FROM dbo.ProductProductGroup
            WHERE ProductGroupID = :pgid AND Deleted = 0
        ),
        ranked AS (
            SELECT TOP (:max_products) oi2.ProductID AS pid, COUNT(*) AS n_lines
            FROM dbo.OrderItem oi2
            JOIN grp ON grp.ProductID = oi2.ProductID
            WHERE oi2.IsValidForCurrentSale = 1
                  AND oi2.ProductID <> :synthetic
                  AND oi2.PricePerItem > 0
                  AND oi2.Qty > 0
            GROUP BY oi2.ProductID
            ORDER BY COUNT(*) DESC
        )
        SELECT
            oi.ProductID AS product_id,
            o.ClientAgreementID AS agreement_id,
            FORMAT(s.Created, 'yyyy-MM') AS month,
            SUM(oi.Qty) AS qty,
            SUM(oi.Qty * oi.PricePerItem) / NULLIF(SUM(oi.Qty), 0) AS price
        FROM dbo.OrderItem oi
        JOIN ranked ON ranked.pid = oi.ProductID
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synthetic
              AND oi.PricePerItem > 0
              AND oi.Qty > 0
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        GROUP BY oi.ProductID, o.ClientAgreementID, FORMAT(s.Created, 'yyyy-MM')
        """,
        {
            "pgid": product_group_id_value, "asof": as_of_date, "neg_months": -window_months,
            "synthetic": s.synthetic_line_product_id, "max_products": max_products,
        },
    )
