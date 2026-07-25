"""Parameterized read queries for the CreditScore-100 solvency engine.

All SQL is parameterized (:name) — no f-string interpolation. Every query honors the
discovery data traps:
  (a) NEVER filter Deleted=0 on Sale/Order/OrderItem (=1 on 100% of rows). Validity comes
      from OrderItem.IsValidForCurrentSale=1 and SaleReturn.IsCanceled=0.
  (b) Synthetic 1С debt-entry line (ProductID 25422404) is EXCLUDED from turnover/activity
      but KEPT in debt/exposure (it is real carried debt).
  (c) FX snapshot date is pinned per run (GetExchangedToEuroValue revalues at call time).
  (d) BaseSalePaymentStatus.Amount=0 even when Paid -> use the status ENUM (count-based).
  (e) Multi-currency: EUR-normalize via dbo.GetExchangedToEuroValue.

The window is bounded by Sale.Created in [as_of - window_months, as_of].
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.data.db import in_clause, query
from app.data.synthetic_product import synthetic_product_ids
from app.domain.money import as_decimal, round_cent

# Regular-sale payment statuses (SalePaymentStatusType). Mapped, never hardcoded inline.
_SALE_PAID = (1, 2)          # Paid, Overpaid
_SALE_PARTIAL = (3,)         # PartialPaid
_SALE_NOTPAID = (0,)         # NotPaid
_SALE_REFUND = (4,)          # Refund -> EXCLUDED from the discipline ratio
_SALE_OPEN_UNPAID = (0, 3)   # NotPaid + PartialPaid -> open exposure proxy
_READINESS_TTL_S = 60.0
_readiness_lock = threading.Lock()
_readiness_state: dict[int, tuple[float, dict[str, Any]]] = {}


def _synthetic_not_in() -> tuple[str, dict[str, Any]]:
    """Parameterized 'NOT IN (...)' over every effective synthetic 1С debt-entry ProductID."""
    ids = sorted(synthetic_product_ids())
    placeholder, params = in_clause("synthetic", ids)
    return placeholder, params


def source_readiness(max_lag_days: int) -> dict[str, Any]:
    """Factual source probe used by health/readiness.

    It verifies a current canonical sales spine, buyer population, the live debt source and
    exactly one dynamically resolved synthetic debt-entry product.
    """
    now_mono = time.monotonic()
    with _readiness_lock:
        cached = _readiness_state.get(max_lag_days)
        if cached is not None and now_mono - cached[0] < _READINESS_TTL_S:
            return dict(cached[1])

    settings = get_settings()
    synthetic_ids = synthetic_product_ids()
    synthetic_ph, synthetic_params = in_clause(
        "readiness_synthetic", sorted(synthetic_ids) or [0]
    )
    rows = query(
        f"""
        SELECT
            (
                SELECT TOP 1 o.Created
                FROM dbo.[Order] o
                JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
                WHERE oi.IsValidForCurrentSale = 1
                      AND oi.ProductID IS NOT NULL
                      AND oi.ProductID NOT IN {synthetic_ph}
                ORDER BY o.Created DESC, o.ID DESC
            ) AS latest_sale_at,
            (
                SELECT COUNT_BIG(DISTINCT cir.ClientID)
                FROM dbo.ClientInRole cir
                JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
                JOIN dbo.Client c ON c.ID = cir.ClientID
                WHERE cir.Deleted = 0 AND ct.[Type] = 0 AND c.Deleted = 0
            ) AS buyer_count,
            (
                SELECT COUNT_BIG(*)
                FROM dbo.Debt d
                WHERE d.Deleted = 0 AND d.Created > '2000-01-01'
            ) AS live_debt_count,
            (
                SELECT COUNT_BIG(*)
                FROM dbo.Product p
                WHERE p.Deleted = 0 AND p.Name = :synthetic_name
                      AND p.ID IN {synthetic_ph}
            ) AS synthetic_product_count
        """,
        {
            **synthetic_params,
            "synthetic_name": settings.synthetic_line_product_name,
        },
    )
    row = rows[0] if rows else {}
    latest = row.get("latest_sale_at")
    source_fresh = isinstance(latest, datetime) and latest >= datetime.now() - timedelta(
        days=max_lag_days
    )
    buyer_count = int(row.get("buyer_count") or 0)
    debt_count = int(row.get("live_debt_count") or 0)
    synthetic_count = int(row.get("synthetic_product_count") or 0)
    reasons: list[str] = []
    if latest is None:
        reasons.append("canonical_sales_missing")
    elif not source_fresh:
        reasons.append("canonical_sales_stale")
    if buyer_count <= 0:
        reasons.append("buyers_missing")
    if debt_count <= 0:
        reasons.append("live_debt_missing")
    if synthetic_count != 1:
        reasons.append("synthetic_product_unresolved")
    result = {
        "business_ready": not reasons,
        "reasons": reasons,
        "latest_sale_at": latest.isoformat() if isinstance(latest, datetime) else None,
        "max_source_lag_days": max_lag_days,
        "buyer_count": buyer_count,
        "live_debt_count": debt_count,
        "synthetic_product_count": synthetic_count,
        "synthetic_product_ids": sorted(synthetic_ids),
    }
    with _readiness_lock:
        _readiness_state[max_lag_days] = (time.monotonic(), dict(result))
    return result


def resolve_client_id(client_net_uid: str) -> int | None:
    rows = query(
        "SELECT TOP 1 ID FROM dbo.Client WHERE NetUID = :uid",
        {"uid": client_net_uid},
    )
    return int(rows[0]["ID"]) if rows else None


def client_exists(client_id: int) -> bool:
    """True when dbo.Client has a row with this ID.

    Guards the direct-client_id entry paths: a caller-supplied ID is never trusted to exist,
    so a fabricated score is never produced for a phantom client.
    """
    rows = query(
        "SELECT TOP 1 1 AS hit FROM dbo.Client WHERE ID = :cid",
        {"cid": client_id},
    )
    return bool(rows)


def has_buyer_role(client_id: int) -> bool:
    """True when the entity has a non-deleted Buyer role (ClientType.[Type]=0).

    Solvency applies ONLY to buyers: a product supplier (Provider-only, Type=1) has no
    buyer-side signal, so the model's no-data => 1.0 fallbacks would yield a misleading 70/B
    baseline. Dual-role entities (both Buyer and Provider) are buyers too, so they pass.
    ClientType.[Type]: 0=Buyer, 1=Provider/supplier.
    """
    rows = query(
        """
        SELECT TOP 1 1 AS hit
        FROM dbo.ClientInRole cir
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE cir.ClientID = :cid
              AND cir.Deleted = 0
              AND ct.[Type] = 0
        """,
        {"cid": client_id},
    )
    return bool(rows)


def payment_status_counts(client_id: int, as_of_date: str, window_months: int) -> dict[str, int]:
    """(1) PaymentDiscipline source — count-based over the status ENUM (trap d).

    Sale JOIN BaseSalePaymentStatus(SalePaymentStatusType) JOIN ClientAgreement, grouped by
    ClientAgreement.ClientID, over the window by Sale.Created. Refund=4 is excluded from the ratio.
    Returns paid / overpaid / partial / notpaid / refund counts.
    """
    rows = query(
        """
        SELECT bsps.SalePaymentStatusType AS status, COUNT(DISTINCT s.ID) AS cnt
        FROM dbo.Sale s
        JOIN dbo.BaseSalePaymentStatus bsps ON bsps.ID = s.BaseSalePaymentStatusID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        GROUP BY bsps.SalePaymentStatusType
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months},
    )
    out = {"paid": 0, "overpaid": 0, "partial": 0, "notpaid": 0, "refund": 0}
    for r in rows:
        st = int(r["status"]) if r["status"] is not None else -1
        n = int(r["cnt"] or 0)
        if st == 1:
            out["paid"] += n
        elif st == 2:
            out["overpaid"] += n
        elif st == 3:
            out["partial"] += n
        elif st == 0:
            out["notpaid"] += n
        elif st == 4:
            out["refund"] += n
    return out


