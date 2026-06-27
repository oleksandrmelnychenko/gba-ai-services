"""Unit tests for dashboard chart data — mocked repo/build_plan, no DB/Redis."""
from __future__ import annotations

from app.domain.models import (
    DemandForecast,
    InventoryPosition,
    ProducerPurchasePlan,
    ReorderSuggestion,
    Urgency,
)
from app.services.replenishment import policy


def _sug(product_id: int, qty: float, urgency: Urgency, cover: float,
         on_hand: float = 0.0, rop: float = 10.0, mean_daily: float = 1.0) -> ReorderSuggestion:
    fc = DemandForecast(product_id=product_id, mean_daily=mean_daily, std_daily=0.0, method="t",
                        horizon_days=30, forecast_units=mean_daily * 30)
    inv = InventoryPosition(product_id=product_id, on_hand=on_hand, reserved=0, on_order=0,
                            available=on_hand, position=on_hand)
    return ReorderSuggestion(
        product_id=product_id, producer_id=99, suggested_qty=qty,
        reorder_point=rop, safety_stock=2.0, days_of_cover=cover,
        urgency=urgency, forecast=fc, inventory=inv, reason="t",
    )


def _plan(items: list[ReorderSuggestion]) -> ProducerPurchasePlan:
    return ProducerPurchasePlan(producer_id=99, lead_time_days=5.0,
                                items=items, item_count=len(items))


def test_cover_bucket_edges():
    assert policy._cover_bucket(-1) == "<0"
    assert policy._cover_bucket(0) == "0-7"
    assert policy._cover_bucket(7) == "0-7"
    assert policy._cover_bucket(8) == "8-30"
    assert policy._cover_bucket(30) == "8-30"
    assert policy._cover_bucket(31) == "31-90"
    assert policy._cover_bucket(90) == "31-90"
    assert policy._cover_bucket(91) == "90+"
    assert policy._cover_bucket(99999.0) == "90+"


def test_next_month_wraps_december():
    from datetime import date
    assert policy._next_month(date(2026, 6, 15)).isoformat() == "2026-07-01"
    assert policy._next_month(date(2026, 12, 3)).isoformat() == "2027-01-01"


def test_build_charts_producer_uses_full_distribution(monkeypatch):
    items = [
        _sug(1, 9.0, Urgency.CRITICAL, 0.0, on_hand=0, rop=50.0, mean_daily=2.0),
        _sug(2, 4.0, Urgency.HIGH, 3.0, on_hand=5, rop=20.0, mean_daily=1.0),
        _sug(3, 0.0, Urgency.NONE, 99999.0, on_hand=1000, rop=10.0, mean_daily=0.5),
        _sug(4, 6.0, Urgency.NORMAL, 45.0, on_hand=30, rop=12.0, mean_daily=1.0),
    ]
    monkeypatch.setattr(policy, "build_plan",
                        lambda pid, as_of, only_needed=True: _plan(items))
    # demand_series bulk fetch mocked: product 1 has 2 months of history
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk",
                        lambda ids, as_of, days: {
                            1: [{"d": "2026-04-10", "units": 3.0},
                                {"d": "2026-04-22", "units": 2.0},
                                {"d": "2026-05-05", "units": 4.0}],
                        })

    charts = policy.build_charts(367, "2026-06-15", top_n=15)

    # urgency_mix: every bucket present, critical-first, counts correct
    assert [b.urgency for b in charts.urgency_mix] == [
        Urgency.CRITICAL, Urgency.HIGH, Urgency.NORMAL, Urgency.NONE]
    assert {b.urgency: b.count for b in charts.urgency_mix} == {
        Urgency.CRITICAL: 1, Urgency.HIGH: 1, Urgency.NORMAL: 1, Urgency.NONE: 1}

    # cover hist: 5 fixed buckets, every one present
    assert [b.bucket for b in charts.days_of_cover_hist] == ["<0", "0-7", "8-30", "31-90", "90+"]
    hist = {b.bucket: b.count for b in charts.days_of_cover_hist}
    assert hist == {"<0": 0, "0-7": 2, "8-30": 0, "31-90": 1, "90+": 1}

    # top_items: urgency then suggested_qty; carries on_hand + reorder_point
    assert [t.product_id for t in charts.top_items] == [1, 2, 4, 3]
    assert charts.top_items[0].on_hand == 0
    assert charts.top_items[0].reorder_point == 50.0
    assert charts.top_items[0].urgency == Urgency.CRITICAL

    # demand_series: history (sorted, monthly) THEN one is_forecast point
    s1 = next(s for s in charts.demand_series if s.product_id == 1)
    assert [p.period for p in s1.points] == ["2026-04", "2026-05", "2026-07"]
    assert [p.units for p in s1.points] == [5.0, 4.0, 60.0]  # forecast = mean_daily 2.0 * 30
    assert [p.is_forecast for p in s1.points] == [False, False, True]


def test_build_charts_cart_wide_dedups_top_items(monkeypatch):
    from app.domain.models import CartReplenishmentPlan
    items = [
        _sug(1, 9.0, Urgency.CRITICAL, 0.0),
        _sug(1, 5.0, Urgency.HIGH, 2.0),
        _sug(2, 7.0, Urgency.HIGH, 1.0),
    ]
    monkeypatch.setattr(policy, "build_cart_plan",
                        lambda as_of, only_needed=True, limit=200:
                        CartReplenishmentPlan(items=items, item_count=len(items)))
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk", lambda ids, as_of, days: {})

    charts = policy.build_charts(None, "2026-06-15", top_n=15)

    assert [t.product_id for t in charts.top_items] == [1, 2]
    series_ids = [s.product_id for s in charts.demand_series]
    assert len(series_ids) == len(set(series_ids))


def test_build_charts_demand_series_capped_to_few(monkeypatch):
    items = [_sug(i, float(20 - i), Urgency.HIGH, 1.0) for i in range(1, 11)]
    monkeypatch.setattr(policy, "build_plan",
                        lambda pid, as_of, only_needed=True: _plan(items))
    captured = {}

    def _bulk(ids, as_of, days):
        captured["ids"] = ids
        return {}
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk", _bulk)

    charts = policy.build_charts(367, "2026-06-15", top_n=15)
    # top_n=15 but demand_series capped at _DEMAND_SERIES_MAX (5)
    assert len(charts.demand_series) == policy._DEMAND_SERIES_MAX
    assert captured["ids"] == [1, 2, 3, 4, 5]
    # each capped series still has the forecast point even with empty history
    for s in charts.demand_series:
        assert s.points[-1].is_forecast is True
        assert len(s.points) == 1


def test_build_charts_cart_wide_uses_cart_plan(monkeypatch):
    seen = {}

    def _cart(as_of, only_needed=True, limit=200):
        seen["limit"] = limit
        from app.domain.models import CartReplenishmentPlan
        return CartReplenishmentPlan(
            items=[_sug(1, 5.0, Urgency.HIGH, 2.0)], item_count=1, as_of_date=as_of)
    monkeypatch.setattr(policy, "build_cart_plan", _cart)
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk",
                        lambda ids, as_of, days: {})

    charts = policy.build_charts(None, "2026-06-15", top_n=5)
    assert charts.producer_id is None
    assert seen["limit"] == -1  # unlimited cart for full chart distribution
    assert charts.top_items[0].product_id == 1
