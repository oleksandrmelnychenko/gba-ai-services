"""Read-only sales-history signals over ConcordDb_V5. All parameterized.

LOAD-BEARING DATA RULES (verified on ConcordDb_V5):
  - SALE-side OrderItem.PricePerItem is ALREADY EUR — never wrap/convert it. Monthly SALE
    amount (EUR) = SUM(oi.Qty * oi.PricePerItem).
  - Time windows MUST key off Order.Created. OrderItem.Created is truncated (~3 days) and unusable.
  - VALIDITY: filter sales with OrderItem.IsValidForCurrentSale = 1 — NEVER o.Deleted = 0 /
    oi.Deleted = 0 on the Sale/Order/OrderItem spine. In ConcordDb_V5 the spine rows are mostly
    Deleted = 1 (a "Deleted = 0" filter keeps only ~16% of valid OrderItems and silently drops
    ~84% of real sales — a catastrophic undercount). IsValidForCurrentSale = 1 is the canonical
    validity flag used by every other GBA AI service (solvency/products/reco/pricing); the ~77%
    valid-but-Deleted=1 spine rows ARE real sales and must be counted. This filter goes on the
    OrderItem alias; the Order spine carries no validity predicate of its own.
  - Client join path: Client.NetUID -> ClientAgreement.ClientID -> [Order].ClientAgreementID.
    A client can hold several agreements; aggregate over all of them.
  - Product join path: OrderItem.ProductID -> Product.ID where Product.NetUID = :uid.
  - Monthly grain = CONVERT(char(7), o.Created, 120); months with no sales are absent (caller fills zeros).
"""

from __future__ import annotations

from typing import Any

from app.data.db import query

# Trailing-window predicate shared by every series query. :months is the history depth.
_WINDOW = "o.Created >= DATEADD(month, -:months, :asof) AND o.Created < :asof"

# Synthetic ProductID used for debt-injection bookkeeping, never a real sale. It must be
# excluded from every EUR series so its amounts can't pollute the forecast input — see the
# note above the backtest samplers for why it matters on the by-client paths specifically.
_SYNTHETIC_PRODUCT_ID = 25422404


def client_id_for_netuid(net_uid: str) -> int | None:
    """Resolve a client NetUID (uuid string) to its dbo.Client.ID, or None if unknown."""
    rows = query(
        "SELECT TOP 1 ID AS id FROM dbo.Client WHERE NetUID = :uid AND Deleted = 0",
        {"uid": net_uid},
    )
    return int(rows[0]["id"]) if rows else None


def product_id_for_netuid(net_uid: str) -> int | None:
    """Resolve a product NetUID (uuid string) to its dbo.Product.ID, or None if unknown."""
    rows = query(
        "SELECT TOP 1 ID AS id FROM dbo.Product WHERE NetUID = :uid AND Deleted = 0",
        {"uid": net_uid},
    )
    return int(rows[0]["id"]) if rows else None


def monthly_sales_by_client(client_id: int, as_of: str, months: int) -> list[dict]:
    """Per-month EUR sale amount for one client (across all its agreements), trailing window."""
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM(oi.Qty * oi.PricePerItem) AS eur
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o    ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID = :cid AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY CONVERT(char(7), o.Created, 120)
        ORDER BY ym
        """,
        {"cid": client_id, "asof": as_of, "months": months, "synth": _SYNTHETIC_PRODUCT_ID},
    )


def monthly_sales_by_product(product_id: int, as_of: str, months: int) -> list[dict]:
    """Per-month EUR sale amount for one product across all clients, trailing window."""
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM(oi.Qty * oi.PricePerItem) AS eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.ProductID = :pid AND oi.IsValidForCurrentSale = 1
              AND {_WINDOW}
        GROUP BY CONVERT(char(7), o.Created, 120)
        ORDER BY ym
        """,
        {"pid": product_id, "asof": as_of, "months": months},
    )


