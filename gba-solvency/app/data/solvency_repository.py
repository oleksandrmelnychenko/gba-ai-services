"""Parameterized read queries for the CreditScore-100 solvency engine.

All SQL is parameterized (:name) — no f-string interpolation. Every query honors the
discovery data traps:
  (a) A sale is gba-server's confirmed sale (app.data.source_rules): Sale.Deleted=0, invoiced,
      not an accounting carrier; lines OrderItem.Deleted=0, IsValidForCurrentSale=1, Qty>0.
      Open debt is a ClientInDebt/Debt pair, both Deleted=0, with Debt.Total > 0.
  (b) Synthetic 1С debt-entry line (ProductID 25422404) is EXCLUDED from turnover/activity
      but KEPT in debt/exposure (it is real carried debt).
  (c) FX snapshot date is pinned per run (GetExchangedToEuroValue revalues at call time).
  (d) BaseSalePaymentStatus.Amount=0 even when Paid -> use the status ENUM (count-based).
  (e) Multi-currency: EUR-normalize via dbo.GetExchangedToEuroValue.

A sale is dated by its invoice instant, Sale.ChangedToInvoice (UTC), as in gba-server's sales
reports. The turnover window is complete Europe/Kyiv days: [as_of - window_months, as_of),
passed as UTC instants (app.core.history.sales_window). Ages in days and months compare the
Europe/Kyiv date of the stored instant with the as-of date.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.core.history import sales_window
from app.data.db import in_clause, query
from app.data.source_rules import (
    convertible_currencies,
    debt_agreement,
    confirmed_line,
    confirmed_sale,
    days_since,
    local_month,
    months_since,
    open_debt,
    sold_at,
)
from app.data.synthetic_product import synthetic_product_ids
from app.domain.money import as_decimal, round_cent

# Regular-sale payment statuses (SalePaymentStatusType). Mapped, never hardcoded inline.
_SALE_PAID = (1, 2)          # Paid, Overpaid
_SALE_PARTIAL = (3,)         # PartialPaid
_SALE_NOTPAID = (0,)         # NotPaid
_SALE_REFUND = (4,)          # Refund -> EXCLUDED from the discipline ratio
_SALE_OPEN_UNPAID = (0, 3)   # NotPaid + PartialPaid -> open exposure proxy
_READINESS_TTL_S = 60.0
# MSSQL caps a statement at 2,100 parameters; id lists are chunked well below that.
_ID_CHUNK = 900
_readiness_lock = threading.Lock()
_readiness_state: dict[int, tuple[float, dict[str, Any]]] = {}


def _synthetic_not_in() -> tuple[str, dict[str, Any]]:
    """Parameterized 'NOT IN (...)' over every effective synthetic 1С debt-entry ProductID."""
    ids = sorted(synthetic_product_ids())
    placeholder, params = in_clause("synthetic", ids)
    return placeholder, params


def _history_start() -> str:
    return get_settings().source_history_start_date.isoformat()


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
                WHERE d.Deleted = 0 AND d.Created IS NOT NULL
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
        "source_history_start": settings.source_history_start_date.isoformat(),
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


def has_hopeless_debt_role(client_id: int) -> bool:
    """True when the entity sits in the «Безнадійна заборгованість» client group.

    The group is a 1С master-data verdict: these debtors carry no open-debt rows, sales or
    payments in the imported history, so every debt feature is empty and the scorecard alone
    would rate them A. The role is matched by name because role IDs are re-minted by syncs.
    """
    rows = query(
        """
        SELECT TOP 1 1 AS hit
        FROM dbo.ClientInRole cir
        JOIN dbo.ClientTypeRole ctr ON ctr.ID = cir.ClientTypeRoleID
        WHERE cir.ClientID = :cid
              AND cir.Deleted = 0
              AND ctr.Name = :hopeless_role
        """,
        {"cid": client_id, "hopeless_role": get_settings().hopeless_debt_role_name},
    )
    return bool(rows)


@dataclass(frozen=True)
class ClientGate:
    is_buyer: bool
    hopeless_debt: bool


def _id_chunks(client_ids: Iterable[int]) -> Iterator[list[int]]:
    ids = list(dict.fromkeys(int(c) for c in client_ids))
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start:start + _ID_CHUNK]


def client_gates(client_ids: Iterable[int]) -> dict[int, ClientGate]:
    """Set-based client_exists + has_buyer_role + has_hopeless_debt_role for an id list.

    One statement per chunk of ids instead of three round trips per id. An id that is absent
    from the result does not exist in dbo.Client; the predicates are the same as the per-client
    helpers above.
    """
    gates: dict[int, ClientGate] = {}
    hopeless_role = get_settings().hopeless_debt_role_name
    for chunk in _id_chunks(client_ids):
        ph, params = in_clause("cid", chunk)
        rows = query(
            f"""
            SELECT c.ID AS client_id,
                   CASE WHEN EXISTS (
                       SELECT 1
                       FROM dbo.ClientInRole cir
                       JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
                       WHERE cir.ClientID = c.ID
                             AND cir.Deleted = 0
                             AND ct.[Type] = 0
                   ) THEN 1 ELSE 0 END AS is_buyer,
                   CASE WHEN EXISTS (
                       SELECT 1
                       FROM dbo.ClientInRole cir
                       JOIN dbo.ClientTypeRole ctr ON ctr.ID = cir.ClientTypeRoleID
                       WHERE cir.ClientID = c.ID
                             AND cir.Deleted = 0
                             AND ctr.Name = :hopeless_role
                   ) THEN 1 ELSE 0 END AS hopeless_debt
            FROM dbo.Client c
            WHERE c.ID IN {ph}
            """,
            {"hopeless_role": hopeless_role, **params},
        )
        for row in rows:
            gates[int(row["client_id"])] = ClientGate(
                is_buyer=bool(row["is_buyer"]),
                hopeless_debt=bool(row["hopeless_debt"]),
            )
    return gates


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
              AND s.Created >= :history_start
        GROUP BY bsps.SalePaymentStatusType
        """,
        {
            "cid": client_id,
            "asof": as_of_date,
            "neg_months": -window_months,
            "history_start": _history_start(),
        },
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
              AND s.Created >= :history_start
              AND bsps.SalePaymentStatusType IN {placeholder}
        """,
        {
            "cid": client_id,
            "asof": as_of_date,
            "neg_months": -window_months,
            "history_start": _history_start(),
            **params,
        },
    )
    r = rows[0] if rows else {}
    return {
        "open_count": int(r.get("open_count") or 0),
        "max_age_days": int(r.get("max_age_days") or 0),
        "avg_age_days": float(r.get("avg_age_days") or 0.0),
    }


def debt_aging_buckets_truth(client_id: int, as_of_date: str,
                             fx_date: str) -> list[dict[str, Any]]:
    """TRUTH aging buckets (0-30 / 31-60 / 61-90 / 90+) from real overdue debt — the same source
    the model's aging features (app/risk/dataset.feat_debt_aging) and overdue_amount_eur use.

    A debt line is overdue by od = days from the Kyiv date of Debt.Created to :asof -
    Agreement.NumberDaysDebt;
    only past-grace lines (od > 0) are bucketed (within-grace carried trade credit is not "open
    overdue"). Count is the line count; amount_eur is the EUR-normalized Debt.Total via the pinned
    fx_date (dbo.GetExchangedToEuroValue). Replaces the stale BaseSalePaymentStatus.NotPaid proxy
    (only 3.3% of NotPaid sales have a live debt row, so the enum aging was ~30x inflated). Every
    debt line counts incl. the synthetic 1С line — it is real carried debt (trap b: kept in debt).
    """
    rows = query(
        f"""
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
                SELECT {days_since("d.Created", ":asof")} - a.NumberDaysDebt AS od,
                       CAST(dbo.GetExchangedToEuroValue(
                           d.Total, a.CurrencyID, :fxdate
                       ) AS decimal(38, 6)) AS e
                FROM dbo.ClientInDebt cid
                JOIN dbo.Debt d ON d.ID = cid.DebtID
                {debt_agreement(":fxdate")}
                WHERE cid.ClientID = :cid
                      AND {open_debt()}
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
    """Discipline donut over one population: the buyer's confirmed sales of the window.

    Each sale is settled (no open debt line), current (open line within the agreement grace)
    or overdue (an open line past grace), so the three slices add up to the window's sales.
    Open lines whose sale lies outside the window (older invoices, opening balances) are not
    part of that population; they are counted in outside_window_lines and stay in the aging
    bars and the exposure line.
    """
    window = sales_window(as_of_date, window_months)
    params = {"cid": client_id, "asof": as_of_date, "fxdate": fx_date, **window.params()}
    rows = query(
        f"""
        SELECT
            SUM(CASE WHEN line.lines = 0 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN line.lines > 0 AND ISNULL(line.od, 0) <= 0 THEN 1 ELSE 0 END)
                AS current_sales,
            SUM(CASE WHEN line.od > 0 THEN 1 ELSE 0 END) AS overdue_sales,
            ISNULL(SUM(CASE WHEN line.lines > 0 AND ISNULL(line.od, 0) <= 0 THEN line.e END), 0)
                AS current_eur,
            ISNULL(SUM(CASE WHEN line.od > 0 THEN line.e END), 0) AS overdue_eur,
            ISNULL(SUM(line.lines), 0) AS window_lines
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        CROSS APPLY (
            SELECT COUNT(*) AS lines,
                   MAX({days_since("d.Created", ":asof")} - a.NumberDaysDebt) AS od,
                   SUM(CAST(dbo.GetExchangedToEuroValue(
                       d.Total, a.CurrencyID, :fxdate
                   ) AS decimal(38, 6))) AS e
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            {debt_agreement(":fxdate")}
            WHERE cid.SaleID = s.ID
                  AND cid.ClientID = :cid
                  AND {open_debt()}
                  AND d.Created <= :asof
        ) line
        WHERE ca.ClientID = :cid
              AND {confirmed_sale()}
              AND {sold_at()} >= :history_start
              AND {sold_at()} >= :window_start
              AND {sold_at()} < :window_end
        """,
        params,
    )
    open_lines = query(
        f"""
        SELECT COUNT(*) AS lines
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        {debt_agreement(":fxdate")}
        WHERE cid.ClientID = :cid
              AND {open_debt()}
              AND d.Created <= :asof
        """,
        {"cid": client_id, "asof": as_of_date, "fxdate": fx_date},
    )
    r = rows[0] if rows else {}
    all_lines = int(open_lines[0]["lines"] or 0) if open_lines else 0
    return {
        "settled": int(r.get("settled") or 0),
        "current": int(r.get("current_sales") or 0),
        "overdue": int(r.get("overdue_sales") or 0),
        "current_eur": round_cent(r.get("current_eur") or Decimal(0)),
        "overdue_eur": round_cent(r.get("overdue_eur") or Decimal(0)),
        "outside_window_lines": max(all_lines - int(r.get("window_lines") or 0), 0),
    }


def unconverted_debt(client_ids: Iterable[int], as_of_date: str,
                     fx_date: str) -> dict[int, list[dict[str, Any]]]:
    """Open debt lines no EUR sum contains: their agreement currency has no rate on fx_date."""
    out: dict[int, list[dict[str, Any]]] = {}
    for chunk in _id_chunks(client_ids):
        placeholder, params = in_clause("uc", chunk)
        for r in query(
            f"""
            SELECT cid.ClientID AS client_id, a.CurrencyID AS currency_id,
                   currency.Code AS currency, COUNT(*) AS lines, SUM(d.Total) AS amount
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            LEFT JOIN dbo.Currency currency ON currency.ID = a.CurrencyID
            WHERE cid.ClientID IN {placeholder}
                  AND {open_debt()}
                  AND d.Created <= :asof
                  AND NOT EXISTS (
                      SELECT 1 FROM {convertible_currencies(":fxdate")} fx
                      WHERE fx.ID = a.CurrencyID
                  )
            GROUP BY cid.ClientID, a.CurrencyID, currency.Code
            ORDER BY cid.ClientID, a.CurrencyID
            """,
            {"asof": as_of_date, "fxdate": fx_date, **params},
        ):
            out.setdefault(int(r["client_id"]), []).append({
                "currency_id": None if r["currency_id"] is None else int(r["currency_id"]),
                "currency": r["currency"],
                "lines": int(r["lines"]),
                "amount": round_cent(r["amount"] or Decimal(0)),
            })
    return out


def total_sales_count(client_id: int, as_of_date: str, window_months: int) -> int:
    """total_sales_12mo — denominator for the live DebtLoad proxy."""
    rows = query(
        """
        SELECT COUNT(DISTINCT s.ID) AS n
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND s.Deleted = 0
              AND s.Created < :window_end
              AND s.Created >= :window_start
              AND s.Created >= :history_start
        """,
        {"cid": client_id, **sales_window(as_of_date, window_months).params()},
    )
    return int(rows[0]["n"]) if rows else 0


def limit_utilization_basis(client_id: int, as_of_date: str) -> list[dict[str, Any]]:
    """Open debt against the credit limit, per amount-controlled agreement with a limit.

    Mirrors gba-server's sale-time limit check: Agreement.AmountDebt is in the agreement
    currency and is compared with the client's positive open debt (Debt.Total) on that
    agreement and on every other agreement of the same currency. No conversion is involved.
    Returns no rows when the client has no live agreement with IsControlAmountDebt=1 and
    AmountDebt > 0 — the caller reports that as "no limit", never as 0%.
    """
    rows = query(
        f"""
        SELECT a.ID AS agreement_id,
               a.CurrencyID AS currency_id,
               a.AmountDebt AS limit_amount,
               ISNULL(debt.amount, 0) AS debt_amount
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        OUTER APPLY (
            SELECT SUM(d.Total) AS amount
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement da ON da.ID = cid.AgreementID
            WHERE cid.ClientID = ca.ClientID
                  AND {open_debt()}
                  AND d.Created <= :asof
                  AND (da.ID = a.ID OR da.CurrencyID = a.CurrencyID)
        ) debt
        WHERE ca.ClientID = :cid
              AND ca.Deleted = 0
              AND a.Deleted = 0
              AND a.IsControlAmountDebt = 1
              AND a.AmountDebt > 0
        ORDER BY a.ID
        """,
        {"cid": client_id, "asof": as_of_date},
    )
    return [
        {
            "agreement_id": int(r["agreement_id"]),
            "currency_id": int(r["currency_id"]) if r["currency_id"] is not None else None,
            "limit_amount": as_decimal(r["limit_amount"]),
            "debt_amount": as_decimal(r["debt_amount"]),
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
    IsValidForCurrentSale=1, NO Deleted=0 filter, ProductID NOT IN synthetic set, and the shared
    source-history boundary.
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
              AND {confirmed_sale()}
              AND {confirmed_line()}
              AND oi.ProductID NOT IN {ph}
              AND {sold_at()} >= :history_start
              AND {sold_at()} >= :window_start
              AND {sold_at()} < :window_end
        """,
        {
            "cid": client_id, **sales_window(as_of_date, window_months).params(), **syn,
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
              AND {confirmed_sale()}
              AND {confirmed_line()}
              AND oi.ProductID NOT IN {ph}
              AND {sold_at()} >= :history_start
              AND {sold_at()} >= :window_start
              AND {sold_at()} < :window_end
        GROUP BY a.CurrencyID
        ORDER BY a.CurrencyID
        """,
        {
            "cid": client_id, **sales_window(as_of_date, window_months).params(), **syn,
        },
    )
    return [
        {
            "currency_id": int(r["currency_id"]) if r["currency_id"] is not None else None,
            "turnover_eur": round_cent(r["turnover_eur"] or Decimal(0)),
        }
        for r in rows
    ]


def turnover_eur_by_currency_many(
    client_ids: Iterable[int], as_of_date: str, window_months: int, fx_date: str
) -> dict[int, list[dict[str, Any]]]:
    """turnover_eur_by_currency for an id list: same filters, grouped by client and currency.

    Returns {client_id: rows} for every requested id (an empty list when the client has no
    sales in the window), each list ordered by currency like the per-client query.
    """
    ph_syn, syn = _synthetic_not_in()
    out: dict[int, list[dict[str, Any]]] = {}
    for chunk in _id_chunks(client_ids):
        out.update({cid: [] for cid in chunk})
        ph, params = in_clause("cid", chunk)
        rows = query(
            f"""
            SELECT ca.ClientID AS client_id,
                   a.CurrencyID AS currency_id,
                   ISNULL(SUM(
                       CAST(oi.Qty AS decimal(19, 6))
                       * CAST(oi.PricePerItem AS decimal(19, 6))
                   ), 0) AS turnover_eur
            FROM dbo.Sale s
            JOIN dbo.[Order] o ON o.ID = s.OrderID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
            JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE ca.ClientID IN {ph}
                  AND {confirmed_sale()}
                  AND {confirmed_line()}
                  AND oi.ProductID NOT IN {ph_syn}
                  AND {sold_at()} >= :history_start
                  AND {sold_at()} >= :window_start
                  AND {sold_at()} < :window_end
            GROUP BY ca.ClientID, a.CurrencyID
            ORDER BY ca.ClientID, a.CurrencyID
            """,
            {**sales_window(as_of_date, window_months).params(), **params, **syn},
        )
        for r in rows:
            out[int(r["client_id"])].append(
                {
                    "currency_id": (
                        int(r["currency_id"]) if r["currency_id"] is not None else None
                    ),
                    "turnover_eur": round_cent(r["turnover_eur"] or Decimal(0)),
                }
            )
    return out


def activity_stats(client_id: int, as_of_date: str, window_months: int) -> dict[str, Any]:
    """order_count / tenure_months / recency_days, over REAL sales only.

    order_count = COUNT(DISTINCT Sale.ID) in window.
    tenure_months = months from the Kyiv date of the first invoice to as_of over all available
    history from SOURCE_HISTORY_START_DATE. recency_days uses the same bounded available history.

    A Sale only counts if it carries a real, valid, non-synthetic line — i.e. EXISTS a
    OrderItem with IsValidForCurrentSale=1 and ProductID not in the synthetic-1С set. Without
    this gate the ~21.7k standalone single-line debt-injection Sales (synthetic ProductID) would
    inflate order_count and skew tenure/recency for affected clients (data trap (b)).
    """
    ph, syn = _synthetic_not_in()
    rows = query(
        f"""
        SELECT
            (SELECT COUNT(DISTINCT s2.ID)
             FROM dbo.Sale s2
             JOIN dbo.ClientAgreement ca2 ON ca2.ID = s2.ClientAgreementID
             WHERE ca2.ClientID = :cid
                   AND {confirmed_sale("s2")}
                   AND {sold_at("s2")} >= :history_start
                   AND {sold_at("s2")} >= :window_start
                   AND {sold_at("s2")} < :window_end
                   AND EXISTS (
                       SELECT 1 FROM dbo.OrderItem oi2
                       WHERE oi2.OrderID = s2.OrderID
                             AND oi2.ProductID NOT IN {ph}
                             AND {confirmed_line("oi2")}
                   )) AS order_count,
            {months_since(f"MIN({sold_at()})", ":asof")} AS tenure_months,
            {days_since(f"MAX({sold_at()})", ":asof")} AS recency_days
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND {confirmed_sale()}
              AND {sold_at()} >= :history_start
              AND {sold_at()} < :window_end
              AND EXISTS (
                  SELECT 1 FROM dbo.OrderItem oi
                  WHERE oi.OrderID = s.OrderID
                        AND oi.ProductID NOT IN {ph}
                        AND {confirmed_line()}
              )
        """,
        {
            "cid": client_id,
            "asof": as_of_date,
            **sales_window(as_of_date, window_months).params(),
            **syn,
        },
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
                      AND sr.FromDate < :window_end
                      AND sr.FromDate >= :window_start
                      AND sr.FromDate >= :history_start
                GROUP BY sri.SaleReturnID, sri.OrderItemID
             ) line) AS return_qty,
            (SELECT ISNULL(SUM(oi.Qty), 0)
             FROM dbo.Sale s3
             JOIN dbo.[Order] o ON o.ID = s3.OrderID
             JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
             JOIN dbo.ClientAgreement ca3 ON ca3.ID = s3.ClientAgreementID
             WHERE ca3.ClientID = :cid
                   AND {confirmed_sale("s3")}
                   AND {confirmed_line()}
                   AND oi.ProductID NOT IN {ph}
                   AND {sold_at("s3")} < :window_end
                   AND {sold_at("s3")} >= :window_start
                   AND {sold_at("s3")} >= :history_start) AS sold_qty
        """,
        {"cid": client_id, **sales_window(as_of_date, window_months).params(), **syn},
    )
    if not rows:
        return 0.0
    sold = float(rows[0]["sold_qty"] or 0.0)
    if sold <= 0:
        return 0.0
    return float(rows[0]["return_qty"] or 0.0) / sold


def debt_sync_is_live() -> bool:
    """Probe whether the Debt table is quiesced / usable.

    Live when there are rows with Deleted=0 and a business timestamp. If the sync is in
    progress every row is Deleted=1, so this returns False and the engine falls back to the
    live open-unpaid proxy. Recorded as SolvencyScore.debt_load_source.
    """
    rows = query(
        """
        SELECT COUNT(*) AS live_rows
        FROM dbo.Debt
        WHERE Deleted = 0 AND Created IS NOT NULL
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
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN """ + ph + """
              AND o.Created >= :history_start
              AND o.Created <= :asof
        GROUP BY oi.ProductID
        ORDER BY SUM(
            CAST(oi.Qty AS decimal(19, 6))
            * CAST(oi.PricePerItem AS decimal(19, 6))
        ) DESC
        """,
        {
            **syn,
            "history_start": _history_start(),
            "asof": datetime.now(),
        },
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

    ClientInDebt(Deleted=0) JOIN Debt(Deleted=0) JOIN Agreement; lateness = days from the Kyiv
    date of Debt.Created to :asof > Agreement.NumberDaysDebt, and only debts created on or
    before :asof count (no future debt leaks into a back-dated valuation). EUR via the pinned
    fx_date. When :asof is today this reproduces the GETUTCDATE() behavior. Only meaningful when
    debt_sync_is_live() is True.
    """
    rows = query(
        f"""
        SELECT ISNULL(SUM(
            CAST(dbo.GetExchangedToEuroValue(
                d.Total, a.CurrencyID, :fxdate
            ) AS decimal(38, 6))
        ), 0) AS overdue
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        {debt_agreement(":fxdate")}
        WHERE cid.ClientID = :cid
              AND {open_debt()}
              AND d.Created <= :asof
              AND {days_since("d.Created", ":asof")} > a.NumberDaysDebt
        """,
        {"cid": client_id, "asof": as_of_date, "fxdate": fx_date},
    )
    return float(round_cent(rows[0]["overdue"])) if rows else 0.0


def monthly_turnover_series(client_id: int, as_of_date: str, window_months: int,
                            fx_date: str) -> list[dict[str, Any]]:
    """Per-month turnover (EUR) for the turnover_trend / turnover_vs_exposure charts.

    Months are Europe/Kyiv calendar months of confirmed sales by invoice date. The series
    starts at the first Kyiv month start inside the window, so it never opens with a partial
    month; the last point is the month of the previous Kyiv day, to date. Every month in
    between is present: a month without sales is an explicit zero.
    """
    ph, syn = _synthetic_not_in()
    window = sales_window(as_of_date, window_months)
    rows = query(
        f"""
        SELECT {local_month(sold_at())} AS period,
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
              AND {confirmed_sale()}
              AND {confirmed_line()}
              AND oi.ProductID NOT IN {ph}
              AND {sold_at()} >= :history_start
              AND {sold_at()} >= :window_start
              AND {sold_at()} < :window_end
        GROUP BY {local_month(sold_at())}
        ORDER BY period
        """,
        {"cid": client_id, **window.params(), "window_start": window.series_start, **syn},
    )
    by_period = {r["period"]: r["turnover_eur"] for r in rows}
    return [
        {
            "period": period,
            "turnover_eur": round_cent(by_period.get(period) or Decimal(0)),
        }
        for period in window.periods
    ]
