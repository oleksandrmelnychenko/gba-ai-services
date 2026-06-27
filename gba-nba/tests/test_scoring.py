"""Scoring tests — lock the real-data calibration (2026-06) and curve bounds."""
from __future__ import annotations

import pytest

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


def test_score_task_priority_delegates_to_the_model():
    # priority must come from the trained propensity model (100 * p_outcome), NOT the legacy blend.
    from app.ml.score_task import score_task
    feat = {"days_past_terms": 40, "overdue_amount": 8000, "max_overdue_days": 40, "debt_lines": 3,
            "monetary": 50000, "recency_days": 5, "order_count": 120}
    prio, p_out, ev, ev_score = scoring.score_task_priority("debt_followup", feat)

    # equals score_task() on the SAME features mapped to the model FEATURE names (the integration contract)
    ref = score_task({"task_type": "debt_followup", "sig_overdue_amount": 8000,
                      "sig_days_past_terms": 40, "sig_max_overdue_days": 40, "sig_debt_lines": 3,
                      "monetary": 50000, "recency_days": 5, "order_count": 120})
    assert prio == ref["priority"] == round(100.0 * p_out, 2)
    assert p_out == ref["p_outcome"]
    assert ev == ref["expected_value"]
    # ev_score is computed from the UNROUNDED probability inside score_task, so it matches the
    # reference exactly but only approximates the reconstruction from the rounded p_outcome.
    assert ev_score == ref["ev_score"]
    assert ev_score == pytest.approx(p_out * ev, abs=0.5)
    # debt E[value] is the overdue amount (cash at stake), not annual turnover
    assert ev == 8000.0


def test_score_task_priority_maps_short_signal_keys_per_type():
    # each type's short raw-signal keys land on the right model FEATURE (mapping is the load-bearing
    # part: a wrong/missing key silently degrades the score).
    from app.ml.score_task import score_task
    cases = {
        "reorder_due": ({"elapsed_days": 30, "cycle_days": 14, "overdue_ratio": 2.14, "n_orders": 9,
                         "monetary": 80000, "recency_days": 2, "order_count": 300},
                        {"sig_elapsed_days": 30, "sig_cycle_days": 14, "sig_overdue_ratio": 2.14,
                         "sig_n_orders": 9, "monetary": 80000, "recency_days": 2, "order_count": 300}),
        "churn_winback": ({"drop_ratio": 0.1, "silence_days": 90, "recent_orders": 1, "prior_orders": 12,
                           "monetary": 30000, "recency_days": 90, "order_count": 60},
                          {"sig_drop_ratio": 0.1, "sig_silence_days": 90, "sig_recent_orders": 1,
                           "sig_prior_orders": 12, "monetary": 30000, "recency_days": 90, "order_count": 60}),
        "cross_sell": ({"top_score": 0.8, "candidates": 5, "monetary": 40000, "recency_days": 3,
                        "order_count": 150},
                       {"sig_top_score": 0.8, "sig_reco_candidates": 5, "monetary": 40000,
                        "recency_days": 3, "order_count": 150}),
    }
    for tt, (short, mapped) in cases.items():
        prio, p_out, _, _ = scoring.score_task_priority(tt, short)
        ref = score_task({"task_type": tt, **mapped})
        assert abs(prio - ref["priority"]) < 1e-9, tt
        assert abs(p_out - ref["p_outcome"]) < 1e-9, tt


def test_score_task_priority_maps_missing_recency_to_sentinel():
    # recency_days None (client with no order in the trailing window) -> 9999, the dataset sentinel.
    from app.ml.score_task import score_task
    feat = {"top_score": 0.5, "candidates": 3, "monetary": 5000,
            "recency_days": None, "order_count": 4}
    prio, _, _, _ = scoring.score_task_priority("cross_sell", feat)
    ref = score_task({"task_type": "cross_sell", "sig_top_score": 0.5, "sig_reco_candidates": 3,
                      "monetary": 5000, "recency_days": 9999, "order_count": 4})
    assert prio == ref["priority"]
