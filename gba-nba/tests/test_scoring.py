"""Scoring tests — lock the real-data calibration (2026-06) and curve bounds."""
from __future__ import annotations

from app.domain.models import Urgency
from app.services import scoring


def test_scoring_knobs_are_env_tunable(monkeypatch):
    # the weights/saturation are read from config -> A/B-tunable without redeploy
    from app.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "value_saturation", 1000.0)
    assert scoring.value_from_monetary(1000) == 0.5          # 1000/(1000+1000)
    monkeypatch.setattr(s, "w_urgency", 1.0)
    monkeypatch.setattr(s, "w_value", 0.0)
    monkeypatch.setattr(s, "w_confidence", 0.0)
    assert scoring.priority(0.5, 0.9, 0.9) == 50.0           # only urgency counts now


def test_value_curve_spreads_real_distribution():
    # calibrated to active-manager client annual monetary: p50=894, p75=5577, p90=22489
    p50 = scoring.value_from_monetary(894)
    p75 = scoring.value_from_monetary(5577)
    p90 = scoring.value_from_monetary(22489)
    assert 0.10 < p50 < 0.20      # median buyer is low but non-zero (old 50000 pinned it ~0.017)
    assert 0.45 < p75 < 0.55      # p75 lands near mid-scale
    assert 0.75 < p90 < 0.85      # top buyers approach the top of the curve
    assert p50 < p75 < p90        # monotonic
    assert scoring.value_from_monetary(0) == 0.0


def test_debt_urgency_any_overdue_is_at_least_high():
    assert scoring.debt_urgency(0) == 0.0
    assert scoring.debt_urgency(-5) == 0.0
    assert scoring.debt_urgency(1) >= 0.6                       # any overdue -> at least HIGH
    assert scoring.urgency_band(scoring.debt_urgency(1)) in (Urgency.HIGH, Urgency.CRITICAL)
    assert scoring.urgency_band(scoring.debt_urgency(120)) == Urgency.CRITICAL
    assert scoring.debt_urgency(1) < scoring.debt_urgency(120) <= 1.0


def test_priority_is_bounded_0_100():
    assert scoring.priority(0, 0, 0) == 0.0
    assert scoring.priority(1, 1, 1) == 100.0
    assert 0.0 <= scoring.priority(0.5, 0.3, 0.7) <= 100.0


def test_reorder_urgency_pyramid():
    # just-due is a routine NORMAL nudge; reorder caps at HIGH (never CRITICAL) so a routine
    # restock can't outrank a CRITICAL debt — the signal's own 3x ceiling already means "abandoned".
    assert scoring.urgency_band(scoring.reorder_urgency(30, 30)) == Urgency.NORMAL   # 1x
    assert scoring.urgency_band(scoring.reorder_urgency(75, 30)) == Urgency.HIGH     # 2.5x
    assert scoring.urgency_band(scoring.reorder_urgency(90, 30)) == Urgency.HIGH     # 3x = top of HIGH
    assert scoring.urgency_band(scoring.reorder_urgency(90, 30)) != Urgency.CRITICAL
    assert scoring.reorder_urgency(30, 30) < scoring.reorder_urgency(90, 30) <= 0.8


def test_urgency_bands():
    assert scoring.urgency_band(0.9) == Urgency.CRITICAL
    assert scoring.urgency_band(0.7) == Urgency.HIGH
    assert scoring.urgency_band(0.4) == Urgency.NORMAL
    assert scoring.urgency_band(0.1) == Urgency.LOW
