"""Task prioritization.

`priority` (0..100) is now driven by the trained propensity model (app.ml.score_task):
`priority = 100 * p_outcome`, where p_outcome = calibrated P(outcome in (T,T+H] | task). Live
ranking/caps use `ev_score = p_outcome * expected_value` after the urgency band, with task type only
as a deterministic tie-breaker and `priority` only as the compatibility field / legacy fallback. This
replaces the old expert-guessed blend
(w_u·urgency + w_v·value + w_c·confidence), whose pooled OOT AUC was ~0.55 (≈coin-flip; the debt
term was actually INVERTED — biggest-overdue ranked LEAST likely to repay). The current model lifts
OOT AUC to ~0.70. Generators call `score_task_priority()`; the legacy blend (`priority()`,
`value_from_monetary()`) is retained only so app.ml.dataset can recompute the OLD priority for the
train-time benchmark.

URGENCY stays a separate, real display/sort BAND (urgency_band + the per-type urgency helpers): the
inbox sort is urgency tier -> EV score -> type tie-breaker -> priority fallback. priority and
urgency are NOT collapsed — priority is the 0..100 likelihood score, urgency is the cash-at-risk /
time-pressure triage band.

The blend weights / value saturation / urgency bands are env-driven (app.core.config); bump
model_version on any scoring change so outcomes can be sliced/A-B'd by scoring generation.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.models import Urgency

# Live generator feature-dict key -> model FEATURE (see app/ml/model_meta.json / app/ml/dataset.py).
# The generators assemble a feature dict from the SAME raw signal rows the dataset builder replayed,
# so these map 1:1 onto the model's sig_* columns. Shared features (monetary/recency_days/order_count)
# pass through under their own names. Anything the model expects but the dict omits -> 0 (score_task's
# _vectorize default), matching the dataset's 0-fill for non-owning-type signals.
_FEATURE_KEY_MAP = {
    # debt_followup
    "overdue_amount": "sig_overdue_amount",
    "days_past_terms": "sig_days_past_terms",
    "max_overdue_days": "sig_max_overdue_days",
    "debt_lines": "sig_debt_lines",
    # reorder_due
    "elapsed_days": "sig_elapsed_days",
    "cycle_days": "sig_cycle_days",
    "overdue_ratio": "sig_overdue_ratio",
    "n_orders": "sig_n_orders",
    # churn_winback
    "drop_ratio": "sig_drop_ratio",
    "silence_days": "sig_silence_days",
    "recent_orders": "sig_recent_orders",
    "prior_orders": "sig_prior_orders",
    # cross_sell
    "top_score": "sig_top_score",
    "candidates": "sig_reco_candidates",
}
_SHARED_KEYS = ("monetary", "recency_days", "order_count")


def score_task_priority(task_type: str, signals: dict) -> tuple[float, float, float, float]:
    """Score one candidate via the trained propensity model. `signals` is the generator's per-task
    feature dict using the SHORT raw-signal keys (overdue_amount, elapsed_days, drop_ratio, top_score,
    ...) plus the shared client features (monetary, recency_days, order_count). Returns
    (priority, p_outcome, expected_value, ev_score):
      priority      = 100 * p_outcome (0..100, the unchanged Mongo/orchestrator/inbox contract field)
      p_outcome     = calibrated P(outcome | task)
      expected_value= documented E[value] in EUR (score_task.expected_value)
      ev_score      = p_outcome * expected_value (the expected-EUR ranking key for the cockpit)

    Lazy import keeps the ML deps (joblib/sklearn) off the hot import path; score_task caches the
    loaded model. recency_days=None is mapped to 9999 — the dataset's missing-recency sentinel."""
    from app.ml.score_task import score_task

    feats: dict = {"task_type": task_type}
    for k, v in signals.items():
        if k in _SHARED_KEYS:
            if k == "recency_days" and v is None:
                feats[k] = 9999.0
            elif v is not None:
                feats[k] = float(v)
        elif k in _FEATURE_KEY_MAP and v is not None:
            feats[_FEATURE_KEY_MAP[k]] = float(v)
    s = score_task(feats)
    return s["priority"], s["p_outcome"], s["expected_value"], s["ev_score"]


