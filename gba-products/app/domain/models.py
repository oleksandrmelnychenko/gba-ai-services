"""Domain vocabulary for product intelligence. Read-only service — no persisted state."""
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class InventoryBand(StrEnum):
    DEAD = "dead"                  # on-hand stock, zero sales in dead_window_days
    SLOW = "slow"                  # sells, but <= slow_max_annual_units / yr
    OVERSTOCK = "overstock"        # days-of-cover above cover_overstock_days
    HEALTHY = "healthy"            # cover within target band
    UNDERSTOCK = "understock"      # cover below understock threshold (with demand)
    ORDER_TO_DEMAND = "order_to_demand"  # nothing on hand — GBA sells these to order (not a stockout)


class LifecycleStage(StrEnum):
    NEW = "new"
    GROWING = "growing"
    MATURE = "mature"
    DECLINING = "declining"
    DEAD = "dead"


class AbcClass(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class XyzClass(StrEnum):
    X = "X"
    Y = "Y"
    Z = "Z"


class ProductAnalyticsWindow(BaseModel):
    """Exact sales window used by the per-product analytics endpoint."""

    months: int = Field(ge=1, le=24)
    start: date
    end_exclusive: date
    includes_partial_current_month: Literal[True] = True


class MonthlySalesPoint(BaseModel):
    """One calendar-month sales bucket. Missing months are represented by zero values."""

    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    period_start: date
    period_end_exclusive: date
    is_complete: bool
    units: float
    order_count: int
    revenue_eur: float
    avg_price_eur: float | None


class ProductAnalyticsDataQuality(BaseModel):
    """Machine-readable disclosure of the analytics source and its known limits."""

    sales_date_field: Literal["Order.Created"] = "Order.Created"
    sales_validity_filter: Literal["OrderItem.IsValidForCurrentSale = 1"] = (
        "OrderItem.IsValidForCurrentSale = 1"
    )
    sales_window_end: Literal["exclusive"] = "exclusive"
    revenue_basis: Literal["SUM(OrderItem.Qty * OrderItem.PricePerItem); PricePerItem is EUR"] = (
        "SUM(OrderItem.Qty * OrderItem.PricePerItem); PricePerItem is EUR"
    )
    avg_price_basis: Literal["revenue_eur / units (quantity-weighted)"] = (
        "revenue_eur / units (quantity-weighted)"
    )
    zero_months_filled: Literal[True] = True
    stock_is_current: Literal[True] = True
    stock_history_available: Literal[False] = False
    stock_note: str = (
        "Stock fields in snapshot come from the current, potentially cached portfolio snapshot, "
        "including when as_of is historical; no historical stock series is inferred."
    )


class ProductAnalyticsResponse(BaseModel):
    product_id: int
    as_of: date
    model_version: str
    window: ProductAnalyticsWindow
    snapshot: dict[str, Any]
    sales_series: list[MonthlySalesPoint]
    data_quality: ProductAnalyticsDataQuality
