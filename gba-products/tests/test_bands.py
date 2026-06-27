"""Pure unit tests for the inventory-health band classifier (no DB)."""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.models import InventoryBand
from app.services.stock_health import classify_band

CFG = get_settings()


def _daily_for_annual(units_per_year: float) -> float:
    return units_per_year / 365.0


def test_no_stock_is_order_to_demand():
    # GBA is order-to-demand: nothing on hand is normal, not a stockout — regardless of demand
    assert classify_band(0, 0.0, sold_in_dead_window=False, cfg=CFG) is InventoryBand.ORDER_TO_DEMAND
    band = classify_band(0, _daily_for_annual(50), sold_in_dead_window=True, cfg=CFG)
    assert band is InventoryBand.ORDER_TO_DEMAND


def test_dead_when_stock_but_no_sale_in_dead_window():
    assert classify_band(10, 0.0, sold_in_dead_window=False, cfg=CFG) is InventoryBand.DEAD


def test_slow_when_annual_units_below_cutoff():
    rate = _daily_for_annual(CFG.slow_max_annual_units)  # exactly at the cutoff -> slow
    assert classify_band(10, rate, sold_in_dead_window=True, cfg=CFG) is InventoryBand.SLOW


def test_overstock_when_cover_exceeds_threshold():
    rate = _daily_for_annual(CFG.slow_max_annual_units + 50)  # above slow, real demand
    qty = rate * (CFG.cover_overstock_days + 30)              # cover beyond overstock threshold
    assert classify_band(qty, rate, sold_in_dead_window=True, cfg=CFG) is InventoryBand.OVERSTOCK


def test_understock_when_cover_below_threshold():
    rate = _daily_for_annual(CFG.slow_max_annual_units + 50)
    qty = rate * (CFG.cover_understock_days - 1)
    assert classify_band(qty, rate, sold_in_dead_window=True, cfg=CFG) is InventoryBand.UNDERSTOCK


def test_healthy_within_target_band():
    rate = _daily_for_annual(CFG.slow_max_annual_units + 50)
    qty = rate * (CFG.cover_understock_days + CFG.cover_overstock_days) / 2  # mid-band cover
    assert classify_band(qty, rate, sold_in_dead_window=True, cfg=CFG) is InventoryBand.HEALTHY