def retail_payment_status_counts(client_id: int, as_of_date: str,
                                 window_months: int) -> dict[str, int]:
    """Retail sales are rows of dbo.Sale carrying RetailClientId; their payment state is the
    same BaseSalePaymentStatus already aggregated by payment_status_counts. There is no
    separate dbo.RetailSale table, and RetailPaymentStatus is a payment-image lookup, not a
    per-sale status. So retail needs no extra counts here — return empty and let the regular
    Sale path drive the discipline ratio.
    """
    return {"paid": 0, "partial": 0, "notpaid": 0}


def open_unpaid_stats(client_id: int, as_of_date: str, window_months: int) -> dict[str, Any]:
    """open_unpaid_count / open_unpaid_max_age_days / avg — Sale where payment status IN
    (NotPaid, PartialPaid); age = DATEDIFF(day, Sale.Created, :asof). Anchored on as_of_date so a
    back-dated run ages invoices as of that date (reproduces GETDATE() when :asof is today). Used
    by the live DebtLoad proxy and the aging chart.
    """
    placeholder, params = in_clause("st", list(_SALE_OPEN_UNPAID))
    rows = query(
        f"""
        SELECT
            COUNT(DISTINCT s.ID) AS open_count,
            MAX(DATEDIFF(day, s.Created, :asof)) AS max_age_days,
            AVG(CAST(DATEDIFF(day, s.Created, :asof) AS FLOAT)) AS avg_age_days
        FROM dbo.Sale s
        JOIN dbo.BaseSalePaymentStatus bsps ON bsps.ID = s.BaseSalePaymentStatusID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
              AND bsps.SalePaymentStatusType IN {placeholder}
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months, **params},
    )
    r = rows[0] if rows else {}
    return {
        "open_count": int(r.get("open_count") or 0),
        "max_age_days": int(r.get("max_age_days") or 0),
        "avg_age_days": float(r.get("avg_age_days") or 0.0),
    }


def open_unpaid_aging_buckets(client_id: int, as_of_date: str,
                              window_months: int) -> list[dict[str, Any]]:
    """Aging buckets (0-30 / 31-60 / 61-90 / 90+) for open NotPaid+PartialPaid sales.

    Age anchored on as_of_date (reproduces GETDATE() when :asof is today) so a back-dated run
    buckets invoices as of that date. Feeds the open_invoice_aging_bars chart. Count-based (trap d).
    """
    placeholder, params = in_clause("st", list(_SALE_OPEN_UNPAID))
    rows = query(
        f"""
        SELECT bucket, COUNT(*) AS cnt FROM (
            SELECT CASE
                WHEN DATEDIFF(day, s.Created, :asof) <= 30 THEN '0-30'
                WHEN DATEDIFF(day, s.Created, :asof) <= 60 THEN '31-60'
                WHEN DATEDIFF(day, s.Created, :asof) <= 90 THEN '61-90'
                ELSE '90+'
            END AS bucket
            FROM dbo.Sale s
            JOIN dbo.BaseSalePaymentStatus bsps ON bsps.ID = s.BaseSalePaymentStatusID
            JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
            WHERE ca.ClientID = :cid
                  AND s.Created <= :asof
                  AND s.Created >= DATEADD(month, :neg_months, :asof)
                  AND bsps.SalePaymentStatusType IN {placeholder}
        ) t
        GROUP BY bucket
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months, **params},
    )
    return [{"bucket": r["bucket"], "count": int(r["cnt"] or 0)} for r in rows]


