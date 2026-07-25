"""Read-only signal queries over ConcordDb_V5 for task generation. All parameterized.

Verified columns:
  Client(ID, MainManagerID, FullName, Name, MobileNumber, EmailAddress, Created, Deleted)
  Debt(ID, Created, Days, Total, Deleted)  -- Days (prod-computed overdue) is ALWAYS 0 here; use Created
  ClientInDebt(ID, AgreementID, ClientID, DebtID, Deleted, SaleID, ReSaleID)
  ClientAgreement(ID, ClientID, AgreementID)
  Agreement(ID, NumberDaysDebt, AmountDebt, CurrencyID)
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from app.core import history
from app.core.config import get_settings
from app.core.money import cents, cents_decimal, decimal_value
from app.data.db import in_clause, query


@lru_cache(maxsize=64)
def ubiquitous_product_ids(pct: float, as_of: str) -> frozenset[int]:
    """Products bought by more than `pct` of distinct clients in the deterministic trailing 12mo.

    The rolling start is clamped to the canonical source-history boundary, and `as_of` is explicit
    so historical generation/training never depends on wall-clock GETDATE().

    Synthetic accounting
    lines / universal staples (e.g. "Ввід боргів"/debt-entry, ~75% of clients) that aren't real
    sellable products. Excluded from reorder/monetary signals so they don't generate nonsensical
    'reorder the debt-entry' tasks or inflate a client's turnover. Cached per pct + as_of."""
    window = history.rolling_months(as_of, 12)
    rows = query(
        """
        WITH base AS (
            SELECT ca.ClientID AS cid, oi.ProductID AS pid
            FROM dbo.[Order] o
            JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                  AND o.Created >= :start AND o.Created < :asof
        ),
        tot AS (SELECT COUNT(DISTINCT cid) AS n FROM base)
        SELECT b.pid AS pid
        FROM base b CROSS JOIN tot
        GROUP BY b.pid, tot.n
        HAVING COUNT(DISTINCT b.cid) * 1.0 / NULLIF(tot.n, 0) > :pct
        """,
        {
            "pct": pct,
            "start": window.effective_start.isoformat(),
            "asof": window.as_of.isoformat(),
        },
    )
    return frozenset(int(r["pid"]) for r in rows)


_SYNTHETIC_REFRESH_S = 3600.0
_synthetic_state: dict = {"at": 0.0, "ids": frozenset()}
_SOURCE_READINESS_TTL_S = 60.0
_source_readiness_state: dict = {
    "at": 0.0,
    "max_lag_days": None,
    "source_history_start": None,
    "value": None,
}


def synthetic_product_ids() -> frozenset[int]:
    """Live id(s) of the synthetic debt-entry product («Ввід боргів»), resolved dynamically so the
    hard exclusion survives dev re-mints (the old pinned 25422404 is a dead row; the live row today
    is 29555414). settings.synthetic_product_ids, when set via env, is an explicit override.
    Cached in-process, refreshed hourly or on an empty resolve."""
    override = get_settings().synthetic_product_ids
    if override:
        return frozenset(override)
    now = time.monotonic()
    if _synthetic_state["ids"] and now - _synthetic_state["at"] < _SYNTHETIC_REFRESH_S:
        return _synthetic_state["ids"]
    rows = query(
        "SELECT TOP 1 ID AS id FROM dbo.Product WHERE Name = :nm AND Deleted = 0 ORDER BY ID DESC",
        {"nm": "Ввід боргів"},
    )
    ids = frozenset(int(r["id"]) for r in rows)
    if ids:
        _synthetic_state.update({"at": now, "ids": ids})
    return ids or _synthetic_state["ids"]


