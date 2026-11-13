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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from app.core import history
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.money import decimal_value
from app.data import debt_snapshot
from app.data.db import in_clause, query
from app.data.purchase_history import retained_purchase_cte

log = get_logger("signals")
_KYIV = ZoneInfo("Europe/Kyiv")


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
    """Clients with few retained purchases; never infer acquisition date from Client.Created."""
    created_window = history.rolling_days(as_of, recent_days)
    purchase_window = history.factual_window(as_of)
    # Client.Created is a sync timestamp, not evidence of a genuinely new customer.
    return query(retained_purchase_cte() + """
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email,
               NULL AS days_since_created, COUNT(DISTINCT p.order_id) AS n_orders
        FROM dbo.Client c LEFT JOIN retained_purchases p ON p.client_id = c.ID
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
        GROUP BY c.ID, c.FullName, c.Name, c.MobileNumber, c.EmailAddress
        HAVING COUNT(DISTINCT p.order_id) <= :maxord
        """, {"mid": manager_id, "asof": created_window.as_of.isoformat(),
              "created_start": created_window.effective_start.isoformat(),
              "source_start": purchase_window.effective_start.isoformat(), "maxord": max_orders})


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


def task_manager_ids(active_assignee_ids: list[int]) -> list[int]:
    """Task-only team: current book owners, active assignees and active sales analysts.

    Deliberately separate from all_managers(), whose financial scope stays current client book.
    """
    assignees = sorted({int(value) for value in active_assignee_ids if int(value) > 0})
    predicate, params = "", {}
    if assignees:
        placeholders, params = in_clause("assignee", assignees)
        predicate = f" OR u.ID IN {placeholders}"
    rows = query(f"""
        SELECT u.ID AS id FROM dbo.[User] u
        LEFT JOIN dbo.UserRole role ON role.ID=u.UserRoleID
        WHERE u.Deleted=0 AND (role.UserRoleType=0
          OR EXISTS (SELECT 1 FROM dbo.Client c WHERE c.MainManagerID=u.ID AND c.Deleted=0)
          {predicate})
        ORDER BY u.ID
    """, params)
    return [int(row["id"]) for row in rows]


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


def contact_book_for_manager(manager_id: int, client_id: int | None = None) -> list[dict]:
    """Current ownership/identity only: no order/debt aggregation on contact actions."""
    return query(
        """
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               LOWER(CONVERT(varchar(36), c.NetUID)) AS client_net_uid
        FROM dbo.Client c
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
        """ + (" AND c.ID = :cid" if client_id is not None else ""),
        {"mid": manager_id, "cid": client_id},
    )


TURNOVER_BASIS = "gross_invoiced_returns_not_subtracted"


def clients_overview_for_manager(manager_id: int, as_of: str, window_days: int = 365) -> list[dict]:
    """Owned client book; turnover and last purchase share the retained-invoice grain.

    turnover_eur («Оборот (12м)») is GROSS: confirmed invoices by invoice date, return
    documents not subtracted. The purchase rule nets OrderItem.ReturnedQty, which the source
    fills for a few euros only (45.96 EUR on 2026-11-13), so the figure equals the gross
    invoiced amount; returns of the same period are 1.0-1.7 % of it. TURNOVER_BASIS names
    this on the API.
    """
    window = history.rolling_days(as_of, window_days)
    params = {"mid": manager_id, "asof": window.as_of.isoformat(),
              "source_start": history.source_history_start().isoformat(),
              "start": window.effective_start.isoformat()}
    excluded = _excluded(as_of)
    predicate = ""
    if excluded:
        placeholders, extra = in_clause("x", sorted(excluded))
        predicate = f" WHERE product_id NOT IN {placeholders}"
        params.update(extra)
    return query(retained_purchase_cte() + f"""
        , commercial AS (SELECT * FROM retained_purchases {predicate}),
        stats AS (
            SELECT client_id, MAX(purchased_at) AS last_order,
                   COUNT(DISTINCT order_id) AS retained_orders_cnt,
                   COUNT(DISTINCT CASE WHEN purchased_at >= :start THEN order_id END) AS orders_cnt,
                   SUM(CASE WHEN purchased_at >= :start THEN turnover_eur ELSE 0 END) AS turnover_eur
            FROM commercial GROUP BY client_id
        )
        SELECT c.ID AS client_id, LOWER(CONVERT(varchar(36), c.NetUID)) AS client_net_uid,
               c.Name AS name, c.FullName AS full_name, c.MobileNumber AS phone, c.EmailAddress AS email,
               s.last_order, COALESCE(s.orders_cnt, 0) AS orders_cnt,
               COALESCE(s.retained_orders_cnt, 0) AS retained_orders_cnt,
               COALESCE(s.turnover_eur, 0) AS turnover_eur
        FROM dbo.Client c LEFT JOIN stats s ON s.client_id = c.ID
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
        ORDER BY COALESCE(s.turnover_eur, 0) DESC, c.Name, c.ID
    """, params)


