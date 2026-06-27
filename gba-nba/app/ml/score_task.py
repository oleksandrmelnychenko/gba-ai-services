"""Pure scoring head for the NBA inbox.

Loads the calibrated propensity model from app/ml/artifacts/ and exposes score_task(), which
returns {p_outcome, expected_value, priority} for a single task feature dict. The model is loaded
lazily and cached. This module has NO DB / network dependency -- it scores a prepared feature row.

Priority semantics: rank the inbox by p_outcome * expected_value (an expected-EUR ordering).
`priority` is a 0..100 convenience score = 100 * p_outcome. Callers that want the expected-value
ordering should use `p_outcome * expected_value` directly (also returned as `ev_score`).

CAVEAT: p_outcome is NATURAL conversion propensity P(outcome in (T,T+H] | task), NOT manager causal
lift. It ranks who is most likely to convert, not who is most moved by a manager touch.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

ART = Path(__file__).resolve().parent / "artifacts"

SHARED = ["monetary", "recency_days", "order_count"]
SIGNALS = [
    "sig_overdue_amount", "sig_days_past_terms", "sig_max_overdue_days", "sig_debt_lines",
    "sig_elapsed_days", "sig_cycle_days", "sig_overdue_ratio", "sig_n_orders",
    "sig_drop_ratio", "sig_silence_days", "sig_recent_orders", "sig_prior_orders",
    "sig_top_score", "sig_reco_candidates",
]
ONEHOT = ["is_reorder_due", "is_debt_followup", "is_churn_winback", "is_cross_sell"]
FEATURES = SHARED + SIGNALS + ONEHOT

TASK_TYPES = ["reorder_due", "debt_followup", "churn_winback", "cross_sell"]


@lru_cache(maxsize=1)
def _model():
    return joblib.load(ART / "propensity_model.joblib")


@lru_cache(maxsize=1)
def _meta() -> dict:
    p = ART / "model_meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _vectorize(task: dict[str, Any]) -> np.ndarray:
    tt = task.get("task_type")
    row = {f: 0.0 for f in FEATURES}
    for f in SHARED + SIGNALS:
        v = task.get(f)
        if v is not None:
            row[f] = float(v)
    if tt in TASK_TYPES:
        row[f"is_{tt}"] = 1.0
    return pd.DataFrame([[row[f] for f in FEATURES]], columns=FEATURES, dtype=float)


def expected_value(task: dict[str, Any]) -> float:
    """Simple, documented E[value] per task (EUR).

    debt_followup : overdue_amount at T (cash directly at stake).
    reorder_due / cross_sell : expected line revenue ~ avg order value = monetary / order_count.
    churn_winback : client monetary (trailing-365 EUR turnover) -- relationship at risk.
    """
    tt = task.get("task_type")
    monetary = float(task.get("monetary", 0.0) or 0.0)
    oc = float(task.get("order_count", 0.0) or 0.0)
    aov = monetary / oc if oc > 0 else 0.0
    if tt == "debt_followup":
        return float(task.get("sig_overdue_amount", 0.0) or 0.0)
    if tt in ("reorder_due", "cross_sell"):
        return aov
    if tt == "churn_winback":
        return monetary
    return monetary


def score_task(task: dict[str, Any]) -> dict[str, float]:
    """Score one prepared task feature dict.

    `task` must carry `task_type` plus any available shared/signal features (missing -> 0).
    Returns p_outcome (calibrated), expected_value (EUR), ev_score (p*EUR, the ranking key),
    and priority (0..100 = 100*p_outcome convenience score).
    """
    p = float(_model().predict_proba(_vectorize(task))[0, 1])
    ev = expected_value(task)
    return {
        "p_outcome": round(p, 6),
        "expected_value": round(ev, 2),
        "ev_score": round(p * ev, 4),
        "priority": round(100.0 * p, 2),
    }


def score_batch(tasks: list[dict[str, Any]]) -> list[dict[str, float]]:
    if not tasks:
        return []
    X = pd.concat([_vectorize(t) for t in tasks], ignore_index=True)
    ps = _model().predict_proba(X)[:, 1]
    out = []
    for t, p in zip(tasks, ps, strict=False):
        ev = expected_value(t)
        out.append({"p_outcome": round(float(p), 6), "expected_value": round(ev, 2),
                    "ev_score": round(float(p) * ev, 4), "priority": round(100.0 * float(p), 2)})
    return out


if __name__ == "__main__":
    demo = [
        {"task_type": "debt_followup", "monetary": 50000, "order_count": 120,
         "recency_days": 5, "sig_overdue_amount": 8000, "sig_days_past_terms": 40,
         "sig_max_overdue_days": 40, "sig_debt_lines": 3},
        {"task_type": "reorder_due", "monetary": 80000, "order_count": 300, "recency_days": 2,
         "sig_elapsed_days": 30, "sig_cycle_days": 14, "sig_overdue_ratio": 2.1, "sig_n_orders": 9},
        {"task_type": "churn_winback", "monetary": 30000, "order_count": 60, "recency_days": 90,
         "sig_silence_days": 90, "sig_recent_orders": 1, "sig_prior_orders": 12, "sig_drop_ratio": 0.1},
        {"task_type": "cross_sell", "monetary": 40000, "order_count": 150, "recency_days": 3,
         "sig_top_score": 0.8, "sig_reco_candidates": 5},
    ]
    for t, s in zip(demo, score_batch(demo), strict=False):
        print(f"{t['task_type']:16s} -> {s}")
