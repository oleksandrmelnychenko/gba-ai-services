from __future__ import annotations

import json
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path
from typing import Any

INDEX_MODEL_VERSION = "creditscore-v4"
INDEX_MODEL_NAME = "payment_behaviour_index"
MATERIAL_DEBT_EUR = 20.0

_ART = Path(__file__).resolve().parent / "artifacts" / "index_card.json"

_RATING = {"A": "low risk", "B": "moderate risk", "C": "elevated risk", "D": "high risk"}
_BAND_ORDER = ("A", "B", "C", "D")
_OVERDUE_BUCKETS = (
    "overdue_eur_1_30",
    "overdue_eur_31_60",
    "overdue_eur_61_90",
    "overdue_eur_91_180",
    "overdue_eur_180plus",
)
_REQUIRED_KEYS = {"model", "model_version", "run_id", "components", "scale", "bands", "policy"}


class IndexCardError(RuntimeError):
    pass


def validate_card(card: dict[str, Any]) -> dict[str, Any]:
    missing = _REQUIRED_KEYS - set(card)
    if missing:
        raise IndexCardError(f"index card misses keys: {sorted(missing)}")
    if card["model_version"] != INDEX_MODEL_VERSION:
        raise IndexCardError(f"index card version {card['model_version']!r} is not served")
    for name, comp in card["components"].items():
        if len(comp["logodds"]) != len(comp["edges"]) + 1:
            raise IndexCardError(f"component {name}: bins and points do not match")
    scale = card["scale"]
    if not scale["z_max"] > scale["z_min"]:
        raise IndexCardError("index card scale is empty")
    floors = [card["bands"][b]["min_score"] for b in _BAND_ORDER]
    if floors != sorted(floors, reverse=True) or floors[-1] != 0:
        raise IndexCardError("index card band floors are not descending to zero")
    return card


@lru_cache(maxsize=1)
def _card() -> dict[str, Any]:
    return validate_card(json.loads(_ART.read_text()))


def index_model_readiness() -> dict[str, Any]:
    try:
        card = _card()
    except (OSError, json.JSONDecodeError, KeyError, IndexCardError) as exc:
        return {"ready": False, "reason": str(exc), "training_run_id": None}
    return {
        "ready": True,
        "reason": None,
        "training_run_id": card["run_id"],
        "trained_at": card.get("trained_at"),
        "validation": card.get("validation"),
    }


def index_inputs(features: dict[str, float]) -> dict[str, float]:
    total_debt = float(features.get("total_debt_eur", 0.0) or 0.0)
    if total_debt < MATERIAL_DEBT_EUR:
        total_debt = 0.0
    turnover = float(features.get("turnover_eur_12mo", 0.0) or 0.0)
    limit = float(features.get("credit_limit_eur", 0.0) or 0.0)
    overdue = sum(float(features.get(name, 0.0) or 0.0) for name in _OVERDUE_BUCKETS)
    if turnover > 0.0:
        debt_to_turnover = min(total_debt / (turnover / 12.0), 12.0)
    else:
        debt_to_turnover = 12.0 if total_debt > 0.0 else 0.0
    return {
        "overdue_now_eur": overdue,
        "oldest_overdue_days": float(features.get("oldest_overdue_days", 0.0) or 0.0),
        "worst_delay_12m_days": float(features.get("worst_delay_12m_days", 0.0) or 0.0),
        "share_late_invoices_12m": float(features.get("share_late_invoices_12m", 0.0) or 0.0),
        "delay_trend_3m_days": float(features.get("delay_trend_3m_days", 0.0) or 0.0),
        "debt_to_monthly_turnover": debt_to_turnover,
        "limit_utilization": min(total_debt / limit, 3.0) if limit > 0.0 else 0.0,
    }


def _band_for(score: int, bands: dict[str, Any]) -> str:
    for band in _BAND_ORDER:
        if score >= bands[band]["min_score"]:
            return band
    return "D"


def _worse(a: str, b: str) -> str:
    return a if _BAND_ORDER.index(a) >= _BAND_ORDER.index(b) else b


def score_index(features: dict[str, float], card: dict[str, Any] | None = None) -> dict[str, Any]:
    card = card or _card()
    inputs = index_inputs(features)
    has_history = float(features.get("matured_invoices_12m", 0.0) or 0.0) > 0.0
    history_components = set(card.get("history_components", []))
    z = 0.0
    contributions = []
    for name, comp in card["components"].items():
        value = inputs[name]
        if name in history_components and not has_history:
            points = float(comp.get("no_history_logodds", 0.0))
            shown: float | None = None
        else:
            points = float(comp["logodds"][bisect_left(comp["edges"], value)])
            shown = value
        z += points
        contributions.append({"feature": name, "value": shown, "points": round(points, 4)})

    scale = card["scale"]
    raw = 100.0 * (scale["z_max"] - z) / (scale["z_max"] - scale["z_min"])
    score = int(round(min(max(raw, 0.0), 100.0)))
    band = _band_for(score, card["bands"])
    probability = float(card["bands"][band]["pd"])

    policy = card["policy"]
    overrides: list[str] = []
    severe = sum(
        float(features.get(name, 0.0) or 0.0)
        for name in ("overdue_eur_91_180", "overdue_eur_180plus")
    )
    delinquent = severe + sum(
        float(features.get(name, 0.0) or 0.0)
        for name in ("overdue_eur_31_60", "overdue_eur_61_90")
    )
    if severe >= policy["severe_min_eur"]:
        band = "D"
        probability = max(probability, float(policy["severe_pd"]))
        overrides.append("overdue_90_plus")
    elif delinquent >= policy["delinquent_min_eur"]:
        band = _worse(band, "C")
        probability = max(probability, float(policy["delinquent_pd"]))
        overrides.append("overdue_30_plus")
    if overrides:
        score = min(score, int(card["bands"][band]["max_score"]))

    contributions.sort(key=lambda c: abs(c["points"]), reverse=True)
    return {
        "score": score,
        "pd": round(probability, 6),
        "band": band,
        "rating": _RATING[band],
        "linear_predictor": round(z, 4),
        "contributions": contributions,
        "model": card["model"],
        "model_version": card["run_id"],
        "policy_overrides": overrides,
    }