def debt_aging_buckets_truth(client_id: int, as_of_date: str,
                             fx_date: str) -> list[dict[str, Any]]:
    """TRUTH aging buckets (0-30 / 31-60 / 61-90 / 90+) from real overdue debt — the same source
    the model's aging features (app/risk/dataset.feat_debt_aging) and overdue_amount_eur use.

    A debt line is overdue by od = DATEDIFF(day, Debt.Created, :asof) - Agreement.NumberDaysDebt;
    only past-grace lines (od > 0) are bucketed (within-grace carried trade credit is not "open
    overdue"). Count is the line count; amount_eur is the EUR-normalized Debt.Total via the pinned
    fx_date (dbo.GetExchangedToEuroValue). Replaces the stale BaseSalePaymentStatus.NotPaid proxy
    (only 3.3% of NotPaid sales have a live debt row, so the enum aging was ~30x inflated). Every
    debt line counts incl. the synthetic 1С line — it is real carried debt (trap b: kept in debt).
    """
    rows = query(
        """
        SELECT bucket,
               COUNT(*) AS cnt,
               ISNULL(SUM(e), 0) AS amount_eur
        FROM (
            SELECT CASE
                WHEN od <= 30 THEN '0-30'
                WHEN od <= 60 THEN '31-60'
                WHEN od <= 90 THEN '61-90'
                ELSE '90+'
            END AS bucket,
            e
            FROM (
                SELECT DATEDIFF(day, d.Created, :asof) - a.NumberDaysDebt AS od,
                       CAST(dbo.GetExchangedToEuroValue(
                           d.Total, a.CurrencyID, :fxdate
                       ) AS decimal(38, 6)) AS e
                FROM dbo.ClientInDebt cid
                JOIN dbo.Debt d ON d.ID = cid.DebtID
                JOIN dbo.Agreement a ON a.ID = cid.AgreementID
                WHERE cid.ClientID = :cid
                      AND cid.Deleted = 0
                      AND d.Deleted = 0
                      AND d.Created <= :asof
            ) g
            WHERE od > 0
        ) t
        GROUP BY bucket
        """,
        {"cid": client_id, "asof": as_of_date, "fxdate": fx_date},
    )
    return [
        {
            "bucket": r["bucket"],
            "count": int(r["cnt"] or 0),
            "amount_eur": round_cent(r["amount_eur"] or Decimal(0)),
        }
        for r in rows
    ]


