"""Read-only sales-history signals over ConcordDb_V5. All parameterized.

LOAD-BEARING DATA RULES (verified on ConcordDb_V5):
  - SALE-side OrderItem.PricePerItem is ALREADY EUR — never currency-convert it. Monthly SALE
    amount (EUR) = SUM(CAST(Qty AS decimal) * CAST(PricePerItem AS decimal)). Qty is stored as
    SQL float, so both operands must be cast BEFORE multiplication; otherwise SQL promotes the
    expression to binary float and a half-cent boundary can drift.
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

import hashlib
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.core.history import HistoryWindow, resolve_history_window
from app.data.db import query

# Trailing calendar-month predicate shared by every series query. ``history_start`` is the
# requested rolling start clamped to the factual source floor. Keeping both predicates is
# intentional defense-in-depth: no caller can accidentally read pre-source rows even if its
# effective-window calculation regresses.
_WINDOW = (
    "o.Created >= :source_history_start "
    "AND o.Created >= :history_start "
    "AND o.Created < :asof"
)
_EUR_AMOUNT = (
    "CAST(oi.Qty AS decimal(18, 8)) * CAST(oi.PricePerItem AS decimal(28, 14))"
)

# Synthetic «Ввід боргів» ProductID used for debt-injection bookkeeping, never a real sale. It
# must be excluded from every EUR series so its amounts can't pollute the forecast input — see
# the note above the backtest samplers for why it matters on the by-client paths specifically.
# The ID is NOT stable (the 1С sync re-mints the Product row), so it is resolved from the live
# table at startup and re-resolved hourly; settings.synthetic_product_id (env) overrides when
# set, and the last known live row is the offline fallback.
_SYNTHETIC_FALLBACK_ID = 29555414
_SYNTHETIC_REFRESH_SECONDS = 3600
_synthetic_cached: tuple[int, float, bool, str] | None = None

_SALES_SOURCE_SCHEMA_SQL = """
SELECT CASE WHEN
    OBJECT_ID(N'dbo.OrderItem', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.[Order]', N'U') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'OrderID') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'ProductID') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'Qty') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'PricePerItem') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'IsValidForCurrentSale') IS NOT NULL
    AND COL_LENGTH(N'dbo.[Order]', N'ID') IS NOT NULL
    AND COL_LENGTH(N'dbo.[Order]', N'Created') IS NOT NULL
THEN 1 ELSE 0 END AS source_schema_present
"""

_SALES_SOURCE_STATUS_SQL = """
SELECT
    COUNT_BIG(*) AS canonical_row_count,
    SUM(CASE WHEN o.Created >= :history_start THEN 1 ELSE 0 END)
        AS history_row_count,
    COUNT(DISTINCT CASE
        WHEN o.Created >= :history_start THEN oi.ProductID
    END) AS history_product_count,
    COUNT(DISTINCT CASE
        WHEN o.Created >= :history_start THEN ca.ClientID
    END) AS history_client_count,
    MAX(o.Created) AS latest_sale_at,
    SUM(CASE
        WHEN oi.Qty IS NULL OR oi.PricePerItem IS NULL
             OR oi.Qty < 0 OR oi.PricePerItem < 0
        THEN 1 ELSE 0
    END) AS invalid_value_row_count
FROM dbo.OrderItem oi
JOIN dbo.[Order] o ON o.ID = oi.OrderID
LEFT JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
WHERE oi.IsValidForCurrentSale = 1
      AND oi.ProductID <> :synth
      AND o.Created >= :source_history_start
      AND o.Created < :asof
"""


def _resolved_history_window(as_of: str | datetime, months: int) -> HistoryWindow:
    return resolve_history_window(
        as_of,
        months,
        get_settings().source_history_start_date,
    )


def _history_query_params(as_of: str | datetime, months: int) -> tuple[HistoryWindow, dict[str, Any]]:
    window = _resolved_history_window(as_of, months)
    return window, {
        "asof": as_of,
        "source_history_start": window.source_history_start.isoformat(),
        "history_start": window.effective_start.isoformat(),
    }


def synthetic_product_id() -> int:
    """Current dbo.Product.ID of the synthetic «Ввід боргів» debt-entry product (cached ~1h)."""
    return int(synthetic_product_status()["product_id"])


def synthetic_product_status() -> dict[str, Any]:
    """Resolve the synthetic row and expose whether exclusion is factually verified."""
    global _synthetic_cached
    override = get_settings().synthetic_product_id
    now = time.monotonic()
    if _synthetic_cached is not None and now - _synthetic_cached[1] < _SYNTHETIC_REFRESH_SECONDS:
        return _synthetic_status_dict(_synthetic_cached)
    try:
        if override:
            rows = query(
                "SELECT TOP 1 ID AS id FROM dbo.Product "
                "WHERE ID = :id AND Name = N'Ввід боргів' AND Deleted = 0",
                {"id": int(override)},
            )
        else:
            rows = query(
                "SELECT TOP 1 ID AS id FROM dbo.Product "
                "WHERE Name = N'Ввід боргів' AND Deleted = 0 ORDER BY ID DESC"
            )
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        source = "verified_override" if override else "verified_database"
        _synthetic_cached = (int(rows[0]["id"]), now, True, source)
    else:
        fallback = _synthetic_cached[0] if _synthetic_cached is not None else _SYNTHETIC_FALLBACK_ID
        _synthetic_cached = (fallback, now, False, "unverified_fallback")
    return _synthetic_status_dict(_synthetic_cached)


def _synthetic_status_dict(state: tuple[int, float, bool, str]) -> dict[str, Any]:
    return {
        "product_id": state[0],
        "resolved": state[2],
        "source": state[3],
    }


def sales_source_status(as_of: datetime, months: int) -> dict[str, Any]:
    """Return a read-only factual-source snapshot used by health/readiness checks.

    ``as_of`` is the same exclusive upper bound used by the forecast queries, so readiness
    cannot be green for rows that the model itself would not yet consume.
    """
    _, window_params = _history_query_params(as_of, months)
    schema_rows = query(_SALES_SOURCE_SCHEMA_SQL)
    schema_present = bool(schema_rows and schema_rows[0].get("source_schema_present"))
    if not schema_present:
        return {
            "source_schema_present": False,
            "canonical_row_count": 0,
            "history_row_count": 0,
            "history_product_count": 0,
            "history_client_count": 0,
            "latest_sale_at": None,
            "invalid_value_row_count": 0,
        }

    rows = query(
        _SALES_SOURCE_STATUS_SQL,
        {
            **window_params,
            "synth": synthetic_product_id(),
        },
    )
    row = rows[0] if rows else {}
    return {
        "source_schema_present": True,
        "canonical_row_count": int(row.get("canonical_row_count") or 0),
        "history_row_count": int(row.get("history_row_count") or 0),
        "history_product_count": int(row.get("history_product_count") or 0),
        "history_client_count": int(row.get("history_client_count") or 0),
        "latest_sale_at": row.get("latest_sale_at"),
        "invalid_value_row_count": int(row.get("invalid_value_row_count") or 0),
    }


def forecast_source_fingerprint(
    client_id: int | None,
    product_id: int | None,
    as_of: str,
    months: int,
) -> str:
    """Stable read-only epoch for every factual row a requested response can consume.

    It deliberately covers the union of the client and product scopes (the pair is a subset).
    Count/sums/checksum plus update timestamps make cache reuse conditional on the underlying
    factual quantities, prices, identity links, validity, and sale dates remaining unchanged.
    """
    window, params = _history_query_params(as_of, months)
    boundary_parts = (
        window.as_of.isoformat(),
        months,
        window.source_history_start.isoformat(),
        window.effective_start.isoformat(),
        window.history_complete,
    )
    if client_id is None and product_id is None:
        return hashlib.sha256(
            "|".join(map(str, ("no-scope", *boundary_parts))).encode()
        ).hexdigest()[:24]

    params["synth"] = synthetic_product_id()
    if client_id is not None and product_id is not None:
        scope = "(ca.ClientID = :cid OR oi.ProductID = :pid)"
        params.update({"cid": client_id, "pid": product_id})
    elif client_id is not None:
        scope = "ca.ClientID = :cid"
        params["cid"] = client_id
    else:
        scope = "oi.ProductID = :pid"
        params["pid"] = product_id

    rows = query(
        f"""
        SELECT
            COUNT_BIG(*) AS row_count,
            MAX(oi.ID) AS max_item_id,
            MAX(oi.Updated) AS max_item_updated,
            MAX(o.Updated) AS max_order_updated,
            MAX(o.Created) AS max_order_created,
            SUM(CAST(oi.Qty AS decimal(38, 6))) AS quantity_sum,
            SUM({_EUR_AMOUNT}) AS amount_sum,
            CHECKSUM_AGG(BINARY_CHECKSUM(
                oi.ID,
                oi.OrderID,
                oi.ProductID,
                oi.Qty,
                oi.PricePerItem,
                oi.IsValidForCurrentSale,
                o.Created,
                ca.ClientID
            )) AS row_checksum
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        LEFT JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {scope}
              AND {_WINDOW}
        """,
        params,
    )
    row = rows[0] if rows else {}
    epoch_parts = (
        client_id,
        product_id,
        *boundary_parts,
        params["synth"],
        row.get("row_count") or 0,
        row.get("max_item_id") or 0,
        row.get("max_item_updated") or "",
        row.get("max_order_updated") or "",
        row.get("max_order_created") or "",
        row.get("quantity_sum") or 0,
        row.get("amount_sum") or 0,
        row.get("row_checksum") or 0,
    )
    return hashlib.sha256("|".join(map(str, epoch_parts)).encode()).hexdigest()[:24]


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
    _, params = _history_query_params(as_of, months)
    params.update({"cid": client_id, "synth": synthetic_product_id()})
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM({_EUR_AMOUNT}) AS eur
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o    ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID = :cid AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY CONVERT(char(7), o.Created, 120)
        ORDER BY ym
        """,
        params,
    )