def monthly_sales_by_client_and_product(
    client_id: int, product_id: int, as_of: str, months: int
) -> list[dict]:
    """Per-month EUR sale amount for one client buying one product, trailing window."""
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM(oi.Qty * oi.PricePerItem) AS eur
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o    ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID = :cid AND oi.ProductID = :pid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY CONVERT(char(7), o.Created, 120)
        ORDER BY ym
        """,
        {
            "cid": client_id,
            "pid": product_id,
            "asof": as_of,
            "months": months,
            "synth": _SYNTHETIC_PRODUCT_ID,
        },
    )


def to_series(rows: list[dict]) -> dict[str, float]:
    """Collapse repo rows ({ym, eur}) into a {yyyy-MM: eur} map (drops NULL/empty months)."""
    out: dict[str, float] = {}
    for r in rows:
        ym = r.get("ym")
        if not ym:
            continue
        out[str(ym)] = float(r["eur"] or 0.0)
    return out


def query_one(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Thin pass-through for ad-hoc parameterized reads (kept for parity/tests)."""
    return query(sql, params)


# --- Backtest sampling (offline accuracy evaluation only; not on the request path) ---------
#
# The synthetic ProductID (_SYNTHETIC_PRODUCT_ID) is excluded here too, for the same reason it
# is excluded on the live paths: its debt-injection amounts are not real demand and must never
# enter an EUR series.
#
# Note on the live paths: only the by-PRODUCT query (monthly_sales_by_product) is inherently
# safe, because it filters by the single requested product and so can never sum the synthetic
# id unless that id is explicitly requested. The by-CLIENT queries
# (monthly_sales_by_client / monthly_sales_by_client_and_product) aggregate every product the
# client bought, so they WOULD pick up the synthetic debt-injection rows; they each carry an
# explicit `oi.ProductID <> _SYNTHETIC_PRODUCT_ID` filter to keep that pollution out.


def sample_client_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{cid, ym, eur} rows for the `limit` most-active clients — one bulk read for the backtest.

    Picks clients by number of active months (so the sample spans smooth..lumpy patterns) and
    returns their full monthly EUR series in one query. Same EUR / Order.Created / validity
    (IsValidForCurrentSale = 1) rules as the live per-client query; the synthetic product is
    excluded.
    """
    return query(
        f"""
        WITH ranked AS (
            SELECT ca.ClientID AS cid,
                   COUNT(DISTINCT CONVERT(char(7), o.Created, 120)) AS active_months,
                   SUM(oi.Qty * oi.PricePerItem) AS total_eur
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o    ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synth
                  AND {_WINDOW}
            GROUP BY ca.ClientID
        ),
        top_clients AS (
            SELECT TOP (:lim) cid FROM ranked ORDER BY active_months DESC, total_eur DESC
        )
        SELECT ca.ClientID AS cid,
               CONVERT(char(7), o.Created, 120) AS ym,
               SUM(oi.Qty * oi.PricePerItem) AS eur
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o    ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID IN (SELECT cid FROM top_clients)
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY ca.ClientID, CONVERT(char(7), o.Created, 120)
        ORDER BY ca.ClientID, ym
        """,
        {"asof": as_of, "months": months, "lim": limit, "synth": _SYNTHETIC_PRODUCT_ID},
    )


def sample_product_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{pid, ym, eur} rows for the `limit` most-active products — one bulk read for the backtest.

    Mirrors sample_client_monthly_series for products. The synthetic product is excluded so it
    never enters the evaluation sample.
    """
    return query(
        f"""
        WITH ranked AS (
            SELECT oi.ProductID AS pid,
                   COUNT(DISTINCT CONVERT(char(7), o.Created, 120)) AS active_months,
                   SUM(oi.Qty * oi.PricePerItem) AS total_eur
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synth
                  AND {_WINDOW}
            GROUP BY oi.ProductID
        ),
        top_products AS (
            SELECT TOP (:lim) pid FROM ranked ORDER BY active_months DESC, total_eur DESC
        )
        SELECT oi.ProductID AS pid,
               CONVERT(char(7), o.Created, 120) AS ym,
               SUM(oi.Qty * oi.PricePerItem) AS eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.ProductID IN (SELECT pid FROM top_products)
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY oi.ProductID, CONVERT(char(7), o.Created, 120)
        ORDER BY oi.ProductID, ym
        """,
        {"asof": as_of, "months": months, "lim": limit, "synth": _SYNTHETIC_PRODUCT_ID},
    )


def group_series_by_entity(rows: list[dict], key: str) -> dict[int, dict[str, float]]:
    """Collapse flat {<key>, ym, eur} rows into {entity_id: {yyyy-MM: eur}} maps."""
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        eid = r.get(key)
        ym = r.get("ym")
        if eid is None or not ym:
            continue
        out.setdefault(int(eid), {})[str(ym)] = float(r["eur"] or 0.0)
    return out