def debt_exposure_donut_truth(client_id: int, as_of_date: str, window_months: int,
                              fx_date: str) -> dict[str, Any]:
    """TRUTH-based discipline donut from settlement reality (Debt/ClientInDebt), replacing the
    stale BaseSalePaymentStatus.NotPaid split that showed ~93% unpaid for nearly every client.

    Slices (count-based, donut shape preserved): the buyer's window sales partition into
      - settled : sales NOT carrying an open debt line (paid / fully settled) -> the good side.
      - current : open debt lines still within the agreement grace (carried trade credit).
      - overdue : open debt lines past grace (od > 0) -> the genuine delinquency exposure.
    settled = max(total_window_sales - open_debt_lines, 0). For a fully-settled client (no debt
    rows) this is ~100% settled; for a debtor the overdue slice carries the real exposure. EUR
    sums (current/overdue) are returned alongside for the amount-aware view (trap e: EUR via the
    pinned fx_date). All debt lines count incl. the synthetic 1С line (real carried debt, trap b).
    """
    total = total_sales_count(client_id, as_of_date, window_months)
    rows = query(
        """
        SELECT
            SUM(CASE WHEN od <= 0 THEN 1 ELSE 0 END) AS current_lines,
            SUM(CASE WHEN od >  0 THEN 1 ELSE 0 END) AS overdue_lines,
            ISNULL(SUM(CASE WHEN od <= 0 THEN e ELSE 0 END), 0) AS current_eur,
            ISNULL(SUM(CASE WHEN od >  0 THEN e ELSE 0 END), 0) AS overdue_eur
        FROM (
            SELECT DATEDIFF(day, d.Created, :asof) - a.NumberDaysDebt AS od,
                   CAST(dbo.GetExchangedToEuroValue(
                       d.Total, a.CurrencyID, :fxdate
                   ) AS decimal(38, 6)) AS e
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            WHERE cid.ClientID = :cid
                  AND cid.Deleted = 0
                  AND d.Deleted = 0
                  AND d.Created <= :asof
        ) t
        """,
        {"cid": client_id, "asof": as_of_date, "fxdate": fx_date},
    )
    r = rows[0] if rows else {}
    current_lines = int(r.get("current_lines") or 0)
    overdue_lines = int(r.get("overdue_lines") or 0)
    settled = max(total - current_lines - overdue_lines, 0)
    return {
        "settled": settled,
        "current": current_lines,
        "overdue": overdue_lines,
        "current_eur": round_cent(r.get("current_eur") or Decimal(0)),
        "overdue_eur": round_cent(r.get("overdue_eur") or Decimal(0)),
    }


