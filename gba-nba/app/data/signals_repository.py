"""Read-only signal queries over ConcordDb_V5 for task generation. All parameterized.

Verified columns:
  Client(ID, MainManagerID, FullName, Name, MobileNumber, EmailAddress, Created, Deleted)
  Debt(ID, Created, Days, Total, Deleted)  -- Days (prod-computed overdue) is ALWAYS 0 here; use Created
  ClientInDebt(ID, AgreementID, ClientID, DebtID, Deleted, SaleID, ReSaleID)
  ClientAgreement(ID, ClientID, AgreementID)
  Agreement(ID, NumberDaysDebt, AmountDebt, CurrencyID)
"""
from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.data.db import in_clause, query


@lru_cache(maxsize=4)
def ubiquitous_product_ids(pct: float) -> frozenset[int]:
    """Products bought by more than `pct` of distinct clients (last 12mo) — synthetic accounting
    lines / universal staples (e.g. "Ввід боргів"/debt-entry, ~75% of clients) that aren't real
    sellable products. Excluded from reorder/monetary signals so they don't generate nonsensical
    'reorder the debt-entry' tasks or inflate a client's turnover. Cached per pct."""
    rows = query(
        """
        WITH base AS (
            SELECT ca.ClientID AS cid, oi.ProductID AS pid
            FROM dbo.[Order] o
            JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE o.Deleted = 0 AND oi.ProductID IS NOT NULL
                  AND o.Created >= DATEADD(month, -12, GETDATE())
        ),
        tot AS (SELECT COUNT(DISTINCT cid) AS n FROM base)
        SELECT b.pid AS pid
        FROM base b CROSS JOIN tot
        GROUP BY b.pid, tot.n
        HAVING COUNT(DISTINCT b.cid) * 1.0 / NULLIF(tot.n, 0) > :pct
        """,
        {"pct": pct},
    )
    return frozenset(int(r["pid"]) for r in rows)


def _excluded() -> frozenset[int]:
    return ubiquitous_product_ids(get_settings().ubiquity_exclude_pct)


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
    """Recently-created clients of a manager with <= max_orders purchases — activation candidates."""
    return query(
        """
        WITH cand AS (
            SELECT c.ID, c.FullName, c.Name, c.MobileNumber, c.EmailAddress, c.Created
            FROM dbo.Client c
            WHERE c.Deleted = 0 AND c.MainManagerID = :mid
                  AND c.Created >= DATEADD(day, -:recent, :asof)
        ),
        oc AS (
            SELECT ca.ClientID AS cid, COUNT(DISTINCT o.ID) AS n
            FROM dbo.ClientAgreement ca
            JOIN cand ON cand.ID = ca.ClientID
            LEFT JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0 AND o.Created < :asof
            GROUP BY ca.ClientID
        )
        SELECT cand.ID AS client_id, cand.FullName AS full_name, cand.Name AS name,
               cand.MobileNumber AS phone, cand.EmailAddress AS email,
               DATEDIFF(day, cand.Created, :asof) AS days_since_created,
               ISNULL(oc.n, 0) AS n_orders
        FROM cand LEFT JOIN oc ON oc.cid = cand.ID
        WHERE ISNULL(oc.n, 0) <= :maxord
        """,
        {"mid": manager_id, "asof": as_of, "recent": recent_days, "maxord": max_orders},
    )


