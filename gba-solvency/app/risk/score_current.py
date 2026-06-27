"""Pure current-state SEV180 risk scorer (WOE + logistic scorecard).

Loads app/risk/artifacts/scorecard_coefficients.json and reproduces, with NO sklearn at
inference time, the production scorecard:

    feature -> WOE bin lookup -> (coef * WOE) summed + intercept = linear predictor
    -> Platt calibration (a*lin + b) -> sigmoid = PD
    -> log-odds points mapping -> 0..100 score (higher = safer)
    -> rating band from PD thresholds.

`score_current(features)` returns {score, pd, band, rating, linear_predictor, contributions}
where contributions are the per-feature signed points (coef*WOE), i.e. why the score is what
it is. The two tautological features (overdue_eur_180plus, pct_debt_180plus) are intentionally
NOT used so the score reflects current risk posture rather than the SEV180 defining rule.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

_ART = Path(__file__).resolve().parent / "artifacts" / "scorecard_coefficients.json"


@lru_cache(maxsize=1)
def _card() -> dict[str, Any]:
    return json.loads(_ART.read_text())


def _woe_value(v: float | None, splits: list[float], woe: list[float]) -> float:
    """Bin lookup matching training: bins are (-inf,s0],(s0,s1],...,(s_{n-1},+inf]; missing->bin0."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return woe[0]
    k = len(splits)
    for i, s in enumerate(splits):
        if v <= s:
            k = i
            break
    if k >= len(woe):
        k = len(woe) - 1
    return woe[k]


def _band_from_pd(p: float, bands: dict[str, float]) -> str:
    if p < bands["A"]:
        return "A"
    if p < bands["B"]:
        return "B"
    if p < bands["C"]:
        return "C"
    return "D"


_RATING = {"A": "low risk", "B": "moderate risk", "C": "elevated risk", "D": "high risk"}


def score_current(features: dict[str, float]) -> dict[str, Any]:
    """Score one client's current-state risk from its feature dict.

    `features` keys = the 20 primary feature names (missing keys treated as missing -> bin0).
    Returns: score (0..100, higher=safer), pd (0..1), band (A/B/C/D), rating, linear_predictor,
    and contributions: list of {feature, value, woe, points} sorted by |points| descending.
    """
    card = _card()
    coefs = card["logistic"]["coef"]
    intercept = card["logistic"]["intercept"]
    bins = card["woe_bins"]

    lin = intercept
    contribs = []
    for f in card["features"]:
        v = features.get(f)
        v = None if v is None else float(v)
        woe = _woe_value(v, bins[f]["splits"], bins[f]["woe"])
        pts = coefs[f] * woe
        lin += pts
        contribs.append({"feature": f, "value": v, "woe": round(woe, 4), "points": round(pts, 4)})

    a = card["calibration"]["a"]
    b = card["calibration"]["b"]
    z = a * lin + b
    pd = 1.0 / (1.0 + math.exp(-z))

    sm = card["score_mapping"]
    pd_c = min(max(pd, 1e-6), 1 - 1e-6)
    odds = (1 - pd_c) / pd_c
    factor = sm["pdo"] / math.log(2)
    raw = sm["anchor_offset"] + factor * math.log(odds)
    score = (raw - sm["score_range_lo"]) / (sm["score_range_hi"] - sm["score_range_lo"]) * 100
    score = min(max(score, 0.0), 100.0)

    band = _band_from_pd(pd, card["bands"])
    contribs.sort(key=lambda c: abs(c["points"]), reverse=True)
    return {
        "score": round(score, 2),
        "pd": round(pd, 6),
        "band": band,
        "rating": _RATING[band],
        "linear_predictor": round(lin, 4),
        "contributions": contribs,
        "model": card["model"],
        "model_version": "sev180-current-v1",
    }
