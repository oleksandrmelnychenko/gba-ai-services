"""Prediction interval around a monthly forecast, from past forecast errors.

The table holds, per scope, demand class, forecast-size band and step, the 10th and 90th
percentile of actual / forecast observed in a rolling-origin backtest
(scripts/build_interval_table.py). The interval is forecast * [low, high]: about 80% of past
months fell inside it, about 10% above. Steps count from the last closed month; the first
emitted point is step 2. Steps past the last tabulated one reuse it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.services.forecasting import classify

TABLE_PATH = Path(__file__).with_name("interval_table.json")
LOWER_QUANTILE = 0.1
UPPER_QUANTILE = 0.9
COVERAGE_PCT = 80
FIRST_STEP = 2
LAST_STEP = 7
_FALLBACK_CLASS = classify.LUMPY


@lru_cache
def table() -> dict:
    payload = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    if (
        payload.get("lower_quantile") != LOWER_QUANTILE
        or payload.get("upper_quantile") != UPPER_QUANTILE
        or payload.get("first_step") != FIRST_STEP
        or payload.get("last_step") != LAST_STEP
    ):
        raise ValueError("interval table does not match the service constants")
    return payload


def band_index(edges: list[float], forecast_eur: float) -> int:
    return sum(1 for edge in edges if forecast_eur > edge)


def lookup(scope_table: dict, segment: str, forecast_eur: float, step: int) -> tuple[float, float]:
    entry = scope_table.get(segment) or scope_table.get(_FALLBACK_CLASS)
    if entry is None:
        raise ValueError("interval table has no entry for this demand class")
    cells = entry["bands"][band_index(entry["size_edges_eur"], forecast_eur)]
    low, high, _count = cells[min(max(step, FIRST_STEP), LAST_STEP) - FIRST_STEP]
    return float(low), float(high)


def multipliers(scope: str, segment: str, forecast_eur: float, step: int) -> tuple[float, float]:
    scopes = table()["scopes"]
    if scope not in scopes:
        raise ValueError(f"unknown forecast scope: {scope}")
    low, high = lookup(scopes[scope], segment, forecast_eur, step)
    return min(low, 1.0), max(high, 1.0)


def metadata() -> dict:
    return {
        "coverage_pct": COVERAGE_PCT,
        "lower_quantile": LOWER_QUANTILE,
        "upper_quantile": UPPER_QUANTILE,
        "basis": "rolling_origin_relative_error",
        "table_version": table()["table_version"],
    }