def _sat(x: float, k: float) -> float:
    """Saturating 0..1 curve: x/(x+k). Diminishing returns for large raw values."""
    return x / (x + k) if x > 0 else 0.0


def value_from_monetary(monetary: float) -> float:
    # k≈p75 of active-manager annual monetary (EUR): median buyer ~0.14, p75->0.5, p90->0.8.
    return _sat(monetary, get_settings().value_saturation)


def priority(urgency_norm: float, value_norm: float, confidence: float) -> float:
    s = get_settings()
    p = s.w_urgency * urgency_norm + s.w_value * value_norm + s.w_confidence * confidence
    return round(100.0 * max(0.0, min(1.0, p)), 2)


def urgency_band(urgency_norm: float) -> Urgency:
    s = get_settings()
    if urgency_norm >= s.urgency_band_critical:
        return Urgency.CRITICAL
    if urgency_norm >= s.urgency_band_high:
        return Urgency.HIGH
    if urgency_norm >= s.urgency_band_normal:
        return Urgency.NORMAL
    return Urgency.LOW


def debt_urgency(days_past_terms: int) -> float:
    """Any overdue debt is at least HIGH (>=0.6) — it's cash already at risk; older debt scales to
    CRITICAL (>=0.85 at ~100 days past terms). Without this floor most debts are only a few days
    past short terms (measured: 152/212 had <13d past), scored LOW, and got buried under reorder.
    """
    if days_past_terms <= 0:
        return 0.0
    return max(0.0, min(1.0, 0.6 + 0.4 * _sat(days_past_terms, 60.0)))


def reorder_urgency(elapsed_days: float, cycle_days: float) -> float:
    """ratio = elapsed/cycle. Just-due (1x) is a routine NORMAL nudge; reorder tops out at HIGH.

    Linear across the [1x, 3x] window the signal keeps (reorder_max_overdue_mult caps it at 3x):
    1x->0.30 (normal), 2.2x->0.60 (high), 3x->0.80 (top of HIGH). Slope 0.25 deliberately keeps
    even the most-overdue reorder below CRITICAL (0.85): the signal's own 3x ceiling already means
    "abandoned/near-churn", so routine restocking must never outrank a CRITICAL debt (cash at risk).
    Measured on real data: at slope 0.35 the per-task lead clustered at ~2.9x -> 49/50 inbox tasks
    were CRITICAL, inverting the debt-first intent.
    """
    if cycle_days <= 0:
        return 0.5
    ratio = elapsed_days / cycle_days
    return max(0.0, min(1.0, 0.3 + 0.25 * (ratio - 1.0)))


def churn_urgency(drop_ratio: float, silence_days: int) -> float:
    """drop_ratio = recent/prior (0..<0.5 for candidates). Bigger drop + longer silence = more urgent."""
    drop = 1.0 - max(0.0, min(1.0, drop_ratio))   # bigger drop -> closer to 1
    silence = _sat(max(silence_days, 0), 120.0)
    return max(0.0, min(1.0, 0.6 * drop + 0.4 * silence))


def crosssell_urgency(top_score: float) -> float:
    """Cross-sell isn't time-urgent; urgency tracks opportunity strength (reco score 0..1)."""
    return max(0.0, min(1.0, 0.3 + 0.5 * top_score))


def new_client_urgency(days_since_created: int) -> float:
    """A new client without a first order grows more urgent the longer they sit un-activated
    (the relationship is slipping away). NORMAL when fresh, approaching HIGH by ~60 days."""
    return max(0.0, min(1.0, 0.4 + 0.4 * _sat(max(days_since_created, 0), 45.0)))
