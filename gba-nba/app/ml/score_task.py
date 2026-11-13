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
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from app.core import history
from app.core.config import get_settings
from app.ml import calibration

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
TRAINING_WINDOW_DAYS = 365


class IncompatibleModelArtifactError(RuntimeError):
    """The serving artifact was trained under a different source-history contract."""


@lru_cache(maxsize=1)
def _model():
    compatibility = model_compatibility()
    if not compatibility["model_compatible"]:
        raise IncompatibleModelArtifactError(
            "incompatible NBA model artifact: "
            + ", ".join(compatibility["model_reasons"])
        )
    return joblib.load(ART / "propensity_model.joblib")


@lru_cache(maxsize=1)
def _meta() -> dict:
    p = ART / "model_meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def model_compatibility() -> dict[str, Any]:
    """Readiness contract for the model artifact; legacy/partial-history models fail closed."""
    try:
        meta = _meta()
    except (OSError, TypeError, ValueError):
        meta = {}
        unreadable = True
    else:
        unreadable = False
    expected_source_start = history.source_history_start().isoformat()
    reasons: list[str] = []
    if unreadable:
        reasons.append("model_metadata_unreadable")
    if not (ART / "propensity_model.joblib").is_file():
        reasons.append("model_artifact_missing")
    if meta.get("source_history_start") != expected_source_start:
        reasons.append("model_source_history_mismatch")
    if meta.get("training_window_days") != TRAINING_WINDOW_DAYS:
        reasons.append("model_training_window_mismatch")

    vintages = meta.get("training_vintages")
    if not isinstance(vintages, list) or not vintages:
        reasons.append("model_training_vintages_missing")
    else:
        try:
            canonical_vintages = [
                history.training_window(vintage, TRAINING_WINDOW_DAYS).as_of.isoformat()
                for vintage in vintages
            ]
            if canonical_vintages != sorted(set(canonical_vintages)):
                reasons.append("model_training_vintages_invalid")
        except (TypeError, ValueError):
            reasons.append("model_training_vintage_incomplete")

    return {
        "model_compatible": not reasons,
        "model_reasons": reasons,
        "model_source_history_start": meta.get("source_history_start"),
        "model_training_window_days": meta.get("training_window_days"),
        "model_training_vintages": vintages if isinstance(vintages, list) else [],
    }


@lru_cache(maxsize=1)
def _metrics() -> dict:
    p = ART / "metrics.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def head_training_rows(task_type: str) -> int:
    """Labelled training rows behind one task type's head (0 when unknown)."""
    try:
        head = (_meta().get("heads") or {}).get(task_type)
        if head is not None:
            return int(head.get("rows") or 0)
        return int((_metrics().get("cv_per_type") or {}).get(task_type, {}).get("n") or 0)
    except (OSError, TypeError, ValueError):
        return 0


def head_published(task_type: str, min_rows: int) -> bool:
    """True when the model may rank this task type: enough labelled rows and not withheld by
    the retrain per-type gate. Otherwise the caller uses the deterministic heuristic."""
    try:
        head = (_meta().get("heads") or {}).get(task_type)
    except (OSError, TypeError, ValueError):
        return False
    if head is not None and not head.get("published", False):
        return False
    return head_training_rows(task_type) >= min_rows


def model_age_days(now: datetime | None = None) -> float | None:
    """Days since the serving artefact was written; None when it is absent."""
    path = ART / "propensity_model.joblib"
    try:
        written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    return ((now or datetime.now(UTC)) - written).total_seconds() / 86400.0


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

    One horizon for every type: the EUR expected inside the outcome window (ev_horizon_days,
    the model's 60-day label window) if the outcome happens.
    debt_followup : overdue_amount at T (cash collected inside the window).
    reorder_due / cross_sell : one order ~ avg order value = monetary / order_count.
    churn_winback : the client's turnover over the window = trailing-365 turnover * horizon/365.
    """
    tt = task.get("task_type")
    monetary = float(task.get("monetary", 0.0) or 0.0)
    oc = float(task.get("order_count", 0.0) or 0.0)
    aov = monetary / oc if oc > 0 else 0.0
    if tt == "debt_followup":
        return float(task.get("sig_overdue_amount", 0.0) or 0.0)
    if tt in ("reorder_due", "cross_sell"):
        return aov
    return horizon_turnover(monetary)


def horizon_turnover(annual_turnover: float) -> float:
    return annual_turnover * get_settings().ev_horizon_days / TRAINING_WINDOW_DAYS


def score_task(task: dict[str, Any]) -> dict[str, float]:
    """Score one prepared task feature dict.

    `task` must carry `task_type` plus any available shared/signal features (missing -> 0).
    Returns p_outcome (calibrated), expected_value (EUR), ev_score (p*EUR, the ranking key),
    and priority (0..100 = 100*p_outcome convenience score).
    """
    p_raw = float(_model().predict_proba(_vectorize(task))[0, 1])
    p = calibration.apply(task.get("task_type"), p_raw)
    ev = expected_value(task)
    return {
        "p_raw": round(p_raw, 6),
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
    for t, p_raw in zip(tasks, ps, strict=False):
        p = calibration.apply(t.get("task_type"), float(p_raw))
        ev = expected_value(t)
        out.append({"p_raw": round(float(p_raw), 6), "p_outcome": round(float(p), 6),
                    "expected_value": round(ev, 2),
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
