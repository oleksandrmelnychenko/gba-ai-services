from __future__ import annotations

from app.services.health_backtest import evaluate_health_snapshot, spearman
from scripts.product_health_backtest import _check_regression


def test_spearman_handles_ties_and_monotonicity():
    assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-12
    assert abs(spearman([1, 2, 3, 4], [40, 30, 20, 10]) + 1.0) < 1e-12
    assert spearman([1, 1, 2, 2], [10, 20, 30, 40]) is not None


def test_evaluate_health_snapshot_reports_lift_and_deciles():
    rows = [
        {"product_id": 1, "health": 10.0, "band": "dead", "lifecycle": "dead", "abc": "C",
         "unit_cost_eur": 5.0, "demand_score": 10.0, "margin_score": 10.0,
         "action_label": "dead_stock_review"},
        {"product_id": 2, "health": 30.0, "band": "slow", "lifecycle": "declining", "abc": "C",
         "unit_cost_eur": 5.0, "demand_score": 30.0, "margin_score": 20.0,
         "action_label": "slow_mover_review"},
        {"product_id": 3, "health": 70.0, "band": "healthy", "lifecycle": "mature", "abc": "B",
         "unit_cost_eur": 5.0, "demand_score": 70.0, "margin_score": 80.0, "action_label": "keep_push"},
        {"product_id": 4, "health": 90.0, "band": "healthy", "lifecycle": "growing", "abc": "A",
         "unit_cost_eur": 5.0, "demand_score": 90.0, "margin_score": 90.0, "action_label": "keep_push"},
    ]
    outcomes = {
        1: {"future_units": 1, "future_revenue_eur": 10, "future_returned_units": 0, "future_margin_eur": 5},
        2: {"future_units": 2, "future_revenue_eur": 20, "future_returned_units": 0, "future_margin_eur": 10},
        3: {"future_units": 8, "future_revenue_eur": 80, "future_returned_units": 1, "future_margin_eur": 45},
        4: {
            "future_units": 10,
            "future_revenue_eur": 100,
            "future_returned_units": 0,
            "future_margin_eur": 50,
        },
    }

    report = evaluate_health_snapshot(rows, outcomes, buckets=2, tail_fraction=0.25)

    assert report["n"] == 4
    assert report["correlations"]["spearman_health_to_future_revenue"] == 1.0
    assert report["correlations"]["spearman_demand_score_to_future_revenue"] == 1.0
    assert report["correlations"]["spearman_margin_score_to_future_margin"] == 1.0
    assert report["top_bottom_lift"]["future_revenue_avg_top_vs_bottom"] == 10.0
    assert {r["action_label"] for r in report["by_action"]} == {
        "dead_stock_review", "keep_push", "slow_mover_review"
    }
    assert sum(r["n"] for r in report["by_action"]) == report["n"]
    assert [b["n"] for b in report["deciles_low_to_high_health"]] == [2, 2]
    assert report["deciles_low_to_high_health"][0]["avg_health"] == 20.0


def test_regression_gate_fails_closed_on_missing_metrics():
    current = {
        "summary": {
            "n": 1,
            "correlations": {"spearman_health_to_future_revenue": 0.3},
            "by_action": [],
        }
    }
    baseline = {"summary": {"n": 1, "correlations": {}, "by_action": []}}

    failures = _check_regression(current, baseline, tolerance=0.02)

    assert any("missing in current report" in f for f in failures)
    assert any("missing in baseline report" in f for f in failures)


def test_regression_gate_fails_on_new_metric_regression_and_action_shape():
    correlations = {
        "spearman_health_to_future_revenue": 0.5,
        "spearman_health_to_future_units": 0.5,
        "spearman_health_to_future_margin": 0.5,
        "spearman_demand_score_to_future_revenue": 0.5,
        "spearman_demand_score_to_future_units": 0.5,
        "spearman_margin_score_to_future_margin": 0.5,
    }
    current = {
        "summary": {
            "n": 3,
            "correlations": {**correlations, "spearman_margin_score_to_future_margin": 0.45},
            "by_action": [{"action_label": "monitor", "n": 2}],
        }
    }
    baseline = {
        "summary": {
            "n": 3,
            "correlations": correlations,
            "by_action": [{"action_label": "monitor", "n": 3}],
        }
    }

    failures = _check_regression(current, baseline, tolerance=0.02)

    assert any("spearman_margin_score_to_future_margin regressed" in f for f in failures)
    assert any("current by_action count mismatch" in f for f in failures)
