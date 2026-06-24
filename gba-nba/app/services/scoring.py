"""Task prioritization: priority = w_u·urgency + w_v·value + w_c·confidence.

All inputs normalized to 0..1. The weights, value saturation and urgency bands are env-driven
(app.core.config) so they can be A/B-tuned without a redeploy — bump model_version on change.
Scoring lives in ONE place so every generator is comparable.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.domain.models import Urgency


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
