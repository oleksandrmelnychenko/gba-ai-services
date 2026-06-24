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

from typing import Any

from app.core.config import get_settings
from app.data.db import in_clause, query

# Regular-sale payment statuses (SalePaymentStatusType). Mapped, never hardcoded inline.
_SALE_PAID = (1, 2)          # Paid, Overpaid
_SALE_PARTIAL = (3,)         # PartialPaid
_SALE_NOTPAID = (0,)         # NotPaid
_SALE_REFUND = (4,)          # Refund -> EXCLUDED from the discipline ratio
_SALE_OPEN_UNPAID = (0, 3)   # NotPaid + PartialPaid -> open exposure proxy


def _synthetic_not_in() -> tuple[str, dict[str, Any]]:
    """Parameterized 'NOT IN (...)' over every configured synthetic 1С debt-entry ProductID."""
    ids = sorted(get_settings().synthetic_line_product_ids)
    placeholder, params = in_clause("synthetic", ids)
    return placeholder, params


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
            oi.Qty * oi.PricePerItem
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
    return float(rows[0]["turnover"]) if rows else 0.0


def turnover_eur_by_currency(client_id: int, as_of_date: str, window_months: int,
                             fx_date: str) -> list[dict[str, Any]]:
    """Per-currency turnover (EUR-normalized) for the currency_breakdown output (trap e)."""
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT a.CurrencyID AS currency_id,
               ISNULL(SUM(
                   oi.Qty * oi.PricePerItem
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
            "turnover_eur": float(r["turnover_eur"] or 0.0),
        }
        for r in rows
    ]


def activity_stats(client_id: int, as_of_date: str, window_months: int) -> dict[str, Any]:
    """order_count / tenure_months / recency_days.

    order_count = COUNT(DISTINCT Sale.ID) in window.
    tenure_months = DATEDIFF(month, MIN(Sale.Created excl sentinel 1980-01-01), as_of) over ALL
    history (not windowed). recency_days = DATEDIFF(day, MAX(Sale.Created), as_of) over history.
    """
    s = get_settings()
    rows = query(
        """
        SELECT
            (SELECT COUNT(DISTINCT s2.ID)
             FROM dbo.Sale s2
             JOIN dbo.ClientAgreement ca2 ON ca2.ID = s2.ClientAgreementID
             WHERE ca2.ClientID = :cid
                   AND s2.Created <= :asof
                   AND s2.Created >= DATEADD(month, :neg_months, :asof)) AS order_count,
            DATEDIFF(month,
                MIN(CASE WHEN s.Created > :sentinel THEN s.Created END), :asof) AS tenure_months,
            DATEDIFF(day, MAX(s.Created), :asof) AS recency_days
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid AND s.Created <= :asof
        """,
        {"cid": client_id, "asof": as_of_date, "neg_months": -window_months,
         "sentinel": s.tenure_sentinel_date},
    )
    r = rows[0] if rows else {}
    return {
        "order_count": int(r.get("order_count") or 0),
        "tenure_months": int(r.get("tenure_months") or 0),
        "recency_days": int(r.get("recency_days")) if r.get("recency_days") is not None else None,
    }


def return_qty_rate(client_id: int, as_of_date: str, window_months: int) -> float:
    """(5) return_qty_rate = SUM(SaleReturnItem.Qty WHERE SaleReturn.IsCanceled=0)
    / SUM(OrderItem.Qty). Validity via IsCanceled / IsValidForCurrentSale (trap a).
    """
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT
            (SELECT ISNULL(SUM(sri.Qty), 0)
             FROM dbo.SaleReturn sr
             JOIN dbo.SaleReturnItem sri ON sri.SaleReturnID = sr.ID
             WHERE sr.ClientID = :cid
                   AND sr.IsCanceled = 0
                   AND sr.Created <= :asof
                   AND sr.Created >= DATEADD(month, :neg_months, :asof)) AS return_qty,
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

    Verifies (1) exactly one Product is named the configured synthetic name and that every such
    Product is in the configured exclusion set, and (2) no UNLISTED ProductID dominates turnover
    — its turnover must not exceed `synthetic_drift_turnover_ratio` x the 2nd-ranked product. A
    new synthetic SKU that escaped the set would top this ranking and silently re-inflate
    turnover, so it is flagged here. Read-only; never raises (callers decide on `ok`).
    """
    s = get_settings()
    listed = sorted(s.synthetic_line_product_ids)
    ph, syn = in_clause("synthetic", listed)

    named = query(
        "SELECT ID FROM dbo.Product WHERE Name = :nm",
        {"nm": s.synthetic_line_product_name},
    )
    named_ids = [int(r["ID"]) for r in named]

    ranked = query(
        """
        SELECT TOP 2 oi.ProductID AS product_id, SUM(oi.Qty * oi.PricePerItem) AS turnover
        FROM dbo.OrderItem oi
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN """ + ph + """
        GROUP BY oi.ProductID
        ORDER BY SUM(oi.Qty * oi.PricePerItem) DESC
        """,
        syn,
    )
    top = ranked[0] if ranked else None
    second = ranked[1] if len(ranked) > 1 else None
    top_turnover = float(top["turnover"]) if top else 0.0
    second_turnover = float(second["turnover"]) if second else 0.0
    dominates = (
        top is not None
        and second is not None
        and second_turnover > 0
        and top_turnover > s.synthetic_drift_turnover_ratio * second_turnover
    )

    name_ok = len(named_ids) == 1 and set(named_ids).issubset(set(listed))
    return {
        "ok": name_ok and not dominates,
        "named_product_ids": named_ids,
        "configured_ids": listed,
        "name_ok": name_ok,
        "unlisted_dominant_product_id": (int(top["product_id"]) if dominates else None),
        "top_unlisted_turnover": top_turnover if dominates else None,
        "second_turnover": second_turnover if dominates else None,
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
            dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fxdate)
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
    return float(rows[0]["overdue"]) if rows else 0.0


def monthly_turnover_series(client_id: int, as_of_date: str, window_months: int,
                            fx_date: str) -> list[dict[str, Any]]:
    """Per-month turnover (EUR) for the turnover_trend / turnover_vs_exposure charts."""
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT FORMAT(s.Created, 'yyyy-MM') AS period,
               ISNULL(SUM(
                   oi.Qty * oi.PricePerItem
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
        {"period": r["period"], "turnover_eur": float(r["turnover_eur"] or 0.0)}
        for r in rows
    ]
