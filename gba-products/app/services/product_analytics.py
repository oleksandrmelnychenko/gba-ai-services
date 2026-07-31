"""Truthful per-product analytics assembled from current profile + actual monthly sales.

The module is deliberately DB-free. The repository returns sparse calendar-month aggregates; this
layer validates them, fills missing months with zeros, and marks the as-of month as partial. It does
not manufacture a stock time series: stock remains a current field of the product snapshot.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core import exact_numbers as exact
from app.core.config import get_settings
from app.core.history import (
    HistoryWindow,
    history_contract_fingerprint,
    month_history_window,
)
from app.domain.models import (
    MonthlySalesPoint,
    ProductAnalyticsDataQuality,
    ProductAnalyticsResponse,
    ProductAnalyticsWindow,
)
from app.services.classification import month_labels


def _first_of_month(label: str) -> date:
    return date.fromisoformat(f"{label}-01")


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _number(value: Any, field: str) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field} in monthly sales row") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite {field} in monthly sales row")
    return number


def _order_count(value: Any) -> int:
    number = _number(value, "order_count")
    if number != number.to_integral_value() or number < 0:
        raise ValueError("invalid order_count in monthly sales row")
    return int(number)


def sales_history_window(
    as_of: str,
    months: int,
    source_history_start: date | None = None,
) -> HistoryWindow:
    """Resolve the requested calendar-month window against factual source history."""
    if not 1 <= months <= 24:
        raise ValueError("months must be between 1 and 24")
    floor = source_history_start or get_settings().source_history_start_date
    return month_history_window(as_of, months, floor)


def sales_window_start(
    as_of: str,
    months: int,
    source_history_start: date | None = None,
) -> str:
    """First factual calendar day included in a trailing dense monthly window."""
    return sales_history_window(as_of, months, source_history_start).effective_start.isoformat()


def build_product_analytics(
    *,
    product_id: int,
    as_of: str,
    months: int,
    model_version: str,
    snapshot: dict[str, Any],
    monthly_rows: list[dict[str, Any]],
    source_history_start: date | None = None,
) -> ProductAnalyticsResponse:
    """Build a validated dense analytics response from sparse repository aggregates."""
    if product_id <= 0:
        raise ValueError("product_id must be positive")
    if not 1 <= months <= 24:
        raise ValueError("months must be between 1 and 24")

    window = sales_history_window(as_of, months, source_history_start)
    as_of_date = window.as_of
    labels = month_labels(as_of, months, window.source_history_start)
    buckets = {
        label: {"units": Decimal(0), "order_count": 0, "revenue_eur": Decimal(0)}
        for label in labels
    }
    seen: set[str] = set()
    for row in monthly_rows:
        label = str(row.get("ym") or "")
        if label not in buckets:
            raise ValueError(f"unexpected monthly sales bucket: {label or '<missing>'}")
        if label in seen:
            raise ValueError(f"duplicate monthly sales bucket: {label}")
        seen.add(label)
        units = _number(row.get("units"), "units")
        order_count = _order_count(row.get("order_count"))
        revenue = _number(row.get("revenue_eur"), "revenue_eur")
        if units < 0 or revenue < 0:
            raise ValueError("monthly sales quantities and revenue must be non-negative")
        if order_count == 0 and (units != 0 or revenue != 0):
            raise ValueError("monthly sales aggregate has values without an order")
        if units == 0 and revenue != 0:
            raise ValueError("monthly sales aggregate has revenue without quantity")
        buckets[label] = {
            "units": units,
            "order_count": order_count,
            "revenue_eur": revenue,
        }

    current_label = as_of[:7]
    series: list[MonthlySalesPoint] = []
    for label in labels:
        period_start = _first_of_month(label)
        is_complete = label != current_label
        period_end = _next_month(period_start) if is_complete else as_of_date
        bucket = buckets[label]
        units = bucket["units"]
        revenue = bucket["revenue_eur"]
        avg_price = revenue / units if units != 0 else None
        series.append(
            MonthlySalesPoint(
                month=label,
                period_start=period_start.isoformat(),
                period_end_exclusive=period_end.isoformat(),
                is_complete=is_complete,
                units=exact.quantity(units, "monthly units"),
                order_count=bucket["order_count"],
                revenue_eur=exact.money(revenue, "monthly revenue_eur"),
                avg_price_eur=(
                    exact.unit_price(avg_price, "monthly avg_price_eur")
                    if avg_price is not None
                    else None
                ),
            )
        )

    # The portfolio snapshot aggregates sales over a rolling day window
    # (settings.dead_window_days), while this response declares a month-aligned window and
    # builds its series from it. Publishing both as one period made revenue_eur disagree with
    # annual_units and with the series, so the window-scoped figures are recomputed here from
    # the very buckets the caller sees.
    snapshot = _align_snapshot_to_window(snapshot, buckets.values())

    return ProductAnalyticsResponse(
        product_id=product_id,
        as_of=as_of,
        model_version=model_version,
        source_history_start=window.source_history_start,
        requested_start=window.requested_start,
        effective_start=window.effective_start,
        history_complete=window.history_complete,
        history_fingerprint=history_contract_fingerprint(window.source_history_start),
        window=ProductAnalyticsWindow(
            months=months,
            source_history_start=window.source_history_start,
            requested_start=window.requested_start,
            effective_start=window.effective_start,
            start=window.effective_start,
            end_exclusive=as_of,
            history_complete=window.history_complete,
            effective_days=window.effective_days,
        ),
        snapshot=snapshot,
        sales_series=series,
        data_quality=ProductAnalyticsDataQuality(
            source_history_start=window.source_history_start,
            requested_start=window.requested_start,
            effective_start=window.effective_start,
            history_complete=window.history_complete,
            zero_fill_begins_at=window.effective_start,
        ),
    )


def _align_snapshot_to_window(
    snapshot: dict[str, Any],
    buckets: Any,
) -> dict[str, Any]:
    """Recompute the sales aggregates of a snapshot over this response's own window."""
    units = Decimal(0)
    revenue = Decimal(0)
    for bucket in buckets:
        units += bucket["units"]
        revenue += bucket["revenue_eur"]

    aligned = dict(snapshot)
    aligned["annual_units"] = exact.quantity(units, "annual_units")
    aligned["revenue_eur"] = exact.money(revenue, "revenue_eur")
    avg_price = (revenue / units) if units > 0 else None
    aligned["avg_price_eur"] = (
        exact.unit_price(avg_price, "avg_price_eur") if avg_price is not None else None
    )

    unit_cost = snapshot.get("unit_cost_eur")
    if avg_price is not None and avg_price > 0 and unit_cost is not None:
        cost = exact.decimal_value(unit_cost, "unit_cost_eur", non_negative=True)
        aligned["margin_pct"] = exact.ratio(
            (avg_price - cost) / avg_price,
            "margin_pct",
        )

    _assert_window_consistency(aligned)
    return aligned


def _assert_window_consistency(snapshot: dict[str, Any]) -> None:
    """Guard the contract the payload advertises: avg_price_eur == revenue_eur / units."""
    units = snapshot.get("annual_units") or 0
    revenue = snapshot.get("revenue_eur") or 0
    avg_price = snapshot.get("avg_price_eur")
    if not units or avg_price is None:
        return

    derived = Decimal(str(revenue)) / Decimal(str(units))
    if abs(derived - Decimal(str(avg_price))) > Decimal("0.01"):
        raise ValueError(
            "snapshot sales aggregates come from different windows: "
            f"revenue_eur/annual_units={derived} but avg_price_eur={avg_price}"
        )
