"""Forward 6-month SEV180 risk scoring (production, explainable).

Loads the WOE+logistic scorecard from app/risk/artifacts/forward_scorecard_coeffs.json
and produces a 0-100 forward-risk score + PD-based band for an at-risk client.

Two scorecards live in the artifact:
  * behavioral_only  -- the PRIMARY honest early-warning ranker (chronicity,
    trajectory, RFM, terms). Drives the 0-100 score and the PD band.
  * with_aging       -- includes overdue aging buckets + total_debt. This model
    is near-deterministic because a 6mo SEV180 label is largely the ARITHMETIC
    of existing overdue debt aging into 180+. We expose its PD as an override
    flag ("debt already rolling into default") rather than as the score basis.

Population: at-risk-with-debt only (total_debt_eur > 0, not already SEV180).
Clients with zero debt are not at risk on a 6mo horizon -> score 0, band "none".
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

_ART = Path(__file__).resolve().parent / "artifacts" / "forward_scorecard_coeffs.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    return json.loads(_ART.read_text())


def _apply_woe(value: float, bins: list[dict]) -> float:
    v = float(value)
    for b in bins:
        lo = b["lo"]
        hi = b["hi"]
        lo_v = -math.inf if lo is None else float(lo)
        hi_v = math.inf if hi is None else float(hi)
        if lo_v == -math.inf:
            if v <= hi_v:
                return float(b["woe"])
        else:
            if lo_v < v <= hi_v:
                return float(b["woe"])
    # value below all bins (e.g. negative) -> nearest (first) bin woe
    return float(bins[0]["woe"]) if bins else 0.0


def _scorecard_pd(features: dict[str, float], card: dict) -> float:
    logit = card["intercept"]
    for c in card["feat_cols"]:
        woe = _apply_woe(features.get(c, 0.0), card["bins"][c])
        logit += card["coefficients"][c] * woe
    return 1.0 / (1.0 + math.exp(-logit))


def _band(pd_beh: float, bands: dict) -> str:
    if pd_beh >= bands["very_high"]:
        return "very_high"
    if pd_beh >= bands["high"]:
        return "high"
    if pd_beh >= bands["medium"]:
        return "medium"
    return "low"


def score_forward(features: dict[str, float]) -> dict[str, Any]:
    """Score one client for 6-month forward SEV180 risk.

    `features` -> mapping of the raw feature columns (same names as the vintage
    dataset). Missing keys default to 0.

    Returns dict with:
      score          : 0-100, higher = higher forward-default risk (behavioral)
      pd_behavioral  : behavioral-only PD (honest early-warning)
      pd_with_aging  : with-aging PD (near-deterministic arithmetic view)
      band           : low / medium / high / very_high (or 'none' if no debt)
      already_rolling: bool, pd_with_aging >= 0.5 (overdue debt arithmetically
                       on track to hit 180+ within horizon)
    """
    art = _load()
    total_debt = float(features.get("total_debt_eur", 0.0) or 0.0)
    if total_debt <= 0:
        return {"score": 0.0, "pd_behavioral": 0.0, "pd_with_aging": 0.0,
                "band": "none", "already_rolling": False,
                "note": "no debt -> not at risk on 6mo horizon"}

    pd_beh = _scorecard_pd(features, art["scorecard_behavioral_only"])
    pd_aging = _scorecard_pd(features, art["scorecard_with_aging"])
    band = _band(pd_beh, art["pd_bands"])
    return {
        "score": round(100.0 * pd_beh, 1),
        "pd_behavioral": round(pd_beh, 4),
        "pd_with_aging": round(pd_aging, 4),
        "band": band,
        "already_rolling": bool(pd_aging >= 0.5),
    }