def source_readiness(max_lag_days: int) -> dict[str, Any]:
    """Business-aware SQL source probe, briefly cached for health polling."""
    now_mono = time.monotonic()
    source_start = history.source_history_start().isoformat()
    cached = _source_readiness_state["value"]
    if (
        cached is not None
        and _source_readiness_state["max_lag_days"] == max_lag_days
        and _source_readiness_state["source_history_start"] == source_start
        and now_mono - _source_readiness_state["at"] < _SOURCE_READINESS_TTL_S
    ):
        return dict(cached)

    synthetic_ids = synthetic_product_ids()
    as_of_exclusive = (datetime.now().date() + timedelta(days=1)).isoformat()
    coverage = history.factual_window(as_of_exclusive)
    rows = query(
        """
        SELECT
            (
                SELECT TOP 1 o.Created
                FROM dbo.[Order] o
                JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
                WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                      AND o.Created >= :source_start AND o.Created < :asof
                ORDER BY o.Created DESC, o.ID DESC
            ) AS latest_sale_at,
            (
                SELECT COUNT_BIG(DISTINCT c.MainManagerID)
                FROM dbo.Client c
                JOIN dbo.[User] u ON u.ID = c.MainManagerID AND u.Deleted = 0
                WHERE c.Deleted = 0 AND c.MainManagerID IS NOT NULL
            ) AS manager_count
        """,
        {
            "source_start": coverage.effective_start.isoformat(),
            "asof": coverage.as_of.isoformat(),
        },
    )
    row = rows[0] if rows else {}
    latest = row.get("latest_sale_at")
    manager_count = int(row.get("manager_count") or 0)
    fresh = isinstance(latest, datetime) and latest >= datetime.now() - timedelta(
        days=max_lag_days
    )
    reasons: list[str] = []
    if not synthetic_ids:
        reasons.append("synthetic_product_unresolved")
    if latest is None:
        reasons.append("valid_sales_missing")
    elif not fresh:
        reasons.append("valid_sales_stale")
    if manager_count <= 0:
        reasons.append("active_managers_missing")
    result = {
        "source_ready": not reasons,
        "source_reasons": reasons,
        "latest_sale_at": latest.isoformat() if isinstance(latest, datetime) else None,
        "manager_count": manager_count,
        "synthetic_product_count": len(synthetic_ids),
        **coverage.metadata(),
    }
    _source_readiness_state.update(
        {
            "at": time.monotonic(),
            "max_lag_days": max_lag_days,
            "source_history_start": source_start,
            "value": dict(result),
        }
    )
    return result


def _excluded(as_of: str) -> frozenset[int]:
    """Products excluded from turnover/feature signals: the synthetic accounting ids (debt-entry
    «Ввід боргів», resolved live) UNION the data-driven ubiquity set. The synthetic ids are a HARD
    guard — excluded unconditionally, so the exclusion holds even if the debt-entry's rolling
    12-month ubiquity ever dips below ubiquity_exclude_pct. Mirrors gba-reco/gba-products. The
    ubiquity set still catches any OTHER future universal staple."""
    s = get_settings()
    return synthetic_product_ids() | ubiquitous_product_ids(s.ubiquity_exclude_pct, as_of)


def existing_client_ids(client_ids: list[int]) -> set[int]:
    """Subset of client_ids that still exist as rows in dbo.Client (any Deleted state — soft-deleted
    clients are still real rows; only re-mint/wipe victims are absent)."""
    out: set[int] = set()
    for i in range(0, len(client_ids), 500):
        ph, params = in_clause("c", client_ids[i:i + 500])
        rows = query(f"SELECT ID AS id FROM dbo.Client WHERE ID IN {ph}", params)
        out.update(int(r["id"]) for r in rows)
    return out


def manager_id_for_netuid(net_uid: str) -> int | None:
    rows = query("SELECT ID AS id FROM dbo.[User] WHERE NetUID = :nu AND Deleted = 0", {"nu": net_uid})
    return int(rows[0]["id"]) if rows else None


_HEAD_DASHBOARD_ROLE_TYPES = (6, 3, 8, 12)


def is_head_of_sales(net_uid: str) -> bool:
    """True for head-of-sales (UserRoleType=6) and oversight roles that see the whole
    department: Administrator (3), TopManager (8), GBA (12)."""
    rows = query(
        """
        SELECT 1 AS ok
        FROM dbo.[User] u
        JOIN dbo.UserRole ur ON ur.ID = u.UserRoleID
        WHERE u.NetUID = :nu AND u.Deleted = 0 AND ur.UserRoleType IN (6, 3, 8, 12)
        """,
        {"nu": net_uid},
    )
    return bool(rows)


