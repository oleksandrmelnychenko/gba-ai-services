"""Read-only sales-history signals over ConcordDb_V5. All parameterized.

Every series reads the facts defined once in app.data.source_rules (gba-server's confirmed
sale net of buyer returns on the returned line, plus the negative facts of returns anchored to
import placeholders, Europe/Kyiv months; a month never goes below zero). Amounts are EUR:
OrderItem.PricePerItem is already EUR, and Qty is a SQL float, so both operands are cast to
decimal before multiplying. Months with no sales are absent (the caller fills zeros).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.core.history import HistoryWindow, resolve_history_window
from app.data import source_rules as rules
from app.data.db import query

_WINDOW = rules.window("f.fact_at")
_FACTS = rules.net_sales_facts()
_NET_EUR = rules.monthly_net_eur()

# Synthetic «Ввід боргів» ProductID used for debt-injection bookkeeping, never a real sale. It
# must be excluded from every EUR series so its amounts can't pollute the forecast input — see
# the note above the backtest samplers for why it matters on the by-client paths specifically.
# The ID is NOT stable (the 1С sync re-mints the Product row), so it is resolved from the live
# table at startup and re-resolved hourly; settings.synthetic_product_id (env) overrides when
# set. There is no hardcoded fallback: an unresolved identity fails loudly.
_SYNTHETIC_REFRESH_SECONDS = 3600
_synthetic_cached: tuple[int | None, float, bool, str] | None = None

_SALES_SOURCE_SCHEMA_SQL = """
SELECT CASE WHEN
    OBJECT_ID(N'dbo.OrderItem', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.[Order]', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.Sale', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.SaleReturn', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.SaleReturnItem', N'U') IS NOT NULL
    AND OBJECT_ID(N'dbo.DataSyncAccountingSourceTarget', N'U') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'OrderID') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'ProductID') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'Qty') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'PricePerItem') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'IsValidForCurrentSale') IS NOT NULL
    AND COL_LENGTH(N'dbo.OrderItem', N'Deleted') IS NOT NULL
    AND COL_LENGTH(N'dbo.[Order]', N'ID') IS NOT NULL
    AND COL_LENGTH(N'dbo.Sale', N'OrderID') IS NOT NULL
    AND COL_LENGTH(N'dbo.Sale', N'ClientAgreementID') IS NOT NULL
    AND COL_LENGTH(N'dbo.Sale', N'ChangedToInvoice') IS NOT NULL
    AND COL_LENGTH(N'dbo.Sale', N'Deleted') IS NOT NULL
    AND COL_LENGTH(N'dbo.SaleReturn', N'FromDate') IS NOT NULL
    AND COL_LENGTH(N'dbo.SaleReturn', N'IsCanceled') IS NOT NULL
    AND COL_LENGTH(N'dbo.SaleReturn', N'ClientAgreementID') IS NOT NULL
    AND COL_LENGTH(N'dbo.SaleReturnItem', N'OrderItemID') IS NOT NULL
    AND COL_LENGTH(N'dbo.SaleReturnItem', N'Qty') IS NOT NULL
THEN 1 ELSE 0 END AS source_schema_present
"""

_SALES_SOURCE_STATUS_SQL = f"""
SELECT
    COUNT_BIG(*) AS canonical_row_count,
    SUM(CASE WHEN f.fact_at >= :history_start THEN 1 ELSE 0 END)
        AS history_row_count,
    COUNT(DISTINCT CASE
        WHEN f.fact_at >= :history_start THEN f.product_id
    END) AS history_product_count,
    COUNT(DISTINCT CASE
        WHEN f.fact_at >= :history_start THEN f.client_id
    END) AS history_client_count,
    MAX(f.fact_at) AS latest_sale_at,
    SUM(CASE
        WHEN f.qty IS NULL OR f.price IS NULL OR f.price < 0
        THEN 1 ELSE 0
    END) AS invalid_value_row_count
FROM {rules.net_sales_facts(whole_source=True)}
WHERE f.product_id <> :synth
      AND {rules.SALE_FACT}
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
        "asof": rules.utc_instant(as_of),
        "source_history_start": rules.utc_instant(window.source_history_start),
        "history_start": rules.utc_instant(window.effective_start),
    }


def _month(params: dict[str, Any]) -> str:
    start, end = params["source_history_start"], params["asof"]
    params.update(rules.zone_params(start, end))
    return rules.fact_month(start, end)