def total_sales_count(client_id: int, as_of_date: str, window_months: int) -> int:
    """total_sales_12mo — denominator for the live DebtLoad proxy."""
    rows = query(
        """
        SELECT COUNT(DISTINCT s.ID) AS n
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months},
    )
    return int(rows[0]["n"]) if rows else 0


def credit_limit_utilization(client_id: int) -> list[dict[str, Any]]:
    """credit_limit / term_days / current_balance / limit_utilization per controlled agreement.

    Agreement.AmountDebt (gate IsControlAmountDebt=1), Agreement.NumberDaysDebt (gate
    IsControlNumberDaysDebt=1). current_balance = ClientAgreement.CurrentAmount.
    limit_utilization = CurrentAmount / AmountDebt. Returns one row per agreement so the engine
    can apply the credit-policy caps and render the utilization gauge.
    """
    rows = query(
        """
        SELECT
            a.ID AS agreement_id,
            a.IsControlAmountDebt AS is_control_amount,
            a.AmountDebt AS amount_debt,
            a.IsControlNumberDaysDebt AS is_control_days,
            a.NumberDaysDebt AS number_days_debt,
            a.CurrencyID AS currency_id,
            ca.CurrentAmount AS current_amount,
            CASE WHEN a.IsControlAmountDebt = 1 AND a.AmountDebt > 0
                 THEN ca.CurrentAmount * 1.0 / a.AmountDebt END AS limit_utilization
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.ClientID = :cid
        """,
        {"cid": client_id},
    )
    return [
        {
            "agreement_id": int(r["agreement_id"]),
            "is_control_amount": bool(r["is_control_amount"]),
            "amount_debt": float(r["amount_debt"] or 0.0),
            "is_control_days": bool(r["is_control_days"]),
            "number_days_debt": int(r["number_days_debt"] or 0),
            "currency_id": int(r["currency_id"]) if r["currency_id"] is not None else None,
            "current_amount": float(r["current_amount"] or 0.0),
            "limit_utilization": (
                float(r["limit_utilization"]) if r["limit_utilization"] is not None else None
            ),
        }
        for r in rows
    ]


def turnover_eur(client_id: int, as_of_date: str, window_months: int,
                 fx_date: str) -> float:
    """turnover_eur — SUM(OrderItem.Qty * PricePerItem) over the window.

    OrderItem.PricePerItem is ALREADY EUR (verified live: PricePerItem == the EUR engine price
    GetCalculatedProductPriceWithSharesAndVat; the agreement-currency value is the *Local engine
    price = EUR x ExchangeRateAmount). So NO GetExchangedToEuroValue conversion — applying it would
    wrongly divide non-EUR-agreement turnover by the FX rate. Filters honor traps (a)/(b):
    IsValidForCurrentSale=1, NO Deleted=0 filter, ProductID NOT IN synthetic set, Created > '2000-01-01'.
    """
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT ISNULL(SUM(
            CAST(oi.Qty AS decimal(19, 6))
            * CAST(oi.PricePerItem AS decimal(19, 6))
        ), 0) AS turnover
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.ClientID = :cid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        """,
        {
            "cid": client_id, "asof": as_of_date, "neg_months": -window_months,
            "fxdate": fx_date, **syn,
        },
    )
    return float(round_cent(rows[0]["turnover"])) if rows else 0.0