def head_user_ids() -> list[int]:
    """User.ID of heads of sales (UserRole.UserRoleType = 6) — SLA escalation targets."""
    rows = query(
        """
        SELECT u.ID AS id FROM dbo.[User] u
        JOIN dbo.UserRole ur ON ur.ID = u.UserRoleID
        WHERE u.Deleted = 0 AND ur.UserRoleType = 6
        """,
    )
    return [int(r["id"]) for r in rows]


def manager_names(manager_ids: list[int]) -> dict[int, str]:
    """User.ID -> display name (FirstName LastName), for dashboards. Missing ids omitted."""
    if not manager_ids:
        return {}
    ph, params = in_clause("m", manager_ids)
    rows = query(
        f"""
        SELECT ID AS id, LTRIM(RTRIM(CONCAT(ISNULL(FirstName, ''), ' ', ISNULL(LastName, '')))) AS name
        FROM dbo.[User] WHERE ID IN {ph}
        """,
        params,
    )
    return {int(r["id"]): (r["name"] or "").strip() for r in rows if (r["name"] or "").strip()}


def new_clients_for_manager(manager_id: int, as_of: str, recent_days: int = 90,
                            max_orders: int = 0) -> list[dict]:
    """Recently-created clients with factual order counts over all available source history."""
    created_window = history.rolling_days(as_of, recent_days)
    purchase_window = history.factual_window(as_of)
    return query(
        """
        WITH cand AS (
            SELECT c.ID, c.FullName, c.Name, c.MobileNumber, c.EmailAddress, c.Created
            FROM dbo.Client c
            WHERE c.Deleted = 0 AND c.MainManagerID = :mid
                  AND c.Created >= :created_start AND c.Created < :asof
        ),
        oc AS (
            SELECT ca.ClientID AS cid, COUNT(DISTINCT oi.OrderID) AS n
            FROM dbo.ClientAgreement ca
            JOIN cand ON cand.ID = ca.ClientID
            LEFT JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
                 AND o.Created >= :source_start AND o.Created < :asof
            LEFT JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            GROUP BY ca.ClientID
        )
        SELECT cand.ID AS client_id, cand.FullName AS full_name, cand.Name AS name,
               cand.MobileNumber AS phone, cand.EmailAddress AS email,
               DATEDIFF(day, cand.Created, :asof) AS days_since_created,
               ISNULL(oc.n, 0) AS n_orders
        FROM cand LEFT JOIN oc ON oc.cid = cand.ID
        WHERE ISNULL(oc.n, 0) <= :maxord
        """,
        {
            "mid": manager_id,
            "asof": created_window.as_of.isoformat(),
            "created_start": created_window.effective_start.isoformat(),
            "source_start": purchase_window.effective_start.isoformat(),
            "maxord": max_orders,
        },
    )


def all_managers() -> list[int]:
    """Distinct managers that have at least one client (Client.MainManagerID). Soft-deleted
    managers (dbo.[User].Deleted=1) are excluded so they never surface in team/head views."""
    rows = query(
        """
        SELECT DISTINCT c.MainManagerID AS mid
        FROM dbo.Client c
        JOIN dbo.[User] u ON u.ID = c.MainManagerID AND u.Deleted = 0
        WHERE c.Deleted = 0 AND c.MainManagerID IS NOT NULL
        """,
    )
    return [int(r["mid"]) for r in rows]


def clients_for_manager(manager_id: int) -> list[dict]:
    return query(
        """
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email
        FROM dbo.Client c
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
        """,
        {"mid": manager_id},
    )


def active_clients_for_manager(manager_id: int, as_of: str, recent_days: int = 120,
                               min_orders: int = 3) -> list[dict]:
    """Clients with >= min_orders distinct orders in the last recent_days — the only clients worth
    a cross-sell reco call (reco needs purchase history; cold clients return no discovery). On real
    data this is ~20% of a manager's book, so it cuts reco HTTP calls 4-5x vs. all clients."""
    window = history.rolling_days(as_of, recent_days)
    return query(
        """
        WITH act AS (
            SELECT ca.ClientID AS cid
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
                 AND o.Created >= :start AND o.Created < :asof
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
            GROUP BY ca.ClientID
            HAVING COUNT(DISTINCT o.ID) >= :minord
        )
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email
        FROM act JOIN dbo.Client c ON c.ID = act.cid
        """,
        {
            "mid": manager_id,
            "asof": window.as_of.isoformat(),
            "start": window.effective_start.isoformat(),
            "minord": min_orders,
        },
    )


