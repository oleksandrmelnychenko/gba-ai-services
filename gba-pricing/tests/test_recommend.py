"""Pure A+B engine math — no DB/Redis, no service wiring. Exercises the unit helpers and the
assembler against the LOCKED spec (clamp, loss flag, discount cap, confidence bands, no-cost
peer-only path)."""
from __future__ import annotations

import pytest

from app.domain.models import Confidence
from app.services.pricing import recommend as engine


def test_price_floor_applies_target_margin():
    assert engine.price_floor(10.0, 12.0) == pytest.approx(11.2)
    assert engine.price_floor(10.0, 0.0) == 10.0


def test_price_floor_none_when_no_cost():
    assert engine.price_floor(None, 12.0) is None


def test_recommended_clamps_peer_p50_within_floor_baseline():
    rec = engine.recommended_price(floor=11.2, peer_p50=18.5, baseline=20.0)
    assert rec.value == 18.5
    assert rec.rationale == engine.R_PEER_MEDIAN


def test_recommended_never_below_floor():
    rec = engine.recommended_price(floor=15.0, peer_p50=12.0, baseline=20.0)
    assert rec.value == 15.0
    assert rec.rationale == engine.R_MARGIN_FLOOR


def test_recommended_never_above_baseline():
    rec = engine.recommended_price(floor=11.2, peer_p50=25.0, baseline=20.0)
    assert rec.value == 20.0
    assert rec.rationale == engine.R_AT_BASELINE


def test_recommended_floor_above_baseline_is_loss_flag():
    rec = engine.recommended_price(floor=22.0, peer_p50=18.0, baseline=20.0)
    assert rec.value == 22.0
    assert rec.rationale == engine.R_BELOW_MARGIN


def test_recommended_no_cost_peer_only_capped_at_baseline():
    rec = engine.recommended_price(floor=None, peer_p50=18.5, baseline=20.0)
    assert rec.value == 18.5
    assert rec.rationale == engine.R_PEER_MEDIAN
    rec_high = engine.recommended_price(floor=None, peer_p50=25.0, baseline=20.0)
    assert rec_high.value == 20.0
    assert rec_high.rationale == engine.R_AT_BASELINE


def test_recommended_no_peer_falls_back_to_floor_or_baseline():
    rec = engine.recommended_price(floor=11.2, peer_p50=None, baseline=20.0)
    assert rec.value == 11.2
    assert rec.rationale == engine.R_MARGIN_FLOOR
    rec2 = engine.recommended_price(floor=None, peer_p50=None, baseline=20.0)
    assert rec2.value == 20.0
    assert rec2.rationale == engine.R_AT_BASELINE
    rec3 = engine.recommended_price(floor=None, peer_p50=None, baseline=None)
    assert rec3.value is None
    assert rec3.rationale == engine.R_NO_ANCHOR


def test_recommended_no_baseline_returns_none():
    rec = engine.recommended_price(floor=11.2, peer_p50=18.5, baseline=0.0)
    assert rec.value is None
    assert rec.rationale == engine.R_NO_BASELINE
    rec_neg = engine.recommended_price(floor=11.2, peer_p50=18.5, baseline=-5.0)
    assert rec_neg.value is None
    assert rec_neg.rationale == engine.R_NO_BASELINE


def test_discount_from_price_reproduces_engine_lever():
    assert engine.discount_from_price(18.5, 20.0) == pytest.approx(7.5)
    assert engine.discount_from_price(20.0, 20.0) == pytest.approx(0.0)


def test_discount_from_price_guards_bad_denominator():
    assert engine.discount_from_price(18.5, None) is None
    assert engine.discount_from_price(18.5, 0.0) is None
    assert engine.discount_from_price(None, 20.0) is None


def test_cap_discount_at_p75():
    capped, was = engine.cap_discount(20.0, peer_p75=12.0, peer_p90=18.0)
    assert capped == 12.0
    assert was is True


def test_cap_discount_hard_caps_at_p90_when_no_p75():
    capped, was = engine.cap_discount(25.0, peer_p75=None, peer_p90=18.0)
    assert capped == 18.0
    assert was is True


def test_cap_discount_p90_overrides_higher_p75():
    capped, was = engine.cap_discount(25.0, peer_p75=30.0, peer_p90=18.0)
    assert capped == 18.0
    assert was is True


def test_cap_discount_no_cap_when_within_band():
    capped, was = engine.cap_discount(7.5, peer_p75=12.0, peer_p90=18.0)
    assert capped == 7.5
    assert was is False


def test_cap_discount_floors_negative_at_zero():
    capped, was = engine.cap_discount(-3.0, peer_p75=12.0, peer_p90=18.0)
    assert capped == 0.0
    assert was is False


def test_confidence_high_needs_lots_and_peers():
    assert engine.confidence_for(3, 10, has_cost=True, baseline=20.0) == Confidence.HIGH
    assert engine.confidence_for(5, 50, has_cost=True, baseline=20.0) == Confidence.HIGH