def monthly_sales_by_product(product_id: int, as_of: str, months: int) -> list[dict]:
    """Per-month EUR sale amount for one product across all clients, trailing window."""
    _, params = _history_query_params(as_of, months)
    params["pid"] = product_id
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM({_EUR_AMOUNT}) AS eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.ProductID = :pid AND oi.IsValidForCurrentSale = 1
              AND {_WINDOW}
        GROUP BY CONVERT(char(7), o.Created, 120)
        ORDER BY ym
        """,
        params,
    )


def monthly_sales_by_client_and_product(
    client_id: int, product_id: int, as_of: str, months: int
) -> list[dict]:
    """Per-month EUR sale amount for one client buying one product, trailing window."""
    _, params = _history_query_params(as_of, months)
    params.update(
        {
            "cid": client_id,
            "pid": product_id,
            "synth": synthetic_product_id(),
        }
    )
    return query(
        f"""
        SELECT CONVERT(char(7), o.Created, 120) AS ym,
               SUM({_EUR_AMOUNT}) AS eur
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
        params,
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


def history_summary(
    rows: list[dict], *, max_months: int | None = None
) -> dict[str, Decimal | int]:
    """Exact aggregate metadata for unique monthly rows; money stays Decimal until API cents."""
    month_count = 0
    non_zero_month_count = 0
    total_eur = Decimal("0")
    seen_months: set[str] = set()
    for row in rows:
        if not row.get("ym"):
            continue
        month = str(row["ym"])
        if month in seen_months:
            raise ValueError(f"sales history contains duplicate month {month}")
        seen_months.add(month)
        value = Decimal(str(row.get("eur") or 0))
        if not value.is_finite() or value < 0:
            raise ValueError("sales history contains a non-finite or negative EUR amount")
        month_count += 1
        if max_months is not None and month_count > max_months:
            raise ValueError("sales history exceeds the configured calendar-month window")
        non_zero_month_count += int(value > 0)
        total_eur += value
    return {
        "month_count": month_count,
        "non_zero_month_count": non_zero_month_count,
        "total_eur": total_eur,
    }