def contacts_for_clients(client_ids: list[int]) -> dict[int, dict]:
    if not client_ids:
        return {}
    ph, params = in_clause("c", client_ids)
    rows = query(
        f"""
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email
        FROM dbo.Client c WHERE c.Deleted = 0 AND c.ID IN {ph}
        """,
        params,
    )
    return {int(r["client_id"]): r for r in rows}


# --- debt_followup signal ---

_EUR_VALUE_CTE = """
    WITH eur AS (
        SELECT TOP(1) ID AS eur_id FROM dbo.Currency WHERE Deleted = 0 AND Code = 'EUR'
    ),
    rws AS (
        SELECT {extra_select}c.ID AS client_id,
               CAST(CASE
                   WHEN ISNULL(a.CurrencyID, 2) = eur.eur_id THEN CAST(d.Total AS decimal(30,14))
                   WHEN er.rate IS NOT NULL THEN CAST(d.Total AS decimal(30,14)) / er.rate
                   WHEN cr.rate IS NOT NULL THEN CAST(d.Total AS decimal(30,14)) * cr.rate
                   WHEN ir.rate IS NOT NULL THEN CAST(d.Total AS decimal(30,14)) / ir.rate
                   ELSE CAST(d.Total AS decimal(30,14))
               END AS money) AS eur_value,
               DATEDIFF(day, d.Created, :asof) AS overdue_days,
               DATEDIFF(day, d.Created, :asof) - ISNULL(a.NumberDaysDebt, 0) AS days_past_terms
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
        JOIN dbo.Client c ON c.ID = cid.ClientID AND c.Deleted = 0
        LEFT JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        CROSS JOIN eur
        OUTER APPLY (
            SELECT TOP(1) IIF(erh.Amount IS NOT NULL, erh.Amount, er0.Amount) AS rate
            FROM dbo.ExchangeRate er0
            LEFT JOIN dbo.ExchangeRateHistory erh
                ON erh.ExchangeRateID = er0.ID AND erh.Created <= d.Created
            WHERE er0.CurrencyID = ISNULL(a.CurrencyID, 2) AND er0.Code = 'EUR' AND er0.Deleted = 0
            ORDER BY erh.ID DESC
        ) er
        OUTER APPLY (
            SELECT TOP(1) IIF(crh.Amount IS NOT NULL, crh.Amount, cr0.Amount) AS rate
            FROM dbo.CrossExchangeRate cr0
            LEFT JOIN dbo.CrossExchangeRateHistory crh
                ON crh.CrossExchangeRateID = cr0.ID AND crh.Created <= d.Created
            WHERE cr0.CurrencyFromID = ISNULL(a.CurrencyID, 2) AND cr0.CurrencyToID = eur.eur_id
                  AND cr0.Deleted = 0
            ORDER BY crh.ID DESC
        ) cr
        OUTER APPLY (
            SELECT TOP(1) IIF(crh.Amount IS NOT NULL, crh.Amount, cr0.Amount) AS rate
            FROM dbo.CrossExchangeRate cr0
            LEFT JOIN dbo.CrossExchangeRateHistory crh
                ON crh.CrossExchangeRateID = cr0.ID AND crh.Created <= d.Created
            WHERE cr0.CurrencyFromID = eur.eur_id AND cr0.CurrencyToID = ISNULL(a.CurrencyID, 2)
                  AND cr0.Deleted = 0
            ORDER BY crh.ID DESC
        ) ir
        WHERE cid.Deleted = 0
              {manager_filter}
              AND d.Total > 0
              AND d.Created >= :start AND d.Created < :asof
              AND DATEDIFF(day, d.Created, :asof) > ISNULL(a.NumberDaysDebt, 0)
              AND DATEDIFF(day, d.Created, :asof) <= :maxage
    )
"""


