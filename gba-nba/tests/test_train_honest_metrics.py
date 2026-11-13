"""OOT quality has to be reported the way the task is served and the data is clustered."""
import numpy as np
import pandas as pd

from app.ml.train import (
    baseline_score,
    cluster_bootstrap_auc_ci,
    honest_oot_report,
    served_reorder_rows,
)


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for client in range(60):
        for product in range(1 + client % 5):
            n_orders = int(rng.integers(3, 12))
            elapsed = float(rng.integers(20, 200))
            rate = n_orders / elapsed
            rows.append({
                "vintage": "2026-08-01", "task_type": "reorder_due", "client_id": client,
                "sig_overdue_ratio": 1.0 + product * 0.4, "sig_n_orders": n_orders,
                "sig_elapsed_days": elapsed, "sig_silence_days": 0.0,
                "label": int(rng.random() < min(0.9, rate * 4)), "_p": min(0.95, rate * 4),
            })
    for client in range(60, 120):
        silence = float(rng.integers(1, 300))
        rows.append({
            "vintage": "2026-08-01", "task_type": "churn_winback", "client_id": client,
            "sig_overdue_ratio": 0.0, "sig_n_orders": 0.0, "sig_elapsed_days": 0.0,
            "sig_silence_days": silence,
            "label": int(rng.random() < 1.0 / (1.0 + silence / 60.0)), "_p": 1.0 / (1.0 + silence / 60.0),
        })
    return pd.DataFrame(rows)


def test_cluster_interval_contains_the_point_estimate_and_is_deterministic():
    frame = _frame()
    churn = frame[frame["task_type"] == "churn_winback"]
    args = (churn["label"].to_numpy(), churn["_p"].to_numpy(), churn["client_id"].to_numpy())
    first = cluster_bootstrap_auc_ci(*args, n_boot=200)
    assert first == cluster_bootstrap_auc_ci(*args, n_boot=200)
    assert first[0] < first[1]


def test_cluster_interval_is_wider_than_treating_duplicated_rows_as_independent():
    frame = _frame()
    churn = frame[frame["task_type"] == "churn_winback"]
    tiled = pd.concat([churn] * 20, ignore_index=True)
    y, p = tiled["label"].to_numpy(), tiled["_p"].to_numpy()
    by_client = cluster_bootstrap_auc_ci(y, p, tiled["client_id"].to_numpy(), n_boot=200)
    by_row = cluster_bootstrap_auc_ci(y, p, np.arange(len(tiled)), n_boot=200)
    assert (by_client[1] - by_client[0]) > 2 * (by_row[1] - by_row[0])


def test_single_class_fold_has_no_interval():
    assert cluster_bootstrap_auc_ci(np.zeros(5), np.linspace(0, 1, 5), np.arange(5)) is None


def test_baselines_exist_only_for_types_with_a_recency_rule():
    frame = _frame()
    assert baseline_score(frame[frame["task_type"] == "reorder_due"], "reorder_due") is not None
    assert baseline_score(frame[frame["task_type"] == "churn_winback"], "churn_winback") is not None
    assert baseline_score(frame, "debt_followup") is None


def test_served_rows_are_one_most_purchased_product_per_client_and_vintage():
    frame = _frame()
    served = served_reorder_rows(frame)
    reorder = frame[frame["task_type"] == "reorder_due"]
    assert len(served) == reorder["client_id"].nunique()
    expected = reorder.groupby("client_id")["sig_n_orders"].max()
    assert (served.set_index("client_id")["sig_n_orders"].sort_index() == expected).all()


def test_report_names_model_baseline_and_served_rows_per_type():
    report = honest_oot_report(_frame(), "_p")
    for task_type in ("reorder_due", "churn_winback"):
        entry = report[task_type]
        assert entry["auc_ci95"][0] <= entry["auc"] <= entry["auc_ci95"][1]
        assert "baseline_auc" in entry and "auc_minus_baseline" in entry
    assert report["debt_followup"] == {"n": 0, "clients": 0}
    assert report["reorder_due_served_rows"]["n"] == 60