def synthetic_product_id() -> int:
    """Current dbo.Product.ID of the synthetic «Ввід боргів» debt-entry product (cached ~1h)."""
    status = synthetic_product_status()
    if not status["resolved"] or status["product_id"] is None:
        raise RuntimeError("synthetic_product_unresolved")
    return int(status["product_id"])


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
        last_known = _synthetic_cached[0] if _synthetic_cached is not None else None
        _synthetic_cached = (last_known, now, False, "unresolved")
    return _synthetic_status_dict(_synthetic_cached)


def _synthetic_status_dict(state: tuple[int | None, float, bool, str]) -> dict[str, Any]:
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


_PLACEHOLDER_GRAINS = {
    "by_client": ("f.client_id",),
    "by_product": ("f.product_id",),
    "by_client_and_product": ("f.client_id", "f.product_id"),
}


def placeholder_return_diagnostic(as_of: str | datetime, months: int) -> dict[str, Any]:
    """Returns anchored to import placeholders: EUR booked and EUR a zero-floored month dropped."""
    _, params = _history_query_params(as_of, months)
    params["synth"] = synthetic_product_id()
    month = _month(params)
    totals = query(
        f"""
        SELECT COUNT_BIG(*) AS line_count,
               COUNT(DISTINCT f.client_id) AS client_count,
               COUNT(DISTINCT f.product_id) AS product_count,
               ISNULL(-SUM(f.eur), 0) AS return_eur
        FROM {_FACTS}
        WHERE f.placeholder_return = 1 AND f.product_id <> :synth
        """,
        params,
    )
    total = totals[0] if totals else {}
    out: dict[str, Any] = {
        "line_count": int(total.get("line_count") or 0),
        "client_count": int(total.get("client_count") or 0),
        "product_count": int(total.get("product_count") or 0),
        "return_eur": Decimal(str(total.get("return_eur") or 0)),
    }
    for name, columns in _PLACEHOLDER_GRAINS.items():
        keys = ", ".join(columns)
        scope = " AND ".join(
            f"{column} IN (SELECT r.{column.split('.')[1]} "
            f"FROM ({rules.placeholder_return_facts()}) r)"
            for column in columns
        )
        rows = query(
            f"""
            SELECT COUNT_BIG(*) AS month_count,
                   ISNULL(SUM(b.return_eur), 0) AS return_eur,
                   ISNULL(SUM(b.dropped_eur), 0) AS dropped_eur,
                   ISNULL(SUM(CASE WHEN b.dropped_eur > 0 THEN 1 ELSE 0 END), 0)
                       AS dropped_month_count
            FROM (
                SELECT {rules.placeholder_eur()} AS return_eur,
                       {rules.dropped_placeholder_eur()} AS dropped_eur
                FROM {_FACTS}
                WHERE f.product_id <> :synth AND {scope}
                GROUP BY {keys}, {month}
                HAVING MAX(f.placeholder_return) = 1
            ) b
            """,
            params,
        )
        row = rows[0] if rows else {}
        booked = Decimal(str(row.get("return_eur") or 0))
        dropped = Decimal(str(row.get("dropped_eur") or 0))
        out[name] = {
            "month_count": int(row.get("month_count") or 0),
            "absorbed_eur": booked - dropped,
            "dropped_eur": dropped,
            "dropped_month_count": int(row.get("dropped_month_count") or 0),
        }
    return out


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
    facts = _FACTS
    if client_id is not None and product_id is not None:
        facts = (
            f"(SELECT * FROM {_FACTS} WHERE f.client_id = :cid "
            f"UNION ALL "
            f"SELECT * FROM {_FACTS} WHERE f.product_id = :pid AND f.client_id <> :cid) f"
        )
        scope = "1 = 1"
        params.update({"cid": client_id, "pid": product_id})
    elif client_id is not None:
        scope = "f.client_id = :cid"
        params["cid"] = client_id
    else:
        scope = "f.product_id = :pid"
        params["pid"] = product_id

    rows = query(
        f"""
        SELECT
            COUNT_BIG(*) AS row_count,
            MAX(f.fact_id) AS max_item_id,
            MAX(f.line_updated) AS max_item_updated,
            MAX(f.document_updated) AS max_order_updated,
            MAX(f.return_updated) AS max_return_updated,
            MAX(f.fact_at) AS max_order_created,
            SUM(f.qty) AS quantity_sum,
            SUM(f.eur) AS amount_sum,
            CHECKSUM_AGG(BINARY_CHECKSUM(
                f.fact_id,
                f.product_id,
                f.qty,
                f.returned_qty,
                f.price,
                f.fact_at,
                f.client_id
            )) AS row_checksum
        FROM {facts}
        WHERE f.product_id <> :synth
              AND {scope}
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
        row.get("max_return_updated") or "",
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
    """Per-month net EUR sales for one client (across all its agreements), trailing window."""
    _, params = _history_query_params(as_of, months)
    params.update({"cid": client_id, "synth": synthetic_product_id()})
    month = _month(params)
    return query(
        f"""
        SELECT {month} AS ym,
               {_NET_EUR} AS eur
        FROM {_FACTS}
        WHERE f.client_id = :cid
              AND f.product_id <> :synth
        GROUP BY {month}
        ORDER BY ym
        """,
        params,
    )


def monthly_sales_by_product(product_id: int, as_of: str, months: int) -> list[dict]:
    """Per-month net EUR sales for one product across all clients, trailing window."""
    _, params = _history_query_params(as_of, months)
    params["pid"] = product_id
    month = _month(params)
    return query(
        f"""
        SELECT {month} AS ym,
               {_NET_EUR} AS eur
        FROM {_FACTS}
        WHERE f.product_id = :pid
        GROUP BY {month}
        ORDER BY ym
        """,
        params,
    )


def monthly_sales_by_client_and_product(
    client_id: int, product_id: int, as_of: str, months: int
) -> list[dict]:
    """Per-month net EUR sales for one client buying one product, trailing window."""
    _, params = _history_query_params(as_of, months)
    params.update(
        {
            "cid": client_id,
            "pid": product_id,
            "synth": synthetic_product_id(),
        }
    )
    month = _month(params)
    return query(
        f"""
        SELECT {month} AS ym,
               {_NET_EUR} AS eur
        FROM {_FACTS}
        WHERE f.client_id = :cid AND f.product_id = :pid
              AND f.product_id <> :synth
        GROUP BY {month}
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


def sample_client_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{cid, ym, eur} rows for the `limit` most-active clients — one bulk read for the backtest."""
    _, params = _history_query_params(as_of, months)
    params.update({"lim": limit, "synth": synthetic_product_id()})
    month = _month(params)
    return query(
        f"""
        WITH monthly AS (
            SELECT f.client_id AS entity_id,
                   {month} AS ym,
                   {_NET_EUR} AS eur
            FROM {_FACTS}
            WHERE f.product_id <> :synth
            GROUP BY f.client_id, {month}
        ),
        top_entities AS (
            SELECT TOP (:lim) entity_id
            FROM monthly
            GROUP BY entity_id
            ORDER BY COUNT(*) DESC, SUM(eur) DESC
        )
        SELECT m.entity_id AS cid, m.ym, m.eur
        FROM monthly m
        WHERE m.entity_id IN (SELECT entity_id FROM top_entities)
        ORDER BY m.entity_id, m.ym
        """,
        params,
    )


def sample_product_monthly_series(as_of: str, months: int, limit: int) -> list[dict]:
    """{pid, ym, eur} rows for the `limit` most-active products — one bulk read for the backtest."""
    _, params = _history_query_params(as_of, months)
    params.update({"lim": limit, "synth": synthetic_product_id()})
    month = _month(params)
    return query(
        f"""
        WITH monthly AS (
            SELECT f.product_id AS entity_id,
                   {month} AS ym,
                   {_NET_EUR} AS eur
            FROM {_FACTS}
            WHERE f.product_id <> :synth
            GROUP BY f.product_id, {month}
        ),
        top_entities AS (
            SELECT TOP (:lim) entity_id
            FROM monthly
            GROUP BY entity_id
            ORDER BY COUNT(*) DESC, SUM(eur) DESC
        )
        SELECT m.entity_id AS pid, m.ym, m.eur
        FROM monthly m
        WHERE m.entity_id IN (SELECT entity_id FROM top_entities)
        ORDER BY m.entity_id, m.ym
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


def all_client_monthly_series(as_of: str, months: int) -> list[dict]:
    _, params = _history_query_params(as_of, months)
    params["synth"] = synthetic_product_id()
    month = _month(params)
    return query(
        f"""
        SELECT f.client_id AS cid,
               {month} AS ym,
               {_NET_EUR} AS eur
        FROM {_FACTS}
        WHERE f.product_id <> :synth
        GROUP BY f.client_id, {month}
        """,
        params,
    )


def all_product_monthly_series(as_of: str, months: int) -> list[dict]:
    _, params = _history_query_params(as_of, months)
    params["synth"] = synthetic_product_id()
    month = _month(params)
    return query(
        f"""
        SELECT f.product_id AS pid,
               {month} AS ym,
               {_NET_EUR} AS eur
        FROM {_FACTS}
        WHERE f.product_id <> :synth
        GROUP BY f.product_id, {month}
        """,
        params,
    )
