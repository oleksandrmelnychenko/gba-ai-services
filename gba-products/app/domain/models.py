"""Domain vocabulary for product intelligence. Read-only service — no persisted state."""
from __future__ import annotations

from enum import StrEnum


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
