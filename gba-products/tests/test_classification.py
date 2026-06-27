"""Pure unit tests for ABC/XYZ/lifecycle classification (no DB)."""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.models import AbcClass, LifecycleStage, XyzClass
from app.services import classification as cl

CFG = get_settings()


def test_month_labels_trailing_oldest_first():
    assert cl.month_labels("2026-06-24", 3) == ["2026-04", "2026-05", "2026-06"]


def test_series_from_fills_zeros():
    labels = cl.month_labels("2026-06-24", 3)
    assert cl.series_from({"2026-06": 5.0}, labels) == [0.0, 0.0, 5.0]


def test_demand_cv_stable_vs_intermittent():
    assert cl.demand_cv([5, 5, 5]) == 0.0
    assert cl.demand_cv([0, 0, 10]) > 1.0          # one-spike intermittent => high CV
    assert cl.demand_cv([]) == 0.0


def test_interval_demand_cv():
    # Perfectly regular cadence (every 2nd month) => zero interval variability.
    assert cl.interval_demand_cv([1, 0, 1, 0, 1, 0, 1]) == 0.0
    # Irregular gaps (1 then 4 months apart) => positive variability.
    assert cl.interval_demand_cv([1, 1, 0, 0, 0, 1]) > 0.0
    # Fewer than two demand months can't establish a cadence => +inf sentinel (=> Z).
    assert cl.interval_demand_cv([0, 0, 5, 0, 0]) == float("inf")
    assert cl.interval_demand_cv([0, 0, 0]) == float("inf")


def test_demand_variability_dispatch():
    cfg_iv = CFG.model_copy(update={"xyz_method": "interval"})
    cfg_cv = CFG.model_copy(update={"xyz_method": "cv"})
    s = [1, 0, 1, 0, 1, 0]
    assert cl.demand_variability(s, cfg_iv) == cl.interval_demand_cv(s)
    assert cl.demand_variability(s, cfg_cv) == cl.demand_cv(s)


def test_xyz_cuts():
    # Cuts are configurable; default x<0.3, y<0.6.
    assert cl.xyz_classify(0.2, CFG) is XyzClass.X
    assert cl.xyz_classify(0.5, CFG) is XyzClass.Y
    assert cl.xyz_classify(2.0, CFG) is XyzClass.Z
    assert cl.xyz_classify(float("inf"), CFG) is XyzClass.Z   # single/no-sale sentinel


def test_abc_cumulative_share():
    assert cl.abc_classify(0.50, CFG) is AbcClass.A
    assert cl.abc_classify(0.90, CFG) is AbcClass.B
    assert cl.abc_classify(0.99, CFG) is AbcClass.C


def test_portfolio_abc_zero_revenue_is_unknown():
    from app.services import portfolio

    rows = [{"product_id": 1, "revenue_eur": 0.0}, {"product_id": 2, "revenue_eur": 0.0}]

    portfolio._assign_abc(rows, CFG)

    assert {r["abc"] for r in rows} == {"unknown"}


def test_lifecycle_dead_when_no_recent_sale():
    assert cl.lifecycle_classify(400, 0, 0, sold_in_dead_window=False, cfg=CFG) is LifecycleStage.DEAD


def test_lifecycle_new_by_age():
    assert cl.lifecycle_classify(30, 5, 0, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.NEW


def test_lifecycle_growing_declining_mature():
    # Recent activity present: trend factors decide.
    assert cl.lifecycle_classify(400, 12, 5, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.GROWING
    assert cl.lifecycle_classify(400, 2, 10, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.DECLINING
    assert cl.lifecycle_classify(400, 10, 10, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.MATURE


def test_lifecycle_dormant_split_not_blanket_declining():
    # No recent demand (recent=0) but sold earlier in the window. The OLD code dumped this whole
    # population into DECLINING; now a short lull is MATURE and only a long fade is DECLINING.
    short_lull = cl.lifecycle_classify(400, 0, 8, sold_in_dead_window=True, cfg=CFG,
                                       months_since_last=4)
    long_fade = cl.lifecycle_classify(400, 0, 8, sold_in_dead_window=True, cfg=CFG,
                                      months_since_last=11)
    assert short_lull is LifecycleStage.MATURE
    assert long_fade is LifecycleStage.DECLINING
    # recent=prior=0 catch-all with a recent-ish last sale is MATURE, not DECLINING.
    assert cl.lifecycle_classify(400, 0, 0, sold_in_dead_window=True, cfg=CFG,
                                 months_since_last=5) is LifecycleStage.MATURE


def test_lifecycle_legacy_fallback_without_dormancy():
    # When months_since_last is omitted, the legacy recent/prior rule applies (back-compat).
    assert cl.lifecycle_classify(400, 0, 0, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.DECLINING
    assert cl.lifecycle_classify(400, 3, 0, sold_in_dead_window=True, cfg=CFG) is LifecycleStage.GROWING


def test_lifecycle_from_series():
    # 12-month series; sales only in the oldest months, last sale 9 months back (long fade).
    early_only = [4, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert cl.months_since_last_sale(early_only) == 9
    assert cl.lifecycle_from_series(early_only, 350, True, CFG) is LifecycleStage.DECLINING
    # Same shape but last sale only 8 months back => MATURE (lull, not yet a fade).
    recent_lull = [4, 3, 2, 5, 0, 0, 0, 0, 0, 0, 0, 0]
    assert cl.months_since_last_sale(recent_lull) == 8
    assert cl.lifecycle_from_series(recent_lull, 350, True, CFG) is LifecycleStage.MATURE
    # Steady recent demand => GROWING/MATURE, never DECLINING.
    steady = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    assert cl.lifecycle_from_series(steady, 350, True, CFG) is LifecycleStage.MATURE
    # Never sold in dead window => DEAD regardless of series.
    assert cl.lifecycle_from_series([0] * 12, 350, False, CFG) is LifecycleStage.DEAD


def test_months_since_last_sale():
    assert cl.months_since_last_sale([1, 0, 0]) == 2
    assert cl.months_since_last_sale([0, 0, 1]) == 0
    assert cl.months_since_last_sale([0, 0, 0]) is None


def test_split_recent_prior():
    assert cl.split_recent_prior([1, 2, 3, 4, 5, 6], 3) == (15, 6)
