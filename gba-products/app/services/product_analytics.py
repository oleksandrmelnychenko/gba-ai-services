"""Truthful per-product analytics assembled from current profile + actual monthly sales.

The module is deliberately DB-free. The repository returns sparse calendar-month aggregates; this
layer validates them, fills missing months with zeros, and marks the as-of month as partial. It does
not manufacture a stock time series: stock remains a current field of the product snapshot.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

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


def sales_window_start(as_of: str, months: int) -> str:
    """First calendar day included in a trailing dense monthly window."""
    if not 1 <= months <= 24:
        raise ValueError("months must be between 1 and 24")
    return _first_of_month(month_labels(as_of, months)[0]).isoformat()


def build_product_analytics(
    *,
    product_id: int,
    as_of: str,
    months: int,
    model_version: str,
    snapshot: dict[str, Any],
    monthly_rows: list[dict[str, Any]],
) -> ProductAnalyticsResponse:
    """Build a validated dense analytics response from sparse repository aggregates."""
    if not 1 <= months <= 24:
        raise ValueError("months must be between 1 and 24")

    as_of_date = date.fromisoformat(as_of)
    labels = month_labels(as_of, months)
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
        buckets[label] = {
            "units": _number(row.get("units"), "units"),
            "order_count": _order_count(row.get("order_count")),
            "revenue_eur": _number(row.get("revenue_eur"), "revenue_eur"),
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
                units=round(float(units), 2),
                order_count=bucket["order_count"],
                revenue_eur=round(float(revenue), 2),
                avg_price_eur=round(float(avg_price), 4) if avg_price is not None else None,
            )
        )

    return ProductAnalyticsResponse(
        product_id=product_id,
        as_of=as_of,
        model_version=model_version,
        window=ProductAnalyticsWindow(
            months=months,
            start=sales_window_start(as_of, months),
            end_exclusive=as_of,
        ),
        snapshot=snapshot,
        sales_series=series,
        data_quality=ProductAnalyticsDataQuality(),
    )