def test_confidence_medium_when_below_high_thresholds():
    assert engine.confidence_for(2, 10, has_cost=True, baseline=20.0) == Confidence.MEDIUM
    assert engine.confidence_for(3, 5, has_cost=True, baseline=20.0) == Confidence.MEDIUM


def test_confidence_low_when_no_cost_or_sparse_peers():
    assert engine.confidence_for(9, 99, has_cost=False, baseline=20.0) == Confidence.LOW
    assert engine.confidence_for(9, 2, has_cost=True, baseline=20.0) == Confidence.LOW


def test_confidence_low_when_baseline_missing_or_nonpositive():
    assert engine.confidence_for(9, 99, has_cost=True, baseline=None) == Confidence.LOW
    assert engine.confidence_for(9, 99, has_cost=True, baseline=0.0) == Confidence.LOW
    assert engine.confidence_for(9, 99, has_cost=True, baseline=-1.0) == Confidence.LOW


def test_margin_pct_at_recommended():
    assert engine.margin_pct_at(20.0, 10.0) == pytest.approx(50.0)
    assert engine.margin_pct_at(None, 10.0) is None
    assert engine.margin_pct_at(20.0, None) is None
    assert engine.margin_pct_at(0.0, 10.0) is None


def test_rationale_surfaces_discount_cap_over_peer_median():
    assert engine.rationale_for(engine.R_PEER_MEDIAN, discount_was_capped=True) == \
        engine.R_DISCOUNT_CAP
    assert engine.rationale_for(engine.R_PEER_MEDIAN, discount_was_capped=False) == \
        engine.R_PEER_MEDIAN


def test_rationale_floor_and_loss_flag_take_precedence_over_cap():
    assert engine.rationale_for(engine.R_BELOW_MARGIN, discount_was_capped=True) == \
        engine.R_BELOW_MARGIN
    assert engine.rationale_for(engine.R_MARGIN_FLOOR, discount_was_capped=True) == \
        engine.R_MARGIN_FLOOR
    assert engine.rationale_for(engine.R_AT_BASELINE, discount_was_capped=True) == \
        engine.R_AT_BASELINE


def _build(**over):
    base = dict(
        product_id=7,
        client_agreement_netuid="ca-uid",
        baseline=20.0,
        marked_up=20.0,
        cost={"unit_cost_eur": 10.0, "lot_count": 4, "cost_source": "median_onhand"},
        peer={"p25": 17.0, "p50": 18.5, "p75": 19.5, "n": 12},
        segment={"p75": 12.0, "p90": 18.0, "n": 40},
        target_margin_pct=12.0,
        as_of_date="2026-06-15",
    )
    base.update(over)
    return engine.build_recommendation(**base)


def test_assembler_happy_path_peer_median():
    reco = _build()
    assert reco.baseline_price == 20.0
    assert reco.price_floor == 11.2
    assert reco.unit_cost_eur == 10.0
    assert reco.recommended_price == 18.5
    assert reco.suggested_discount_pct == pytest.approx(7.5)
    assert reco.confidence == Confidence.HIGH
    assert reco.rationale == engine.R_PEER_MEDIAN
    assert reco.margin_pct_at_recommended == pytest.approx(45.95, abs=0.01)
    assert reco.discount_band.min_pct == 18.0
    assert reco.discount_band.target_pct == 18.0
    assert reco.discount_band.max_pct == pytest.approx(44.0)
    assert (
        reco.discount_band.min_pct
        <= reco.discount_band.target_pct
        <= reco.discount_band.max_pct
    )


def test_assembler_loss_flag_when_floor_above_baseline():
    reco = _build(
        cost={"unit_cost_eur": 25.0, "lot_count": 4, "cost_source": "median_onhand"},
    )
    assert reco.price_floor == 28.0
    assert reco.recommended_price == 28.0
    assert reco.rationale == engine.R_BELOW_MARGIN


def test_assembler_discount_cap_binds_rationale():
    reco = _build(
        baseline=20.0,
        marked_up=20.0,
        cost={"unit_cost_eur": 5.0, "lot_count": 4, "cost_source": "median_onhand"},
        peer={"p25": 9.0, "p50": 10.0, "p75": 11.0, "n": 12},
        segment={"p75": 8.0, "p90": 12.0, "n": 40},
    )
    assert reco.recommended_price == 10.0
    assert reco.suggested_discount_pct == 8.0
    assert reco.rationale == engine.R_DISCOUNT_CAP


def test_assembler_no_cost_path_is_peer_only_low_confidence():
    reco = _build(
        cost={"unit_cost_eur": None, "lot_count": 0, "cost_source": "none"},
        peer={"p25": 17.0, "p50": 18.5, "p75": 19.5, "n": 12},
    )
    assert reco.price_floor is None
    assert reco.unit_cost_eur is None
    assert reco.recommended_price == 18.5
    assert reco.margin_pct_at_recommended is None
    assert reco.confidence == Confidence.LOW
    assert reco.discount_band.min_pct == 0.0
