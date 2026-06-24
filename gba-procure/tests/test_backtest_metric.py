"""Unit tests for the magnitude-aware overstock metric — mocked plan + demand, no DB.

Guards the fix: overstock must measure EXCESS UNITS beyond realized forward demand
(sum(max(0, position + suggested_qty - demand))), not the old saturating binary flag,
so it can trade off against the unit/event stockout signal.
"""
from __future__ import annotations

import inspect
from datetime import date

import pytest

from app.domain.models import (
    DemandForecast,
    InventoryPosition,
    ProducerPurchasePlan,
    ReorderSuggestion,
    Urgency,
)
from app.services.eval import backtest as bt


def _sug(product_id: int, position: float, qty: float) -> ReorderSuggestion:
    fc = DemandForecast(product_id=product_id, mean_daily=1.0, std_daily=0.0, method="t",
                        horizon_days=30, forecast_units=30.0)
    inv = InventoryPosition(product_id=product_id, on_hand=position, reserved=0, on_order=0,
                            available=position, position=position)
    return ReorderSuggestion(
        product_id=product_id, producer_id=1, suggested_qty=qty,
        reorder_point=0.0, safety_stock=0.0, days_of_cover=1.0,
        urgency=Urgency.NORMAL, forecast=fc, inventory=inv, reason="t",
    )


def _patch(monkeypatch, items, demand):
    plan = ProducerPurchasePlan(producer_id=1, lead_time_days=10.0, lead_time_std_days=0.0,
                                items=items, item_count=len(items), as_of_date="2026-01-01")
    monkeypatch.setattr(bt.policy, "build_plan", lambda *a, **k: plan)
    monkeypatch.setattr(bt, "_actual_demand", lambda *a, **k: demand)


def test_overstock_units_counts_excess_beyond_demand(monkeypatch):
    # position 5 + order 20 = 25 post-order; demand 10 -> excess = 15 units.
    _patch(monkeypatch, [_sug(1, position=5, qty=20)], {1: 10.0})
    r = bt.backtest_producer(1, "2026-01-01", 30)
    assert r.overstock_units == 15.0
    assert r.products == 1
    assert r.overstock_units_per_product == 15.0
    assert r.overstock_units_per_demand == 1.5
    assert r.demand_units == 10.0


def test_no_excess_when_post_order_below_demand(monkeypatch):
    # position 2 + order 3 = 5 post-order; demand 10 -> short, NOT overstock; it's a stockout.
    _patch(monkeypatch, [_sug(1, position=2, qty=3)], {1: 10.0})
    r = bt.backtest_producer(1, "2026-01-01", 30)
    assert r.overstock_units == 0.0
    assert r.stockout_rate == 1.0
    assert r.fill_rate == 0.5
    assert r.demand_units == 10.0


def test_zero_demand_product_is_pure_excess(monkeypatch):
    # no forward demand at all -> the whole post-order position is excess.
    _patch(monkeypatch, [_sug(1, position=4, qty=6)], {})
    r = bt.backtest_producer(1, "2026-01-01", 30)
    assert r.overstock_units == 10.0
    assert r.products == 0
    assert r.overstock_units_per_demand == 0.0


def test_overstock_units_grows_with_order_size(monkeypatch):
    # The metric is MAGNITUDE-aware: a bigger order over the same demand => more excess,
    # unlike the saturating binary legacy flag.
    _patch(monkeypatch, [_sug(1, position=0, qty=20)], {1: 10.0})
    small = bt.backtest_producer(1, "2026-01-01", 30).overstock_units
    _patch(monkeypatch, [_sug(1, position=0, qty=200)], {1: 10.0})
    big = bt.backtest_producer(1, "2026-01-01", 30).overstock_units
    assert big > small
    assert small == 10.0 and big == 190.0


def test_actual_demand_uses_valid_sale_filter_not_order_deleted():
    src = inspect.getsource(bt._actual_demand)
    assert "oi.IsValidForCurrentSale = 1" in src
    assert "oi.ProductID <> 25422404" in src
    assert "o.Deleted" not in src


def test_backtest_rejects_incomplete_future_window_before_policy(monkeypatch):
    monkeypatch.setattr(bt, "_today", lambda: date(2026, 1, 15))

    def boom(*a, **k):
        raise AssertionError("policy must not run for an incomplete backtest window")

    monkeypatch.setattr(bt.policy, "build_plan", boom)
    with pytest.raises(ValueError, match="complete_future_demand_window"):
        bt.backtest_producer(1, "2026-01-01", 30)


def test_backtest_rejects_invalid_window():
    with pytest.raises(ValueError, match="eval_window_days"):
        bt.backtest_producer(1, "2026-01-01", 0)