def overdue_debts_for_manager(manager_id: int, as_of: str, max_age_days: int = 365,
                              min_amount: float = 0.0) -> list[dict]:
    """Per client: overdue debt in EUR, summed. Overdue age = DATEDIFF(Debt.Created, as_of).

    NB: Debt.Days (the prod-computed overdue column) is NOT maintained on ConcordDb_V5 here
    (always 0), so overdue is derived from Debt.Created age vs Agreement.NumberDaysDebt terms.
    Debt has NO CurrencyID — Debt.Total is in the AGREEMENT currency, so each line is converted to
    EUR via _EUR_VALUE_CTE, a set-based, per-row reproduction of dbo.GetExchangedToEuroValue(Total,
    ISNULL(Agreement.CurrencyID, 2=EUR), Debt.Created) that is identical to the UDF to the cent.
    Without this, UAH debts (CurrencyID 10038) were summed as if EUR (~50× inflated), saturating
    every client's urgency/value to critical. min_amount is therefore an EUR threshold.
    max_age_days drops stale write-off debts (real data has overdue up to ~3800 days, i.e. 2015
    invoices that are not actionable follow-ups). min_amount drops settled/rounding sub-threshold
    overdues (on real data ~25% of debt clients owe < €10 — not worth a collection call).
    """
    window = history.rolling_days(as_of, max_age_days)
    return query(
        _EUR_VALUE_CTE.format(extra_select="", manager_filter="AND c.MainManagerID = :mid")
        + """
        SELECT client_id,
               SUM(eur_value) AS overdue_amount,
               MAX(overdue_days) AS max_overdue_days,
               MAX(days_past_terms) AS max_days_past_terms,
               COUNT(*) AS debt_lines
        FROM rws
        GROUP BY client_id
        HAVING SUM(eur_value) >= :minamt
        """,
        {
            "mid": manager_id,
            "asof": window.as_of.isoformat(),
            "start": window.effective_start.isoformat(),
            "maxage": max_age_days,
            "minamt": min_amount,
        },
    )


def overdue_debts_all_managers(as_of: str, max_age_days: int = 365,
                               min_amount: float = 0.0) -> list[dict]:
    """overdue_debts_for_manager for EVERY manager in a single set-based pass (per-client rows tagged
    with c.MainManagerID). Identical per-client EUR math, filters and HAVING; only the manager
    scoping moves into the GROUP BY so the head/team dashboard needs one query, not one per manager."""
    window = history.rolling_days(as_of, max_age_days)
    return query(
        _EUR_VALUE_CTE.format(extra_select="c.MainManagerID AS manager_id, ",
                              manager_filter="AND c.MainManagerID IS NOT NULL")
        + """
        SELECT manager_id, client_id,
               SUM(eur_value) AS overdue_amount,
               MAX(overdue_days) AS max_overdue_days,
               MAX(days_past_terms) AS max_days_past_terms,
               COUNT(*) AS debt_lines
        FROM rws
        GROUP BY manager_id, client_id
        HAVING SUM(eur_value) >= :minamt
        """,
        {
            "asof": window.as_of.isoformat(),
            "start": window.effective_start.isoformat(),
            "maxage": max_age_days,
            "minamt": min_amount,
        },
    )