def all_managers() -> list[int]:
    """Distinct managers that have at least one client (Client.MainManagerID)."""
    rows = query(
        """
        SELECT DISTINCT MainManagerID AS mid
        FROM dbo.Client WHERE Deleted = 0 AND MainManagerID IS NOT NULL
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
    return query(
        """
        WITH act AS (
            SELECT ca.ClientID AS cid
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID AND o.Deleted = 0
                 AND o.Created >= DATEADD(day, -:recent, :asof) AND o.Created < :asof
            JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
            GROUP BY ca.ClientID
            HAVING COUNT(DISTINCT o.ID) >= :minord
        )
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email
        FROM act JOIN dbo.Client c ON c.ID = act.cid
        """,
        {"mid": manager_id, "asof": as_of, "recent": recent_days, "minord": min_orders},
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

def overdue_debts_for_manager(manager_id: int, as_of: str, max_age_days: int = 365,
                              min_amount: float = 0.0) -> list[dict]:
    """Per client: overdue debt in EUR, summed. Overdue age = DATEDIFF(Debt.Created, as_of).

    NB: Debt.Days (the prod-computed overdue column) is NOT maintained on ConcordDb_V5 here
    (always 0), so overdue is derived from Debt.Created age vs Agreement.NumberDaysDebt terms.
    Debt has NO CurrencyID — Debt.Total is in the AGREEMENT currency, so each line is converted to
    EUR via dbo.GetExchangedToEuroValue(Total, ISNULL(Agreement.CurrencyID, 2=EUR), Debt.Created).
    Without this, UAH debts (CurrencyID 10038) were summed as if EUR (~50× inflated), saturating
    every client's urgency/value to critical. min_amount is therefore an EUR threshold.
    max_age_days drops stale write-off debts (real data has overdue up to ~3800 days, i.e. 2015
    invoices that are not actionable follow-ups). min_amount drops settled/rounding sub-threshold
    overdues (on real data ~25% of debt clients owe < €10 — not worth a collection call).
    """
    return query(
        """
        SELECT c.ID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(d.Total, ISNULL(a.CurrencyID, 2), d.Created))
                   AS overdue_amount,
               MAX(DATEDIFF(day, d.Created, :asof)) AS max_overdue_days,
               MAX(DATEDIFF(day, d.Created, :asof) - ISNULL(a.NumberDaysDebt, 0)) AS max_days_past_terms,
               COUNT(*) AS debt_lines
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
        JOIN dbo.Client c ON c.ID = cid.ClientID AND c.Deleted = 0
        LEFT JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0
              AND c.MainManagerID = :mid
              AND d.Total > 0
              AND DATEDIFF(day, d.Created, :asof) > ISNULL(a.NumberDaysDebt, 0)
              AND DATEDIFF(day, d.Created, :asof) <= :maxage
        GROUP BY c.ID
        HAVING SUM(dbo.GetExchangedToEuroValue(d.Total, ISNULL(a.CurrencyID, 2), d.Created)) >= :minamt
        """,
        {"mid": manager_id, "asof": as_of, "maxage": max_age_days, "minamt": min_amount},
    )


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
    buckets = [("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None)]
    aging = {label: {"amount_eur": 0.0, "count": 0} for label, _, _ in buckets}
    value_at_risk = 0.0
    for r in rows:
        amount = float(r["overdue_amount"] or 0.0)
        days = int(r["max_overdue_days"] or 0)
        value_at_risk += amount
        for label, lo, hi in buckets:
            if days >= lo and (hi is None or days <= hi):
                aging[label]["amount_eur"] += amount
                aging[label]["count"] += 1
                break
    return {
        "value_at_risk_eur": round(value_at_risk, 2),
        "debt_aging": [{"bucket": label, "amount_eur": round(aging[label]["amount_eur"], 2),
                        "count": aging[label]["count"]} for label, _, _ in buckets],
    }


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
            WHERE o.Deleted = 0 AND o.Created < :asof AND oi.ProductID IS NOT NULL
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
        {"mid": manager_id, "asof": as_of, "minp": min_purchases, "mincyc": min_cycle_days,
         "maxmult": max_overdue_mult},
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
    return query(
        """
        WITH client_orders AS (
            SELECT ca.ClientID AS client_id, o.ID AS order_id, o.Created AS dt
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.Client c ON c.ID = ca.ClientID
            WHERE o.Deleted = 0 AND o.Created < :asof
                  AND c.Deleted = 0 AND c.MainManagerID = :mid
        ),
        agg AS (
            SELECT client_id,
                   SUM(CASE WHEN dt >= DATEADD(day, -:recent, :asof) THEN 1 ELSE 0 END) AS recent_orders,
                   SUM(CASE WHEN dt >= DATEADD(day, -:base, :asof)
                            AND dt < DATEADD(day, -:recent, :asof) THEN 1 ELSE 0 END) AS prior_orders,
                   MAX(dt) AS last_order
            FROM client_orders
            GROUP BY client_id
        )
        SELECT client_id, recent_orders, prior_orders,
               DATEDIFF(day, last_order, :asof) AS silence_days
        FROM agg
        WHERE prior_orders >= 2
              AND (recent_orders * 1.0 / :recent) < 0.5 * (prior_orders * 1.0 / (:base - :recent))
        """,
        {"mid": manager_id, "asof": as_of, "recent": recent_days, "base": baseline_days},
    )


def client_monetary(client_ids: list[int], as_of: str, window_days: int = 365) -> dict[int, float]:
    """Recent revenue per client (for the 'value' term in scoring). Best-effort via OrderItem totals."""
    if not client_ids:
        return {}
    ph, params = in_clause("c", client_ids)
    excl = _excluded()
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
        WHERE o.Deleted = 0 AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof
              AND oi.ProductID IS NOT NULL AND ca.ClientID IN {ph}{not_in}
        GROUP BY ca.ClientID
        """,
        {"asof": as_of, "win": window_days, **params},
    )
    return {int(r["client_id"]): float(r["monetary"] or 0) for r in rows}


# --- sales-target engine: monthly shipped & paid per manager ---

def monthly_shipped(manager_id: int, since: str, as_of: str) -> dict[str, float]:
    """Per-month SHIPPED revenue (EUR) for a manager: SUM(OrderItem.Qty*PricePerItem) by Order month,
    synthetic lines excluded. since/as_of are 'YYYY-MM-DD'. Returns {'YYYY-MM': amount}."""
    excl = _excluded()
    params = {"mid": manager_id, "since": since, "asof": as_of}
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
        WHERE o.Deleted = 0 AND o.Created >= :since AND o.Created < :asof
              AND oi.ProductID IS NOT NULL{not_in}
        GROUP BY FORMAT(o.Created, 'yyyy-MM')
        """,
        params,
    )
    return {r["ym"]: float(r["amt"] or 0) for r in rows}


def monthly_paid(manager_id: int, since: str, as_of: str) -> dict[str, float]:
    """Per-month PAID cash (EUR) for a manager, by FromDate month, via ClientID->manager.

    NB: use FromDate (actual payment date), NOT Created (which is the bulk-sync insert date — all
    history was loaded in one batch). EuroAmount is NOT reliably EUR on this data (UAH payments
    have EuroAmount ≈ the local amount, ~16-23× too high), so convert the local Amount to EUR with
    dbo.GetExchangedToEuroValue(Amount, CurrencyID, FromDate) — IncomePaymentOrder carries both.
    """
    rows = query(
        """
        SELECT FORMAT(p.FromDate, 'yyyy-MM') AS ym,
               SUM(dbo.GetExchangedToEuroValue(p.Amount, p.CurrencyID, p.FromDate)) AS amt
        FROM dbo.IncomePaymentOrder p
        JOIN dbo.Client c ON c.ID = p.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
        WHERE p.Deleted = 0 AND p.FromDate >= :since AND p.FromDate < :asof
        GROUP BY FORMAT(p.FromDate, 'yyyy-MM')
        """,
        {"mid": manager_id, "since": since, "asof": as_of},
    )
    return {r["ym"]: float(r["amt"] or 0) for r in rows}
