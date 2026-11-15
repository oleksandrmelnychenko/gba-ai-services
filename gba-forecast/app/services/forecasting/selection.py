"""Per-scope, per-segment forecast-method selection.

Regular series (smooth, erratic) are forecast with an EWMA level. Sporadic series
(intermittent, lumpy) of the client and product scopes are forecast with TSB: a smoothed
demand probability times a smoothed demand size. A sale already booked in the open current
month is a demand occurrence (its amount is still partial, so it never updates the size).
Sporadic client+product pairs keep the EWMA
level. Constants depend on the scope and the demand class and are chosen on the months the
service emits: the current month is still open when a forecast is requested, so the first
emitted point is two steps past the last closed month.

Evidence, 2026-11-13: rolling origins on confirmed sales net of returns 2025-01..2026-10,
steps 2..7, constants chosen on targets up to 2026-04 and checked on 2026-05..2026-10
(section 11 of docs/statistical-review-2026-11-12.md).

  scope    class         method            WAPE on the choice window, before -> after
  client   smooth        EWMA 0.30         kept
  client   erratic       EWMA 0.30         kept
  client   intermittent  TSB 0.5 / 0.5     116.8 -> 116.3
  client   lumpy         TSB 0.5 / 0.5     145.2 -> 141.2
  product  smooth        EWMA 0.15         kept
  product  erratic       EWMA 0.15         kept
  product  intermittent  TSB 0.2 / 0.3     124.2 -> 123.0
  product  lumpy         TSB 0.2 / 0.3     118.0 -> 114.4
  pair     smooth        EWMA 0.10         kept
  pair     erratic       EWMA 0.10         kept
  pair     sporadic      EWMA 0.40         kept, no candidate is better

The horizon is a flat run-rate. A pooled growth term, a pooled seasonal factor, a damped trend,
reversion to the long mean and a separate constant per step were each tested per step; none
beats the flat line on the choice window.

`forecast_method` in Settings selects the MODE:
  "auto"                                   -> use this selector (recommended).
  "moving_avg"|"croston"|"sba"|"ewma"|"damped_trend"
                                           -> force that single method for every series (A-B).
"""

from __future__ import annotations

from app.services.forecasting import classify, demand

DEFAULT_SEGMENT_METHOD: dict[str, str] = {
    classify.SMOOTH: "ewma",
    classify.ERRATIC: "ewma",
    classify.INTERMITTENT: "ewma",
    classify.LUMPY: "ewma",
    classify.NO_DEMAND: "moving_avg",
}

SCOPE_CLIENT = "client"
SCOPE_PRODUCT = "product"
SCOPE_PAIR = "pair"
SCOPES = (SCOPE_CLIENT, SCOPE_PRODUCT, SCOPE_PAIR)

SEGMENT_EWMA_ALPHA: dict[str, dict[str, float]] = {
    SCOPE_CLIENT: {
        classify.SMOOTH: 0.3,
        classify.ERRATIC: 0.3,
    },
    SCOPE_PRODUCT: {
        classify.SMOOTH: 0.15,
        classify.ERRATIC: 0.15,
    },
    SCOPE_PAIR: {
        classify.SMOOTH: 0.1,
        classify.ERRATIC: 0.1,
        classify.INTERMITTENT: 0.4,
        classify.LUMPY: 0.4,
    },
}

SEGMENT_TSB: dict[str, dict[str, tuple[float, float]]] = {
    SCOPE_CLIENT: {
        classify.INTERMITTENT: (0.5, 0.5),
        classify.LUMPY: (0.5, 0.5),
    },
    SCOPE_PRODUCT: {
        classify.INTERMITTENT: (0.2, 0.3),
        classify.LUMPY: (0.2, 0.3),
    },
    SCOPE_PAIR: {},
}

HORIZON_FLAT = "flat_run_rate"
HORIZON_DAMPED_TREND = "damped_trend"

LEGACY_SEGMENT_METHOD: dict[str, str] = {
    classify.SMOOTH: "ewma",
    classify.ERRATIC: "moving_avg",
    classify.INTERMITTENT: "sba",
    classify.LUMPY: "sba",
    classify.NO_DEMAND: "moving_avg",
}

# Methods the selector is allowed to return; anything else falls back to the safe default.
# Includes the trend/recency-aware methods (ewma adopted above; damped_trend kept runnable for
# A-B and forced-mode even though the backtest preferred EWMA over it on every segment).
_VALID_METHODS = {"moving_avg", "croston", "sba", "ewma", "damped_trend"}
_SAFE_DEFAULT = "moving_avg"

# forecast_method values that mean "force this single method for every series".
_FORCED_METHODS = _VALID_METHODS
# forecast_method value that means "use the per-segment selector".
AUTO = "auto"


def method_for_segment(
    segment: str, segment_method: dict[str, str] | None = None
) -> str:
    """Pick the forecast method for a demand segment, defaulting safely.

    Uses `segment_method` (e.g. a backtest-tuned map) if given, else the literature prior.
    An unknown segment or an unknown/invalid mapped method falls back to moving_avg so the
    forecaster can never be handed a method it cannot run.
    """
    table = segment_method or DEFAULT_SEGMENT_METHOD
    method = table.get(segment, _SAFE_DEFAULT)
    return method if method in _VALID_METHODS else _SAFE_DEFAULT


def is_auto(forecast_method: str) -> bool:
    return (forecast_method or AUTO).lower() not in _FORCED_METHODS


def select_method(
    series: list[float],
    forecast_method: str,
    segment_method: dict[str, str] | None = None,
    scope: str = SCOPE_CLIENT,
) -> tuple[str, str]:
    """Resolve the method for one series given the configured MODE.

    Returns (method, segment). When `forecast_method` forces a single method, that method is
    used and the segment is still reported (for logging/metrics). When it is "auto" (or any
    unrecognised value), the per-segment selector decides. Always returns a runnable method.
    """
    mode = (forecast_method or AUTO).lower()
    segment = classify.classify(series)
    if mode in _FORCED_METHODS:
        return mode, segment
    if segment_method is None and segment in SEGMENT_TSB.get(scope, {}):
        return "tsb", segment
    return method_for_segment(segment, segment_method), segment


def auto_rate(
    series: list[float],
    segment: str,
    scope: str = SCOPE_CLIENT,
    open_month_sold: bool = False,
) -> tuple[float, str]:
    if scope not in SEGMENT_EWMA_ALPHA:
        raise ValueError(f"unknown forecast scope: {scope}")
    constants = SEGMENT_TSB[scope].get(segment)
    if constants is not None:
        alpha, beta = constants
        rate = demand.tsb_rate(series, alpha, beta, open_month_sold)
        return rate, f"tsb_a{alpha:g}_b{beta:g}"
    alpha = SEGMENT_EWMA_ALPHA[scope].get(segment, demand.EWMA_ALPHA)
    return demand.ewma_rate(series, alpha), method_label("ewma", alpha)


def horizon_shape(forecast_method: str) -> str:
    mode = (forecast_method or AUTO).lower()
    return HORIZON_DAMPED_TREND if mode == "damped_trend" else HORIZON_FLAT


def method_label(method: str, ewma_alpha: float) -> str:
    return f"ewma_a{ewma_alpha:g}" if method == "ewma" else method
