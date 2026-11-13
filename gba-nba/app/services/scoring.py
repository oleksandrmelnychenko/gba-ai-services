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
    from app.ml.score_task import expected_value, score_task

    feats: dict = {"task_type": task_type}
    for k, v in signals.items():
        if k in _SHARED_KEYS:
            if k == "recency_days" and v is None:
                feats[k] = 9999.0
            elif v is not None:
                feats[k] = float(v)
        elif k in _FEATURE_KEY_MAP and v is not None:
            feats[_FEATURE_KEY_MAP[k]] = float(v)
    if score_source(task_type) == "heuristic":
        p_outcome = _heuristic_probability(task_type, signals)
        value = expected_value(feats)
        return (
            round(100.0 * p_outcome, 2),
            round(p_outcome, 6),
            round(value, 2),
            round(p_outcome * value, 4),
        )
    s = score_task(feats)
    return s["priority"], s["p_outcome"], s["expected_value"], s["ev_score"]


HEAD_UNTRAINED = "model_head_untrained"
HEAD_WITHHELD = "model_head_withheld"
MODEL_MISSING = "model_artifact_missing"
MODEL_INCOMPATIBLE = "model_incompatible"
MODEL_STALE = "model_stale"
RECO_UNAVAILABLE = "reco_unavailable"
SCORED_TYPES = ("debt_followup", "reorder_due", "churn_winback", "cross_sell")


def score_basis(task_type: str) -> dict:
    """What stands behind p_outcome for a task type: {"source", "reason"}.

    source "heuristic" always carries the reason the trained head is not used: no usable
    artefact, a head with fewer labelled rows than min_model_training_rows, or a head the
    retrain gate withheld. source "model" carries MODEL_STALE when the artefact is older than
    model_max_age_days (the weekly retrain has not replaced it); the probability is still the
    model's.
    """
    from app.ml import score_task

    settings = get_settings()

    def unusable() -> dict:
        reasons = score_task.model_compatibility()["model_reasons"]
        return {"source": "heuristic",
                "reason": MODEL_MISSING if MODEL_MISSING in reasons else MODEL_INCOMPATIBLE}

    try:
        head = (score_task._meta().get("heads") or {}).get(task_type)
    except (OSError, TypeError, ValueError):
        head = None
    if head is not None and not head.get("published", False):
        return {"source": "heuristic", "reason": HEAD_WITHHELD}
    if not score_task.head_published(task_type, settings.min_model_training_rows):
        if score_task.head_training_rows(task_type) == 0 and score_task.model_age_days() is None:
            return unusable()
        return {"source": "heuristic", "reason": HEAD_UNTRAINED}
    try:
        score_task._model()
    except score_task.IncompatibleModelArtifactError:
        return unusable()
    age = score_task.model_age_days()
    stale = age is not None and age > settings.model_max_age_days
    return {"source": "model", "reason": MODEL_STALE if stale else None}


def score_source(task_type: str) -> str:
    """"model" when the trained head may rank this task type, else "heuristic"."""
    return score_basis(task_type)["source"]


def degradation(extra_reasons: list[str] | None = None) -> dict:
    """Explicit degraded marker of the scoring service for API payloads.

    degraded_reasons lists every task type scored by the rule instead of the model as
    "heuristic_score:<type>:<reason>", a stale artefact as MODEL_STALE, plus caller-supplied
    run reasons such as RECO_UNAVAILABLE.
    """
    reasons: list[str] = []
    sources: dict[str, dict] = {}
    for task_type in SCORED_TYPES:
        basis = score_basis(task_type)
        sources[task_type] = basis
        if basis["source"] == "heuristic":
            reasons.append(f"heuristic_score:{task_type}:{basis['reason']}")
        elif basis["reason"] and basis["reason"] not in reasons:
            reasons.append(basis["reason"])
    for reason in extra_reasons or []:
        if reason not in reasons:
            reasons.append(reason)
    return {"degraded": bool(reasons), "degraded_reasons": reasons, "score_sources": sources}


def _heuristic_probability(task_type: str, signals: dict) -> float:
    """Deterministic score for a task type the model has no basis to rank."""
    monetary = float(signals.get("monetary") or 0.0)
    if task_type == "debt_followup":
        urgency = debt_urgency(int(
            signals.get("lead_days_past_terms") or signals.get("days_past_terms") or 0))
        value = value_from_monetary(float(signals.get("overdue_amount") or 0.0))
        confidence = 1.0
    elif task_type == "reorder_due":
        urgency = reorder_urgency(
            float(signals.get("elapsed_days") or 0.0),
            float(signals.get("cycle_days") or 0.0),
        )
        value = value_from_monetary(monetary)
        confidence = min(1.0, 0.4 + 0.1 * float(signals.get("n_orders") or 0.0))
    elif task_type == "churn_winback":
        urgency = churn_urgency(
            float(signals.get("drop_ratio") or 0.0),
            int(signals.get("silence_days") or 0),
        )
        value = value_from_monetary(monetary)
        confidence = min(1.0, 0.5 + 0.05 * int(signals.get("prior_orders") or 0))
    elif task_type == "cross_sell":
        top_score = float(signals.get("top_score") or 0.0)
        urgency = crosssell_urgency(top_score)
        value = value_from_monetary(monetary)
        confidence = max(0.0, min(1.0, top_score))
    else:
        return 0.0
    return priority(urgency, value, confidence) / 100.0


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


DEBT_BAND_GRACE = "grace"
DEBT_BAND_FRESH = "fresh"
DEBT_BAND_AGED = "aged"
DEBT_BAND_OLD = "old"
_DEBT_BAND_RANK = {DEBT_BAND_FRESH: 0, DEBT_BAND_AGED: 1, DEBT_BAND_OLD: 2, DEBT_BAND_GRACE: 3}


def debt_age_band(days_past_terms: int) -> str:
    s = get_settings()
    if days_past_terms <= s.debt_grace_days:
        return DEBT_BAND_GRACE
    if days_past_terms <= s.debt_fresh_max_days:
        return DEBT_BAND_FRESH
    return DEBT_BAND_AGED if days_past_terms <= s.debt_aged_max_days else DEBT_BAND_OLD


def debt_urgency(days_past_terms: int) -> float:
    """Urgency of one overdue document by how collectable it is, not by how old it is.

    Inside the grace period: 0 (most are paid without a call). Just past it the debt is the
    most collectable and the most urgent: HIGH, 0.84 falling to 0.70 at debt_fresh_max_days.
    Up to debt_aged_max_days: NORMAL, 0.55 falling to 0.40. Older: LOW 0.20 — old debt is
    rarely settled by a call (5.6% in five days), it stays listed and no longer leads.
    """
    s = get_settings()
    band = debt_age_band(days_past_terms)
    if band == DEBT_BAND_GRACE:
        return 0.0
    if band == DEBT_BAND_FRESH:
        span = max(1, s.debt_fresh_max_days - s.debt_grace_days - 1)
        return 0.84 - 0.14 * (days_past_terms - s.debt_grace_days - 1) / span
    if band == DEBT_BAND_AGED:
        span = max(1, s.debt_aged_max_days - s.debt_fresh_max_days - 1)
        return 0.55 - 0.15 * (days_past_terms - s.debt_fresh_max_days - 1) / span
    return 0.2


def debt_lead_days(days_past_terms: list[int]) -> int:
    """The document that sets a client's debt urgency: the most collectable one past grace."""
    return min(days_past_terms, key=lambda days: (_DEBT_BAND_RANK[debt_age_band(days)], days))


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
