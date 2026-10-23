"""Truthful per-product analytics assembled from current profile + actual monthly sales.

The module is deliberately DB-free. The repository returns sparse calendar-month aggregates; this
layer validates them, fills missing months with zeros, and marks the as-of month as partial. It does
not manufacture a stock time series: stock remains a current field of the product snapshot.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
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
        label: {"units": Decimal(0), "order_count": 0, "revenue_eur": Decimal(0),
                "returned_units": Decimal(0), "known_returned_value_eur": Decimal(0), "unresolved_amount_count": 0}
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
        if order_count == 0 and (units != 0 or revenue != 0):
            raise ValueError("monthly sales aggregate has values without an order")
        buckets[label] = {
            "units": units,
            "order_count": order_count,
            "revenue_eur": revenue,
            **_return_bucket(row),
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
                period_start=max(period_start, window.effective_start).isoformat(),
                period_end_exclusive=period_end.isoformat(),
                is_complete=is_complete,
                units=exact.quantity(units, "monthly units", non_negative=False),
                order_count=bucket["order_count"],
                revenue_eur=exact.money(revenue, "monthly revenue_eur", non_negative=False),
                gross_revenue_eur=exact.money(revenue, "monthly gross revenue", non_negative=False),
                **_financial_values(revenue, bucket),
                financial_basis={"revenue_eur": format(revenue, "f"), "units": format(units, "f"),
                    "known_returned_value_eur": format(bucket["known_returned_value_eur"], "f"),
                    "unresolved_amount_count": bucket["unresolved_amount_count"]},
                avg_price_eur=(
                    exact.unit_price(avg_price, "monthly avg_price_eur", non_negative=False)
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
    known_returns = Decimal(0)
    returned_units = Decimal(0)
    unresolved = 0
    return_coverage_known = True
    with localcontext() as context:
        context.prec = 50
        for bucket in buckets:
            units += bucket["units"]
            revenue += bucket["revenue_eur"]
            known_returns += bucket["known_returned_value_eur"]
            if bucket["unresolved_amount_count"] is None or bucket["returned_units"] is None:
                return_coverage_known = False
            else:
                unresolved += bucket["unresolved_amount_count"]
                returned_units += bucket["returned_units"]

    aligned = dict(snapshot)
    aligned.update(_financial_values(revenue, {
        "known_returned_value_eur": known_returns,
        "returned_units": returned_units if return_coverage_known else None,
        "unresolved_amount_count": unresolved if return_coverage_known else None,
    }))
    aligned["financial_basis"] = {"revenue_eur": format(revenue, "f"), "units": format(units, "f"),
        "known_returned_value_eur": format(known_returns, "f"),
        "unresolved_amount_count": unresolved if return_coverage_known else None}
    aligned["gross_revenue_eur"] = exact.money(revenue, "gross revenue", non_negative=False)
    aligned["annual_units"] = exact.quantity(units, "annual_units", non_negative=False)
    aligned["revenue_eur"] = exact.money(revenue, "revenue_eur", non_negative=False)
    avg_price = (revenue / units) if units > 0 else None
    aligned["avg_price_eur"] = (
        exact.unit_price(avg_price, "avg_price_eur", non_negative=False) if avg_price is not None else None
    )

    aligned["margin_pct"] = None
    unit_cost = snapshot.get("unit_cost_eur")
    if avg_price is not None and avg_price > 0 and unit_cost is not None:
        cost = exact.decimal_value(unit_cost, "unit_cost_eur", non_negative=True)
        aligned["margin_pct"] = exact.ratio(
            (avg_price - cost) / avg_price,
            "margin_pct",
        )

    return aligned


def _return_bucket(row: dict) -> dict:
    """A legacy gross-only caller has unknown return coverage, never invented zero returns."""
    fields = {"returned_units", "known_returned_value_eur", "unresolved_amount_count"}
    if not fields.intersection(row):
        return {"returned_units": None, "known_returned_value_eur": Decimal(0), "unresolved_amount_count": None}
    if not fields.issubset(row):
        raise ValueError("monthly return evidence is incomplete")
    return {
        "returned_units": exact.decimal_value(row["returned_units"], "returned units"),
        "known_returned_value_eur": exact.decimal_value(row["known_returned_value_eur"], "known returned amount"),
        "unresolved_amount_count": exact.non_negative_int(row["unresolved_amount_count"], "unresolved amount count"),
    }


def _financial_values(gross: Decimal, bucket: dict) -> dict:
    returned = bucket["known_returned_value_eur"]
    unresolved = bucket["unresolved_amount_count"]
    complete = unresolved == 0
    with localcontext() as context:
        context.prec = 50
        known_net = gross - returned
    return {
        "returned_units": None if bucket["returned_units"] is None else exact.quantity(bucket["returned_units"], non_negative=False),
        "returned_value_eur": exact.money(returned, non_negative=False) if complete else None,
        "known_returned_value_eur": exact.money(returned, non_negative=False),
        "net_revenue_eur": exact.money(known_net, non_negative=False) if complete else None,
        "known_net_revenue_eur": exact.money(known_net, non_negative=False),
        "unresolved_amount_count": unresolved,
    }
