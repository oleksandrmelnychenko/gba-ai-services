from __future__ import annotations

from scripts import procure_backtest_sweep as sweep


def test_gate_accepts_summary_inside_thresholds():
    summary = sweep._empty_summary()
    summary.update({
        "n_pairs": 3,
        "products": 42,
        "critical_share": 0.2,
        "economic_cost_eur_per_product": 12.5,
        "stockout_rate": 0.1,
    })
    gate = {
        "min_n_pairs": 1,
        "min_products": 1,
        "max_critical_share": 0.5,
        "max_economic_cost_eur_per_product": 50.0,
        "max_stockout_rate": 0.2,
    }

    report = sweep.evaluate_gate(summary, gate)

    assert report["ok"]
    assert all(check["ok"] for check in report["checks"])


def test_gate_rejects_critical_or_economic_regression():
    summary = sweep._empty_summary()
    summary.update({
        "n_pairs": 3,
        "products": 42,
        "critical_share": 0.9,
        "economic_cost_eur_per_product": 500.0,
        "stockout_rate": 0.1,
    })
    gate = {
        "max_critical_share": 0.5,
        "max_economic_cost_eur_per_product": 50.0,
    }

    report = sweep.evaluate_gate(summary, gate)

    assert not report["ok"]
    failed = {check["name"] for check in report["checks"] if not check["ok"]}
    assert failed == {"max_critical_share", "max_economic_cost_eur_per_product"}