def query_one(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Thin pass-through for ad-hoc parameterized reads (kept for parity/tests)."""
    return query(sql, params)


# --- Backtest sampling (offline accuracy evaluation only; not on the request path) ---------
#
# The synthetic ProductID (synthetic_product_id()) is excluded here too, for the same reason it
# is excluded on the live paths: its debt-injection amounts are not real demand and must never
# enter an EUR series.
#
# Note on the live paths: only the by-PRODUCT query (monthly_sales_by_product) is inherently
# safe, because it filters by the single requested product and so can never sum the synthetic
# id unless that id is explicitly requested. The by-CLIENT queries
# (monthly_sales_by_client / monthly_sales_by_client_and_product) aggregate every product the
# client bought, so they WOULD pick up the synthetic debt-injection rows; they each carry an
# explicit `oi.ProductID <> synthetic_product_id()` filter to keep that pollution out.


def sample_client_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{cid, ym, eur} rows for the `limit` most-active clients — one bulk read for the backtest.

    Picks clients by number of active months (so the sample spans smooth..lumpy patterns) and
    returns their full monthly EUR series in one query. Same EUR / Order.Created / validity
    (IsValidForCurrentSale = 1) rules as the live per-client query; the synthetic product is
    excluded.
    """
    _, params = _history_query_params(as_of, months)
    params.update({"lim": limit, "synth": synthetic_product_id()})
    return query(
        f"""
        WITH ranked AS (
            SELECT ca.ClientID AS cid,
                   COUNT(DISTINCT CONVERT(char(7), o.Created, 120)) AS active_months,
                   SUM({_EUR_AMOUNT}) AS total_eur
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
               SUM({_EUR_AMOUNT}) AS eur
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
        params,
    )


def sample_product_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{pid, ym, eur} rows for the `limit` most-active products — one bulk read for the backtest.

    Mirrors sample_client_monthly_series for products. The synthetic product is excluded so it
    never enters the evaluation sample.
    """
    _, params = _history_query_params(as_of, months)
    params.update({"lim": limit, "synth": synthetic_product_id()})
    return query(
        f"""
        WITH ranked AS (
            SELECT oi.ProductID AS pid,
                   COUNT(DISTINCT CONVERT(char(7), o.Created, 120)) AS active_months,
                   SUM({_EUR_AMOUNT}) AS total_eur
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
               SUM({_EUR_AMOUNT}) AS eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.ProductID IN (SELECT pid FROM top_products)
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND {_WINDOW}
        GROUP BY oi.ProductID, CONVERT(char(7), o.Created, 120)
        ORDER BY oi.ProductID, ym
        """,
        params,
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