def turnover_eur_by_currency(client_id: int, as_of_date: str, window_months: int,
                             fx_date: str) -> list[dict[str, Any]]:
    """Per-currency turnover (EUR-normalized) for the currency_breakdown output (trap e)."""
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT a.CurrencyID AS currency_id,
               ISNULL(SUM(
                   CAST(oi.Qty AS decimal(19, 6))
                   * CAST(oi.PricePerItem AS decimal(19, 6))
               ), 0) AS turnover_eur
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.ClientID = :cid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        GROUP BY a.CurrencyID
        """,
        {
            "cid": client_id, "asof": as_of_date, "neg_months": -window_months,
            "fxdate": fx_date, **syn,
        },
    )
    return [
        {
            "currency_id": int(r["currency_id"]) if r["currency_id"] is not None else None,
            "turnover_eur": round_cent(r["turnover_eur"] or Decimal(0)),
        }
        for r in rows
    ]


def activity_stats(client_id: int, as_of_date: str, window_months: int) -> dict[str, Any]:
    """order_count / tenure_months / recency_days, over REAL sales only.

    order_count = COUNT(DISTINCT Sale.ID) in window.
    tenure_months = DATEDIFF(month, MIN(Sale.Created excl sentinel 1980-01-01), as_of) over ALL
    history (not windowed). recency_days = DATEDIFF(day, MAX(Sale.Created), as_of) over history.

    A Sale only counts if it carries a real, valid, non-synthetic line — i.e. EXISTS a
    OrderItem with IsValidForCurrentSale=1 and ProductID not in the synthetic-1С set. Without
    this gate the ~21.7k standalone single-line debt-injection Sales (synthetic ProductID) would
    inflate order_count and skew tenure/recency for affected clients (data trap (b)).
    """
    s = get_settings()
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT
            (SELECT COUNT(DISTINCT s2.ID)
             FROM dbo.Sale s2
             JOIN dbo.ClientAgreement ca2 ON ca2.ID = s2.ClientAgreementID
             WHERE ca2.ClientID = :cid
                   AND s2.Created <= :asof
                   AND s2.Created >= DATEADD(month, :neg_months, :asof)
                   AND EXISTS (
                       SELECT 1 FROM dbo.OrderItem oi2
                       WHERE oi2.OrderID = s2.OrderID
                             AND oi2.ProductID NOT IN {ph}
                             AND oi2.IsValidForCurrentSale = 1
                   )) AS order_count,
            DATEDIFF(month,
                MIN(CASE WHEN s.Created > :sentinel THEN s.Created END), :asof) AS tenure_months,
            DATEDIFF(day, MAX(s.Created), :asof) AS recency_days
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid AND s.Created <= :asof
              AND EXISTS (
                  SELECT 1 FROM dbo.OrderItem oi
                  WHERE oi.OrderID = s.OrderID
                        AND oi.ProductID NOT IN {ph}
                        AND oi.IsValidForCurrentSale = 1
              )
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months,
         "sentinel": s.tenure_sentinel_date, **syn},
    )
    r = rows[0] if rows else {}
    return {
        "order_count": int(r.get("order_count") or 0),
        "tenure_months": int(r.get("tenure_months") or 0),
        "recency_days": int(r.get("recency_days")) if r.get("recency_days") is not None else None,
    }


def return_qty_rate(client_id: int, as_of_date: str, window_months: int) -> float:
    """(5) return_qty_rate = returned qty / sold qty over the window.

    Returned qty follows gba-server's canonical model: SUM(SaleReturnItem.Qty), grouped first by
    (SaleReturnID, OrderItemID). This preserves partial returns and sums multiple active item rows;
    using MAX(OrderItem.Qty) incorrectly replaces both with the original sold quantity. Window on
    SaleReturn.FromDate (sr.Created is a bulk-sync mirror stamp); active returns only
    (sr.Deleted=0 AND sr.IsCanceled=0); oi.Deleted is intentionally not filtered; synthetic 1С
    products are excluded. Sold qty is valid OrderItem.Qty over the same business window.
    """
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT
            (SELECT ISNULL(SUM(line.returned_qty), 0)
             FROM (
                SELECT SUM(sri.Qty) AS returned_qty
                FROM dbo.SaleReturnItem sri
                JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
                JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                     AND sr.Deleted = 0 AND sr.IsCanceled = 0
                WHERE sri.Deleted = 0
                      AND oi.ProductID IS NOT NULL
                      AND oi.ProductID NOT IN {ph}
                      AND sr.ClientID = :cid
                      AND sr.FromDate <= :asof
                      AND sr.FromDate >= DATEADD(month, :neg_months, :asof)
                GROUP BY sri.SaleReturnID, sri.OrderItemID
             ) line) AS return_qty,
            (SELECT ISNULL(SUM(oi.Qty), 0)
             FROM dbo.Sale s3
             JOIN dbo.[Order] o ON o.ID = s3.OrderID
             JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
             JOIN dbo.ClientAgreement ca3 ON ca3.ID = s3.ClientAgreementID
             WHERE ca3.ClientID = :cid
                   AND oi.IsValidForCurrentSale = 1
                   AND oi.ProductID NOT IN {ph}
                   AND s3.Created <= :asof
                   AND s3.Created >= DATEADD(month, :neg_months, :asof)) AS sold_qty
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months, **syn},
    )
    if not rows:
        return 0.0
    sold = float(rows[0]["sold_qty"] or 0.0)
    if sold <= 0:
        return 0.0
    return float(rows[0]["return_qty"] or 0.0) / sold


def client_flags(client_id: int) -> dict[str, Any]:
    """Client.IsBlocked — drives the blocked-half cap."""
    rows = query(
        "SELECT IsBlocked FROM dbo.Client WHERE ID = :cid",
        {"cid": client_id},
    )
    return {"is_blocked": bool(rows[0]["IsBlocked"]) if rows else False}


def debt_sync_is_live() -> bool:
    """Probe whether the Debt table is quiesced / usable.

    Live when there are rows with Deleted=0 AND sane Created (post-2000). If the sync is in
    progress every row is Deleted=1, so this returns False and the engine falls back to the
    live open-unpaid proxy. Recorded as SolvencyScore.debt_load_source.
    """
    rows = query(
        """
        SELECT COUNT(*) AS live_rows
        FROM dbo.Debt
        WHERE Deleted = 0 AND Created > '2000-01-01'
        """,
    )
    return bool(rows and int(rows[0]["live_rows"] or 0) > 0)


def synthetic_line_drift_check() -> dict[str, Any]:
    """Drift insurance for the synthetic 1С debt-entry line(s) (config trap b).

    Verifies (1) exactly one live Product is named the configured synthetic name and that every
    such Product is in the EFFECTIVE exclusion set (env IDs ∪ the dynamically-resolved live ID),
    and (2) no UNLISTED ProductID dominates turnover — its turnover must not exceed
    `synthetic_drift_turnover_ratio` x the 2nd-ranked product. A new synthetic SKU that escaped
    the set would top this ranking and silently re-inflate turnover, so it is flagged here.
    Read-only; never raises (callers decide on `ok`).
    """
    s = get_settings()
    listed = sorted(synthetic_product_ids())
    ph, syn = in_clause("synthetic", listed)

    named = query(
        "SELECT ID FROM dbo.Product WHERE Name = :nm AND Deleted = 0",
        {"nm": s.synthetic_line_product_name},
    )
    named_ids = [int(r["ID"]) for r in named]

    ranked = query(
        """
        SELECT TOP 2 oi.ProductID AS product_id,
               SUM(
                   CAST(oi.Qty AS decimal(19, 6))
                   * CAST(oi.PricePerItem AS decimal(19, 6))
               ) AS turnover
        FROM dbo.OrderItem oi
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN """ + ph + """
        GROUP BY oi.ProductID
        ORDER BY SUM(
            CAST(oi.Qty AS decimal(19, 6))
            * CAST(oi.PricePerItem AS decimal(19, 6))
        ) DESC
        """,
        syn,
    )
    top = ranked[0] if ranked else None
    second = ranked[1] if len(ranked) > 1 else None
    top_turnover = as_decimal(top["turnover"]) if top else Decimal(0)
    second_turnover = as_decimal(second["turnover"]) if second else Decimal(0)
    dominates = (
        top is not None
        and second is not None
        and second_turnover > 0
        and top_turnover
        > as_decimal(s.synthetic_drift_turnover_ratio) * second_turnover
    )

    name_ok = len(named_ids) == 1 and set(named_ids).issubset(set(listed))
    return {
        "ok": name_ok and not dominates,
        "named_product_ids": named_ids,
        "configured_ids": listed,
        "name_ok": name_ok,
        "unlisted_dominant_product_id": (int(top["product_id"]) if dominates else None),
        "top_unlisted_turnover": (
            float(round_cent(top_turnover)) if dominates else None
        ),
        "second_turnover": (
            float(round_cent(second_turnover)) if dominates else None
        ),
    }


def overdue_amount_eur(client_id: int, as_of_date: str, fx_date: str) -> float:
    """overdue_amount (Debt-live path): SUM(Debt.Total -> EUR) for debts older than the
    agreement grace (Agreement.NumberDaysDebt), evaluated as of as_of_date.

    ClientInDebt(Deleted=0) JOIN Debt(Deleted=0) JOIN Agreement; lateness =
    DATEDIFF(day, Debt.Created, :asof) > Agreement.NumberDaysDebt, and only debts created on or
    before :asof count (no future debt leaks into a back-dated valuation). EUR via the pinned
    fx_date. When :asof is today this reproduces the GETUTCDATE() behavior. Only meaningful when
    debt_sync_is_live() is True.
    """
    rows = query(
        """
        SELECT ISNULL(SUM(
            CAST(dbo.GetExchangedToEuroValue(
                d.Total, a.CurrencyID, :fxdate
            ) AS decimal(38, 6))
        ), 0) AS overdue
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.ClientID = :cid
              AND cid.Deleted = 0
              AND d.Deleted = 0
              AND d.Created <= :asof
              AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt
        """,
        {"cid": client_id, "asof": as_of_date, "fxdate": fx_date},
    )
    return float(round_cent(rows[0]["overdue"])) if rows else 0.0


def monthly_turnover_series(client_id: int, as_of_date: str, window_months: int,
                            fx_date: str) -> list[dict[str, Any]]:
    """Per-month turnover (EUR) for the turnover_trend / turnover_vs_exposure charts."""
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT FORMAT(s.Created, 'yyyy-MM') AS period,
               ISNULL(SUM(
                   CAST(oi.Qty AS decimal(19, 6))
                   * CAST(oi.PricePerItem AS decimal(19, 6))
               ), 0) AS turnover_eur
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.ClientID = :cid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, :neg_months, :asof)
        GROUP BY FORMAT(s.Created, 'yyyy-MM')
        ORDER BY period
        """,
        {
            "cid": client_id, "asof": as_of_date, "neg_months": -window_months,
            "fxdate": fx_date, **syn,
        },
    )
    return [
        {
            "period": r["period"],
            "turnover_eur": round_cent(r["turnover_eur"] or Decimal(0)),
        }
        for r in rows
    ]
