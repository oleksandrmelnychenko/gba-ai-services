"""Pure unit tests for the composite health-score (no DB)."""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.models import AbcClass, InventoryBand, LifecycleStage, XyzClass
from app.services.health_score import action_label, demand_score, margin_score, score

CFG = get_settings()


def test_best_case_scores_high():
    val, comp = score(InventoryBand.HEALTHY, LifecycleStage.GROWING, CFG.margin_target,
                      XyzClass.X, 0.0, CFG, abc=AbcClass.A)
    assert val >= 95.0
    assert comp["stock"] == 1.0 and comp["trend"] == 1.0 and comp["returns"] == 1.0
    assert comp["abc"] == 1.0


def test_dead_case_scores_low():
    val, _ = score(InventoryBand.DEAD, LifecycleStage.DEAD, 0.0, XyzClass.Z, 0.0, CFG)
    assert val < 40.0


def test_unknown_margin_is_neutral():
    val_none, comp_none = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, None,
                                XyzClass.Y, 0.0, CFG)
    assert comp_none["margin"] == 0.5
    val_zero, _ = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, 0.0, XyzClass.Y, 0.0, CFG)
    assert val_none > val_zero  # unknown (neutral) beats a known zero margin


def test_returns_penalty_monotonic():
    low, _ = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, CFG.margin_target, XyzClass.X, 0.0, CFG)
    high, _ = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, CFG.margin_target, XyzClass.X,
                    CFG.return_rate_cap, CFG)
    assert high < low


def test_abc_component_boosts_commercially_relevant_skus():
    a_score, a_comp = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, CFG.margin_target,
                            XyzClass.X, 0.0, CFG, abc=AbcClass.A)
    c_score, c_comp = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, CFG.margin_target,
                            XyzClass.X, 0.0, CFG, abc=AbcClass.C)

    assert a_comp["abc"] == 1.0
    assert c_comp["abc"] == 0.2
    assert a_score > c_score


def test_demand_score_prioritizes_abc_and_stable_demand():
    strong, comp = demand_score(InventoryBand.HEALTHY, LifecycleStage.GROWING, XyzClass.X,
                                AbcClass.A, CFG)
    weak, _ = demand_score(InventoryBand.ORDER_TO_DEMAND, LifecycleStage.DECLINING, XyzClass.Z,
                           AbcClass.C, CFG)

    assert comp["abc"] == 1.0
    assert strong > weak


def test_margin_score_penalizes_returns_and_negative_margin():
    good, _ = margin_score(CFG.margin_target, 0.0, CFG, abc=AbcClass.A)
    bad, _ = margin_score(-0.1, CFG.return_rate_cap, CFG, abc=AbcClass.C)

    assert good > bad


def test_action_label_maps_operational_cases():
    assert action_label(InventoryBand.UNDERSTOCK, LifecycleStage.MATURE, AbcClass.A,
                        CFG.margin_target, 0.0, 80.0, 80.0, CFG)[0] == "reorder_check"
    assert action_label(InventoryBand.OVERSTOCK, LifecycleStage.MATURE, AbcClass.B,
                        CFG.margin_target, 0.0, 60.0, 80.0, CFG)[0] == "discount_or_redistribute"
    assert action_label(InventoryBand.HEALTHY, LifecycleStage.MATURE, AbcClass.A,
                        -0.1, 0.0, 80.0, 10.0, CFG)[0] == "fix_margin"
    assert action_label(InventoryBand.ORDER_TO_DEMAND, LifecycleStage.GROWING, AbcClass.A,
                        None, 0.0, 85.0, 60.0, CFG)[0] == "to_order_candidate"


def test_keep_push_requires_known_healthy_margin():
    unknown = action_label(InventoryBand.HEALTHY, LifecycleStage.GROWING, AbcClass.A,
                           None, 0.0, 90.0, 65.0, CFG)
    known = action_label(InventoryBand.HEALTHY, LifecycleStage.GROWING, AbcClass.A,
                         CFG.margin_target, 0.0, 90.0, 90.0, CFG)

    assert unknown[0] == "margin_review"
    assert "unknown_margin" in unknown[1]
    assert known[0] == "keep_push"


def test_zero_stock_is_graded_by_demand_trend_not_a_flat_default():
    """order_to_demand (zero on-hand stock) must NOT collapse to a flat 0.7 stock sub-score:
    a still-growing to-order line scores well above a fading (declining) one."""
    _, growing = score(InventoryBand.ORDER_TO_DEMAND, LifecycleStage.GROWING, CFG.margin_target,
                       XyzClass.X, 0.0, CFG)
    _, declining = score(InventoryBand.ORDER_TO_DEMAND, LifecycleStage.DECLINING, CFG.margin_target,
                         XyzClass.X, 0.0, CFG)
    # the flattering 0.7 default is gone, and the two trends are clearly separated
    assert growing["stock"] != 0.7 or declining["stock"] != 0.7
    assert growing["stock"] > declining["stock"]
    assert declining["stock"] <= 0.3  # fading + nothing on hand is not "healthy"


def test_zero_stock_declining_scores_below_an_in_stock_healthy_line():
    declining_zero, _ = score(InventoryBand.ORDER_TO_DEMAND, LifecycleStage.DECLINING, None,
                              XyzClass.Z, 0.0, CFG)
    healthy_stocked, _ = score(InventoryBand.HEALTHY, LifecycleStage.MATURE, None, XyzClass.Z, 0.0, CFG)
    assert declining_zero < healthy_stocked


def test_in_stock_bands_unchanged_by_the_zero_stock_fix():
    # the fix only re-grades order_to_demand; the other bands keep their fixed sub-scores
    for band, expected in [(InventoryBand.HEALTHY, 1.0), (InventoryBand.OVERSTOCK, 0.4),
                           (InventoryBand.SLOW, 0.3), (InventoryBand.DEAD, 0.0)]:
        _, comp = score(band, LifecycleStage.MATURE, CFG.margin_target, XyzClass.X, 0.0, CFG)
        assert comp["stock"] == expected