def _debt_dashboard_from_rows(rows: list[dict]) -> dict:
    """Fold overdue_debts_for_manager rows into the chart-ready debt dashboard DTO (value_at_risk +
    aging buckets). Shared by the single-manager and all-managers paths so both produce identical
    numbers from identical per-client rows."""
    buckets = [("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None)]
    aging = {label: {"amount_eur": Decimal("0"), "count": 0} for label, _, _ in buckets}
    for r in rows:
        amount = decimal_value(r["overdue_amount"])
        days = int(r["max_overdue_days"] or 0)
        for label, lo, hi in buckets:
            if days >= lo and (hi is None or days <= hi):
                aging[label]["amount_eur"] += amount
                aging[label]["count"] += 1
                break
    # The headline is canonically the sum of the bucket cents the API actually displays.
    # Independently rounding the raw grand total and four buckets can otherwise differ by €0.01.
    rounded_amounts = {
        label: cents_decimal(aging[label]["amount_eur"])
        for label, _, _ in buckets
    }
    value_at_risk = sum(rounded_amounts.values(), Decimal("0"))
    return {
        "value_at_risk_eur": cents(value_at_risk),
        "debt_aging": [
            {
                "bucket": label,
                "amount_eur": cents(rounded_amounts[label]),
                "count": aging[label]["count"],
            }
            for label, _, _ in buckets
        ],
    }


def debt_dashboards_for_all_managers(as_of: str) -> dict[int, dict]:
    """{manager_id: debt dashboard DTO} for every manager, from ONE overdue_debts_all_managers query.
    Each manager's DTO equals debt_dashboard_for_manager(manager_id, as_of) exactly (same rows, same
    fold); managers with no qualifying overdue debt are simply absent (callers default them to 0)."""
    s = get_settings()
    rows = overdue_debts_all_managers(as_of, max_age_days=s.debt_max_age_days,
                                      min_amount=s.debt_min_amount)
    by_manager: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_manager[int(r["manager_id"])].append(r)
    return {mid: _debt_dashboard_from_rows(rs) for mid, rs in by_manager.items()}


def debt_dashboard_for_manager(manager_id: int, as_of: str) -> dict:
    """Chart-ready debt aggregation for a manager dashboard, derived from the SAME EUR-correct
    overdue_debts_for_manager aggregation the debt_followup generator uses (Debt.Total converted to
    EUR via dbo.GetExchangedToEuroValue — never raw PricePerItem). Uses the configured
    debt_max_age_days / debt_min_amount so the dashboard total matches what actually generates tasks.

    Returns:
      value_at_risk_eur: SUM of every open overdue debt (EUR) for this manager;
      debt_aging: [{bucket, amount_eur, count}] over the client's max overdue age (days since
                  Debt.Created), bucketed 0-30 / 31-60 / 61-90 / 90+.
    """
    s = get_settings()
    rows = overdue_debts_for_manager(manager_id, as_of, max_age_days=s.debt_max_age_days,
                                     min_amount=s.debt_min_amount)
    return _debt_dashboard_from_rows(rows)


# --- reorder_due signal ---

def reorder_candidates_for_manager(manager_id: int, as_of: str, min_purchases: int = 3,
                                   min_cycle_days: int = 7, max_overdue_mult: float = 3.0) -> list[dict]:
    """Per client×product: purchase cycle vs elapsed days. Flags products 'due to reorder'.

    cycle = avg gap between orders of that product (span / (n-1)), floored at min_cycle_days;
    elapsed = days since last buy. Returns rows where cycle <= elapsed <= max_overdue_mult*cycle.
    The min floor suppresses burst-buyers (raw cycle ~1-2d) that would flag as perpetually
    'critical' (~11% of pairs on real data). The max ceiling drops abandoned products: on real
    data the mean overdue ratio is ~11x, i.e. products a client stopped buying long ago — those
    are churn, not a reorder nudge, so they're excluded here (and the urgency band stays meaningful).
    """
    window = history.factual_window(as_of)
    return query(
        """
        WITH per_product AS (
            SELECT ca.ClientID AS client_id, oi.ProductID AS product_id,
                   COUNT(DISTINCT o.ID) AS n_orders,
                   MIN(o.Created) AS first_buy,
                   MAX(o.Created) AS last_buy
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            JOIN dbo.Client c ON c.ID = ca.ClientID
            WHERE oi.IsValidForCurrentSale = 1
                  AND o.Created >= :source_start AND o.Created < :asof
                  AND oi.ProductID IS NOT NULL
                  AND c.Deleted = 0 AND c.MainManagerID = :mid
            GROUP BY ca.ClientID, oi.ProductID
            HAVING COUNT(DISTINCT o.ID) >= :minp
        ),
        cyc AS (
            SELECT client_id, product_id, n_orders,
                   DATEDIFF(day, last_buy, :asof) AS elapsed_days,
                   CASE WHEN DATEDIFF(day, first_buy, last_buy) * 1.0 / NULLIF(n_orders - 1, 0) < :mincyc
                        THEN :mincyc * 1.0
                        ELSE DATEDIFF(day, first_buy, last_buy) * 1.0 / NULLIF(n_orders - 1, 0)
                   END AS cycle_days
            FROM per_product
            WHERE DATEDIFF(day, first_buy, last_buy) > 0
        )
        SELECT client_id, product_id, n_orders, cycle_days, elapsed_days
        FROM cyc
        WHERE elapsed_days >= cycle_days
              AND elapsed_days <= cycle_days * :maxmult
        """,
        {
            "mid": manager_id,
            "asof": window.as_of.isoformat(),
            "source_start": window.effective_start.isoformat(),
            "minp": min_purchases,
            "mincyc": min_cycle_days,
            "maxmult": max_overdue_mult,
        },
    )


def product_names(product_ids: list[int]) -> dict[int, str]:
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"SELECT ID AS pid, Name AS name FROM dbo.Product WHERE ID IN {ph}",
        params,
    )
    return {int(r["pid"]): r["name"] for r in rows}


# --- churn_winback signal ---

def churn_candidates_for_manager(manager_id: int, as_of: str,
                                 recent_days: int = 90, baseline_days: int = 365) -> list[dict]:
    """Per client: compare the recent-window order RATE vs the prior baseline RATE.

    A client is a churn candidate if they were active in the baseline period but their recent
    order rate fell below half their baseline rate. Rates are window-length-normalized (recent =
    last :recent days; baseline = the :base..:recent days before that) so a steady buyer is NOT
    flagged just because the recent window is shorter than the baseline window.
    """
    baseline = history.rolling_days(as_of, baseline_days)
    recent = history.rolling_days(as_of, recent_days)
    recent_span_days = (recent.as_of - recent.effective_start).days
    prior_span_days = (recent.effective_start - baseline.effective_start).days
    if recent_span_days <= 0 or prior_span_days <= 0:
        return []
    return query(
        """
        WITH client_orders AS (
            SELECT DISTINCT ca.ClientID AS client_id, o.ID AS order_id, o.Created AS dt
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            JOIN dbo.Client c ON c.ID = ca.ClientID
            WHERE o.Created >= :start AND o.Created < :asof
                  AND c.Deleted = 0 AND c.MainManagerID = :mid
        ),
        agg AS (
            SELECT client_id,
                   SUM(CASE WHEN dt >= :recent_start THEN 1 ELSE 0 END) AS recent_orders,
                   SUM(CASE WHEN dt >= :start
                            AND dt < :recent_start THEN 1 ELSE 0 END) AS prior_orders,
                   MAX(dt) AS last_order
            FROM client_orders
            GROUP BY client_id
        )
        SELECT client_id, recent_orders, prior_orders,
               DATEDIFF(day, last_order, :asof) AS silence_days
        FROM agg
        WHERE prior_orders >= 2
              AND (recent_orders * 1.0 / :recent_span)
                  < 0.5 * (prior_orders * 1.0 / :prior_span)
        """,
        {
            "mid": manager_id,
            "asof": baseline.as_of.isoformat(),
            "start": baseline.effective_start.isoformat(),
            "recent_start": recent.effective_start.isoformat(),
            "recent_span": recent_span_days,
            "prior_span": prior_span_days,
        },
    )


def client_monetary(client_ids: list[int], as_of: str, window_days: int = 365) -> dict[int, float]:
    """Recent revenue per client (for the 'value' term in scoring). Best-effort via OrderItem totals."""
    if not client_ids:
        return {}
    window = history.rolling_days(as_of, window_days)
    ph, params = in_clause("c", client_ids)
    excl = _excluded(window.as_of.isoformat())
    not_in = ""
    if excl:
        eph, eparams = in_clause("x", sorted(excl))
        not_in = f" AND oi.ProductID NOT IN {eph}"
        params = {**params, **eparams}
    rows = query(
        f"""
        SELECT ca.ClientID AS client_id, SUM(oi.Qty * oi.PricePerItem) AS monetary
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created >= :start AND o.Created < :asof
              AND oi.ProductID IS NOT NULL AND ca.ClientID IN {ph}{not_in}
        GROUP BY ca.ClientID
        """,
        {
            "asof": window.as_of.isoformat(),
            "start": window.effective_start.isoformat(),
            **params,
        },
    )
    return {int(r["client_id"]): float(r["monetary"] or 0) for r in rows}


def client_features(client_ids: list[int], as_of: str, window_days: int = 365) -> dict[int, dict]:
    """Per client as-of T: the SHARED model features (trailing-365d EUR turnover, days-since-last-order
    recency, trailing-365d distinct order_count). Identical query/window/exclusions to
    app.ml.dataset.client_features so the LIVE feature row matches the training distribution exactly
    — the propensity model was trained on these three shared features, so a generator must supply all
    three (not just monetary) or the score silently degrades. recency_days is None for clients with no
    order in the window (callers map None -> 9999, the dataset's missing-recency sentinel)."""
    if not client_ids:
        return {}
    window = history.rolling_days(as_of, window_days)
    out: dict[int, dict] = {cid: {"monetary": 0.0, "recency_days": None, "order_count": 0}
                            for cid in client_ids}
    ph, params = in_clause("c", client_ids)
    excl = _excluded(window.as_of.isoformat())
    not_in = ""
    if excl:
        eph, eparams = in_clause("x", sorted(excl))
        not_in = f" AND oi.ProductID NOT IN {eph}"
        params = {**params, **eparams}
    rows = query(
        f"""
        SELECT ca.ClientID AS client_id,
               SUM(oi.Qty * oi.PricePerItem) AS monetary,
               COUNT(DISTINCT o.ID) AS order_count,
               DATEDIFF(day, MAX(o.Created), :asof) AS recency_days
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created >= :start AND o.Created < :asof
              AND oi.ProductID IS NOT NULL AND ca.ClientID IN {ph}{not_in}
        GROUP BY ca.ClientID
        """,
        {
            "asof": window.as_of.isoformat(),
            "start": window.effective_start.isoformat(),
            **params,
        },
    )
    for r in rows:
        cid = int(r["client_id"])
        out[cid] = {
            "monetary": float(r["monetary"] or 0.0),
            "order_count": int(r["order_count"] or 0),
            "recency_days": int(r["recency_days"]) if r["recency_days"] is not None else None,
        }
    return out


# --- sales-target engine: monthly shipped & paid per manager ---

def monthly_shipped(manager_id: int, since: str, as_of: str) -> dict[str, Decimal]:
    """Per-month SHIPPED revenue (EUR) for a manager: SUM(OrderItem.Qty*PricePerItem) by Order month,
    synthetic lines excluded. since/as_of are 'YYYY-MM-DD'. Returns {'YYYY-MM': amount}."""
    window = history.explicit_window(since, as_of)
    excl = _excluded(window.as_of.isoformat())
    params = {
        "mid": manager_id,
        "since": window.effective_start.isoformat(),
        "asof": window.as_of.isoformat(),
    }
    not_in = ""
    if excl:
        eph, eparams = in_clause("x", sorted(excl))
        not_in = f" AND oi.ProductID NOT IN {eph}"
        params = {**params, **eparams}
    rows = query(
        f"""
        SELECT FORMAT(o.Created, 'yyyy-MM') AS ym, SUM(oi.Qty * oi.PricePerItem) AS amt
        FROM dbo.[Order] o
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created >= :since AND o.Created < :asof
              AND oi.ProductID IS NOT NULL{not_in}
        GROUP BY FORMAT(o.Created, 'yyyy-MM')
        """,
        params,
    )
    return {r["ym"]: decimal_value(r["amt"]) for r in rows}


def monthly_paid(manager_id: int, since: str, as_of: str) -> dict[str, Decimal]:
    """Per-month PAID cash (EUR) for a manager, by FromDate month, via ClientID->manager.

    NB: use FromDate (actual payment date), NOT Created (which is the bulk-sync insert date — all
    history was loaded in one batch). EuroAmount is NOT reliably EUR on this data (UAH payments
    have EuroAmount ≈ the local amount, ~16-23× too high), so convert the local Amount to EUR with
    dbo.GetExchangedToEuroValue(Amount, CurrencyID, FromDate) — IncomePaymentOrder carries both.
    """
    window = history.explicit_window(since, as_of)
    rows = query(
        """
        SELECT FORMAT(p.FromDate, 'yyyy-MM') AS ym,
               SUM(dbo.GetExchangedToEuroValue(p.Amount, p.CurrencyID, p.FromDate)) AS amt
        FROM dbo.IncomePaymentOrder p
        JOIN dbo.Client c ON c.ID = p.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
        WHERE p.Deleted = 0 AND p.FromDate >= :since AND p.FromDate < :asof
        GROUP BY FORMAT(p.FromDate, 'yyyy-MM')
        """,
        {
            "mid": manager_id,
            "since": window.effective_start.isoformat(),
            "asof": window.as_of.isoformat(),
        },
    )
    return {r["ym"]: decimal_value(r["amt"]) for r in rows}