_BOOK_SCOPE = "c.MainManagerID IS NOT NULL"
_MANAGER_SCOPE = "c.MainManagerID = :mid"


def _book_scope(manager_id: int | None) -> tuple[str, dict]:
    """One manager's clients, or with None the whole managed book the generators serve.

    The retrain dataset enumerates its candidates through the same readers with None, so a
    training row and a served task come from one rule over one population.
    """
    if manager_id is None:
        return _BOOK_SCOPE, {}
    return _MANAGER_SCOPE, {"mid": manager_id}


def active_clients_for_manager(manager_id: int | None, as_of: str, recent_days: int = 120,
                               min_orders: int = 3) -> list[dict]:
    """Clients with >= min_orders distinct orders in the last recent_days — the only clients worth
    a cross-sell reco call (reco needs purchase history; cold clients return no discovery). On real
    data this is ~20% of a manager's book, so it cuts reco HTTP calls 4-5x vs. all clients."""
    window = history.rolling_days(as_of, recent_days)
    scope, scope_params = _book_scope(manager_id)
    return query(
        retained_purchase_cte(scope_sql=scope, start_param="start") + """
        , act AS (
            SELECT client_id AS cid FROM retained_purchases
            GROUP BY client_id HAVING COUNT(DISTINCT order_id) >= :minord
        )
        SELECT c.ID AS client_id, c.FullName AS full_name, c.Name AS name,
               c.MobileNumber AS phone, c.EmailAddress AS email,
               c.MainManagerID AS manager_id,
               LOWER(CONVERT(varchar(36), c.NetUID)) AS client_net_uid
        FROM act JOIN dbo.Client c ON c.ID = act.cid
        """,
        {
            **scope_params,
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
               c.MobileNumber AS phone, c.EmailAddress AS email,
               LOWER(CONVERT(varchar(36), c.NetUID)) AS client_net_uid
        FROM dbo.Client c WHERE c.Deleted = 0 AND c.ID IN {ph}
        """,
        params,
    )
    return {int(r["client_id"]): r for r in rows}


# --- debt_followup signal ---

def overdue_debts_for_manager(manager_id: int, as_of: str, min_amount: float = 0.0, *,
                              with_unverified: bool = False) -> list[dict]:
    """Per client: overdue debt in EUR under debt_snapshot's rule; min_amount is a task threshold."""
    rows = debt_snapshot.overdue_clients(as_of, manager_id, with_unverified=with_unverified)
    return [row for row in rows
            if row["overdue_amount"] >= decimal_value(min_amount) or not row["debt_lines"]]


def debt_task_candidates(manager_id: int, as_of: str) -> list[dict]:
    """Clients of a manager a debt follow-up task may be raised for (debt_snapshot rule)."""
    s = get_settings()
    return debt_snapshot.task_candidates(
        debt_snapshot.overdue_clients(as_of, manager_id), s.debt_min_amount, s.debt_grace_days
    )


# --- reorder_due signal ---

def repeat_products_for_manager(manager_id: int | None, as_of: str, min_purchases: int = 3,
                                min_cycle_days: int = 7, *, include_day: bool = False,
                                include_book: bool = False) -> list[dict]:
    """Same retained purchase cycle for generation and the client screen, before due filtering.

    Only the selected screen day is inclusive; scheduled generation keeps its exclusive cutoff.
    Evidence is compact (three latest documents) while counts/cycles use the entire source window.
    Stock is current free availability across active non-defective warehouses, not an agreement quote.
    """
    window = history.factual_window(as_of)
    end = window.as_of + timedelta(days=1) if include_day else window.as_of
    recent = history.rolling_days(end, 90)
    baseline = history.rolling_days(end, 365)
    scope, scope_params = _book_scope(manager_id)
    params = {**scope_params, "asof": end.isoformat(), "cycle_asof": as_of,
              "source_start": window.effective_start.isoformat(), "minp": min_purchases,
              "mincyc": min_cycle_days, "maxmult": get_settings().reorder_max_overdue_mult,
              "recent_start": recent.effective_start.isoformat(),
              "baseline_start": baseline.effective_start.isoformat()}
    predicate = "1 = 1"
    if include_book:
        excluded = _excluded(as_of)
        if excluded:
            placeholders, extra = in_clause("x", sorted(excluded))
            predicate = f"product_id NOT IN {placeholders}"
            params.update(extra)
    # Window ranks count each order only once across its product rows, so the entire book,
    # repeat cycles and churn use one scan of the same retained source snapshot.
    sql = retained_purchase_cte(scope_sql=scope, materialize_orders=True) + f"""
        , ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY client_id, product_id ORDER BY purchased_at DESC, order_id DESC) AS rn,
                ROW_NUMBER() OVER (PARTITION BY client_id, order_id ORDER BY product_id) AS order_rn
            FROM retained_purchases
        ), stats AS (
            SELECT client_id, product_id, COUNT(*) AS n_orders,
                   MIN(purchased_at) AS first_buy, MAX(purchased_at) AS last_buy,
                   MAX(CASE WHEN rn = 1 THEN quantity END) AS last_quantity,
                   MAX(CASE WHEN rn = 2 THEN purchased_at END) AS previous_buy,
                   MAX(CASE WHEN rn = 3 THEN purchased_at END) AS third_buy,
                   SUM(CASE WHEN order_rn = 1 THEN 1 ELSE 0 END) AS retained_component,
                   SUM(CASE WHEN order_rn = 1 AND purchased_at >= :recent_start THEN 1 ELSE 0 END)
                       AS recent_component,
                   SUM(CASE WHEN order_rn = 1 AND purchased_at >= :baseline_start
                                 AND purchased_at < :recent_start THEN 1 ELSE 0 END) AS prior_component,
                   SUM(CASE WHEN purchased_at >= :baseline_start AND {predicate}
                            THEN turnover_eur ELSE 0 END) AS turnover_component
            FROM ranked GROUP BY client_id, product_id
        ), book_stats AS (
            SELECT *, SUM(retained_component) OVER (PARTITION BY client_id) AS retained_orders_cnt,
                   SUM(recent_component) OVER (PARTITION BY client_id) AS recent_orders,
                   SUM(prior_component) OVER (PARTITION BY client_id) AS prior_orders,
                   SUM(turnover_component) OVER (PARTITION BY client_id) AS turnover_eur,
                   MAX(last_buy) OVER (PARTITION BY client_id) AS last_order
            FROM stats
        ), cycles AS (
            SELECT *, DATEDIFF(day, last_buy, :cycle_asof) AS elapsed_days,
                   CASE WHEN n_orders >= :minp AND DATEDIFF(day, first_buy, last_buy) > 0
                        THEN CASE WHEN DATEDIFF(day, first_buy, last_buy) * 1.0 / (n_orders - 1) < :mincyc
                                  THEN :mincyc * 1.0
                                  ELSE DATEDIFF(day, first_buy, last_buy) * 1.0 / (n_orders - 1) END
                        END AS cycle_days
            FROM book_stats
        ), eligible AS (
            SELECT *, CASE WHEN elapsed_days >= cycle_days AND elapsed_days <= cycle_days * :maxmult
                                AND {predicate} THEN 1 ELSE 0 END AS is_due
            FROM cycles
        ), evidence AS (
            SELECT *,
                   SUM(CASE WHEN is_due = 1 AND elapsed_days = CEILING(cycle_days) THEN 1 ELSE 0 END)
                       OVER (PARTITION BY client_id) AS reorder_due_count,
                   SUM(CASE WHEN is_due = 1 AND elapsed_days > CEILING(cycle_days) THEN 1 ELSE 0 END)
                       OVER (PARTITION BY client_id) AS reorder_overdue_count,
                   MAX(CASE WHEN is_due = 1 THEN DATEADD(day,
                       CONVERT(int, CEILING(cycle_days * FLOOR(elapsed_days / cycle_days))),
                       CONVERT(date, last_buy)) END) OVER (PARTITION BY client_id) AS cooldown_cycle_start,
                   ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY is_due DESC,
                       elapsed_days / NULLIF(cycle_days, 0) DESC, product_id ASC) AS evidence_rank
            FROM eligible
        )
        SELECT c.ID AS client_id, LOWER(CONVERT(varchar(36), c.NetUID)) AS client_net_uid,
               c.Name AS name, c.FullName AS full_name, c.MobileNumber AS phone, c.EmailAddress AS email,
               x.product_id, x.n_orders, x.first_buy, x.last_buy, x.last_quantity,
               x.previous_buy, x.third_buy,
               COALESCE(x.retained_orders_cnt, 0) AS retained_orders_cnt,
               COALESCE(x.recent_orders, 0) AS recent_orders, COALESCE(x.prior_orders, 0) AS prior_orders,
               COALESCE(x.recent_orders + x.prior_orders, 0) AS orders_cnt,
               COALESCE(x.turnover_eur, 0) AS turnover_eur, x.last_order,
               x.elapsed_days, x.cycle_days,
               COALESCE(x.reorder_due_count, 0) AS reorder_due_count,
               COALESCE(x.reorder_overdue_count, 0) AS reorder_overdue_count,
               x.cooldown_cycle_start,
               p.Name AS product_name, LOWER(CONVERT(varchar(36), p.NetUID)) AS product_net_uid,
               CASE WHEN p.ID IS NULL THEN NULL ELSE COALESCE(stock.available_qty, 0) END AS available_qty
        FROM dbo.Client c LEFT JOIN evidence x ON x.client_id = c.ID
             {"AND x.evidence_rank <= 5" if include_book else ""}
        LEFT JOIN dbo.Product p ON p.ID = x.product_id AND p.Deleted = 0
        OUTER APPLY (
            SELECT SUM(CASE WHEN pa.Amount > 0 THEN pa.Amount ELSE 0 END) AS available_qty
            FROM dbo.ProductAvailability pa JOIN dbo.Storage st ON st.ID = pa.StorageID
            WHERE pa.ProductID = x.product_id AND pa.Deleted = 0
                  AND st.Deleted = 0 AND st.ForDefective = 0
        ) stock
        WHERE c.Deleted = 0 AND {scope}
    """ + ("" if include_book else " AND x.product_id IS NOT NULL") + " ORDER BY c.ID, x.product_id; DROP TABLE #repeat_purchase_orders;"
    return query(sql, params)


def reorder_candidates_for_manager(manager_id: int | None, as_of: str, min_purchases: int = 3,
                                   min_cycle_days: int = 7, max_overdue_mult: float = 3.0) -> list[dict]:
    """Existing average-gap/minimum-cycle policy, shared verbatim with the client evidence."""
    return [row for row in repeat_products_for_manager(manager_id, as_of, min_purchases, min_cycle_days)
            if row["cycle_days"] is not None
            and float(row["cycle_days"]) <= row["elapsed_days"]
            <= float(row["cycle_days"]) * max_overdue_mult]


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

def churn_candidates_for_manager(manager_id: int | None, as_of: str,
                                 recent_days: int = 90, baseline_days: int = 365) -> list[dict]:
    """Per client: compare the recent-window order RATE vs the prior baseline RATE.

    A client is a churn candidate if they were active in the baseline period but their recent
    order rate fell below half their baseline rate. Rates are window-length-normalized (recent =
    last :recent days; baseline = the :base..:recent days before that) so a steady buyer is NOT
    flagged just because the recent window is shorter than the baseline window.

    The silence must also exceed the client's own rhythm: at least churn_min_silence_days and
    churn_silence_cycle_mult times the client's mean gap between purchase days in the baseline
    window. A client inside their natural cycle is not lapsing.
    """
    settings = get_settings()
    baseline = history.rolling_days(as_of, baseline_days)
    recent = history.rolling_days(as_of, recent_days)
    recent_span_days = (recent.as_of - recent.effective_start).days
    prior_span_days = (recent.effective_start - baseline.effective_start).days
    if recent_span_days <= 0 or prior_span_days <= 0:
        return []
    scope, scope_params = _book_scope(manager_id)
    rows = query(
        retained_purchase_cte(scope_sql=scope, start_param="start") + """
        , client_orders AS (
            SELECT DISTINCT client_id, order_id, purchased_at AS dt FROM retained_purchases
        ),
        agg AS (
            SELECT client_id,
                   SUM(CASE WHEN dt >= :recent_start THEN 1 ELSE 0 END) AS recent_orders,
                   SUM(CASE WHEN dt >= :start
                            AND dt < :recent_start THEN 1 ELSE 0 END) AS prior_orders,
                   MAX(dt) AS last_order,
                   COUNT(DISTINCT CONVERT(date, dt)) AS purchase_days,
                   DATEDIFF(day, MIN(dt), MAX(dt)) AS purchase_span_days
            FROM client_orders
            GROUP BY client_id
        )
        SELECT client_id, recent_orders, prior_orders,
               DATEDIFF(day, last_order, :asof) AS silence_days,
               purchase_days, purchase_span_days
        FROM agg
        WHERE prior_orders >= 2
              AND (recent_orders * 1.0 / :recent_span)
                  < 0.5 * (prior_orders * 1.0 / :prior_span)
        """,
        {
            **scope_params,
            "asof": baseline.as_of.isoformat(),
            "start": baseline.effective_start.isoformat(),
            "recent_start": recent.effective_start.isoformat(),
            "recent_span": recent_span_days,
            "prior_span": prior_span_days,
        },
    )
    out = []
    for row in rows:
        days = int(row.get("purchase_days") or 0)
        interval = (float(row.get("purchase_span_days") or 0) / (days - 1)) if days >= 2 else None
        silence = int(row["silence_days"] or 0)
        if silence < churn_silence_floor(interval, settings):
            continue
        out.append({**row, "own_interval_days": round(interval, 1) if interval is not None else None})
    return out


def churn_silence_floor(own_interval_days: float | None, settings=None) -> float:
    """Days of silence before a rate drop counts as lapsing, given the client's own rhythm."""
    settings = settings or get_settings()
    return max(float(settings.churn_min_silence_days),
               settings.churn_silence_cycle_mult * float(own_interval_days or 0.0))


def client_features(client_ids: list[int], as_of: str, window_days: int = 365) -> dict[int, dict]:
    """Per client as-of T: the shared model features over the retained confirmed purchases.

    monetary is the same number as «Оборот (12м)» in clients_overview_for_manager: invoiced,
    non-deleted, non-merged sales by invoice date — gross, return documents are not
    subtracted (see TURNOVER_BASIS). order_count and
    recency_days count the same purchases, so average order value has one population.
    recency_days is None for clients without a purchase in the window (callers map None -> 9999,
    the dataset's missing-recency sentinel). app.ml.dataset.client_features delegates here.
    """
    if not client_ids:
        return {}
    window = history.rolling_days(as_of, window_days)
    out: dict[int, dict] = {cid: {"monetary": 0.0, "recency_days": None, "order_count": 0}
                            for cid in client_ids}
    ph, params = in_clause("c", client_ids)
    excluded = _excluded(window.as_of.isoformat())
    predicate = ""
    if excluded:
        eph, eparams = in_clause("x", sorted(excluded))
        predicate = f" WHERE product_id NOT IN {eph}"
        params = {**params, **eparams}
    rows = query(
        retained_purchase_cte(scope_sql=f"c.ID IN {ph}", start_param="start") + f"""
        SELECT client_id,
               SUM(turnover_eur) AS monetary,
               COUNT(DISTINCT order_id) AS order_count,
               DATEDIFF(day, MAX(purchased_at), :asof) AS recency_days
        FROM retained_purchases{predicate}
        GROUP BY client_id
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


def client_net_uids() -> set[str]:
    """NetUID of every client row, lower case: the stable half of a persisted client key."""
    return {str(row["net_uid"]) for row in query(
        "SELECT LOWER(CONVERT(varchar(36), NetUID)) AS net_uid FROM dbo.Client")}


def purchases_between(client_ids: list[int], start: str, end: str) -> list[dict]:
    """(client_id, product_id) of the retained confirmed purchases on the Kyiv days
    start <= day < end: the outcome side of the rule the generators read their signals with."""
    out: list[dict] = []
    ids = sorted({int(value) for value in client_ids})
    for index in range(0, len(ids), 800):
        ph, params = in_clause("c", ids[index:index + 800])
        out.extend(query(
            retained_purchase_cte(scope_sql=f"c.ID IN {ph}", start_param="start", end_param="end") + """
            SELECT DISTINCT client_id, product_id FROM retained_purchases
            """,
            {"start": start, "end": end, **params},
        ))
    return out


def payments_between(client_ids: list[int], start: str, end: str) -> dict[int, Decimal]:
    """Cash received per client on the Kyiv register days start <= day < end, in EUR.

    The payment rule of monthly_paid: not deleted, not cancelled, no owned accounting carrier,
    the document's own EuroAmount.
    """
    out: dict[int, Decimal] = {}
    ids = sorted({int(value) for value in client_ids})
    bounds = {"since_utc": _kyiv_midnight_utc(history.require_as_of(start)),
              "until_utc": _kyiv_midnight_utc(history.require_as_of(end))}
    for index in range(0, len(ids), 800):
        ph, params = in_clause("c", ids[index:index + 800])
        rows = query(
            f"""
            SELECT p.ClientID AS client_id, SUM(p.EuroAmount) AS paid
            FROM dbo.IncomePaymentOrder p
            JOIN dbo.Client c ON c.ID = p.ClientID AND c.Deleted = 0
            WHERE p.Deleted = 0 AND p.IsCanceled = 0
                  AND p.FromDate >= :since_utc AND p.FromDate < :until_utc
                  AND p.ClientID IN {ph}
                  AND NOT EXISTS (
                      SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier
                      WHERE carrier.TargetEntityType = 'IncomePaymentOrder'
                            AND carrier.TargetEntityID = p.ID
                            AND carrier.IsCurrent = 1 AND carrier.OwnershipType = 1)
            GROUP BY p.ClientID
            """,
            {**bounds, **params},
        )
        out.update({int(r["client_id"]): decimal_value(r["paid"] or 0) for r in rows})
    return out


# --- sales-target engine: monthly shipped & paid per manager ---

class InvoiceSeries(dict):
    """Monthly verified invoice gross plus the orders left out for an unverified invoice date."""

    unverified_orders: int = 0
    unverified_amount: Decimal = Decimal("0")


def monthly_shipped(manager_id: int, since: str, as_of: str) -> InvoiceSeries:
    """Compatibility name: gross confirmed invoice documents, NOT recognized shipment revenue.

    Current document revisions and current client book; original line quantity (returns have
    their own event, never rewrite the invoice month). Orders without a verified invoice clock
    are excluded from every month and reported on the result, never silently summed.
    """
    window = history.explicit_window(since, as_of)
    excluded = _excluded(window.as_of.isoformat())
    params = {"mid": manager_id, "source_start": window.effective_start.isoformat(),
              "asof": window.as_of.isoformat()}
    predicate = ""
    if excluded:
        placeholders, values = in_clause("x", sorted(excluded))
        params.update(values)
        predicate = f" AND oi.ProductID NOT IN {placeholders}"
    rows = query(retained_purchase_cte() + f"""
        SELECT CASE WHEN p.timestamp_basis IS NULL THEN NULL
                    ELSE CONVERT(char(7),p.purchased_at,126) END AS ym,
               SUM(CAST(oi.Qty AS decimal(30,8))*oi.PricePerItem) AS amt,
               COUNT(DISTINCT p.order_id) AS orders
        FROM purchase_orders p JOIN dbo.OrderItem oi ON oi.OrderID=p.order_id
        WHERE oi.Deleted=0 AND oi.IsValidForCurrentSale=1 AND oi.ProductID IS NOT NULL
              AND oi.Qty>0 {predicate}
        GROUP BY CASE WHEN p.timestamp_basis IS NULL THEN NULL
                      ELSE CONVERT(char(7),p.purchased_at,126) END
    """, params)
    series = InvoiceSeries()
    for row in rows:
        if row["ym"] is None:
            series.unverified_orders = int(row["orders"] or 0)
            series.unverified_amount = decimal_value(row["amt"] or 0)
        else:
            series[row["ym"]] = decimal_value(row["amt"])
    if series.unverified_orders:
        log.warning("invoice_event_date_unverified", manager_id=manager_id,
                    orders=series.unverified_orders)
    return series


def _kyiv_midnight_utc(day) -> str:
    local = datetime.combine(day, datetime.min.time(), _KYIV)
    return local.astimezone(UTC).replace(tzinfo=None).isoformat()


def monthly_paid(manager_id: int, since: str, as_of: str) -> dict[str, Decimal]:
    """Per-month PAID cash (EUR) for a manager, by the Kyiv register day, via ClientID->manager.

    FromDate is the payment register date stored as a UTC instant (a document dated the 1st is
    stored at 21:00/22:00 UTC of the previous day), so months and the as-of boundary are Kyiv
    business days, never the raw UTC value. Created may be the bulk import timestamp. Use the
    persisted document EuroAmount: current sync preserves its document FX/settlement conversion.
    Revaluing it through today's rate tables changes the historical cash result. Reject missing
    conversion. Owned accounting payments are balance carriers, not cash, as in gba-server.
    """
    window = history.explicit_window(since, as_of)
    rows = query(
        """
        SELECT p.FromDate AS registered_at,
               SUM(p.EuroAmount) AS amt,
               SUM(CASE WHEN p.Amount <> 0 AND (p.EuroAmount IS NULL OR p.EuroAmount = 0
                    OR SIGN(p.Amount) <> SIGN(p.EuroAmount)) THEN 1 ELSE 0 END) AS missing_conversion
        FROM dbo.IncomePaymentOrder p
        JOIN dbo.Client c ON c.ID = p.ClientID AND c.Deleted = 0 AND c.MainManagerID = :mid
        WHERE p.Deleted = 0 AND p.IsCanceled = 0
              AND p.FromDate >= :since_utc AND p.FromDate < :asof_utc
              AND NOT EXISTS (
                  SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier
                  WHERE carrier.TargetEntityType = 'IncomePaymentOrder'
                        AND carrier.TargetEntityID = p.ID
                        AND carrier.IsCurrent = 1 AND carrier.OwnershipType = 1)
        GROUP BY p.FromDate
        """,
        {
            "mid": manager_id,
            "since_utc": _kyiv_midnight_utc(window.effective_start),
            "asof_utc": _kyiv_midnight_utc(window.as_of),
        },
    )
    if any(r['missing_conversion'] for r in rows):
        raise ValueError('payment_eur_conversion_missing')
    months: dict[str, Decimal] = {}
    for row in rows:
        registered = row["registered_at"]
        if isinstance(registered, str):
            registered = datetime.fromisoformat(registered)
        month = registered.replace(tzinfo=UTC).astimezone(_KYIV).strftime("%Y-%m")
        months[month] = months.get(month, Decimal("0")) + decimal_value(row["amt"])
    return months
