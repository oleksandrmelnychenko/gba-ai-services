"""Per-segment forecast-method selection.

Replaces a single fixed FORECAST_METHOD with a principled choice driven by the demand
pattern of each series. The map below is the BACKTEST-PROVEN choice per segment, re-derived on
the CORRECTED full-sales data (rolling-origin evaluation over 1000 real client+product series,
scripts/run_backtest.py). The earlier map was tuned on a buggy ~16% sample (the old
`o.Deleted = 0` validity filter dropped ~84% of real sales); on the full IsValidForCurrentSale=1
data the erratic winner moves, while smooth and the sporadic quadrants hold:

  - smooth                -> EWMA  (these EUR series trend UPWARD, so a backward-looking window
                                   mean structurally LAGS the growth and under-forecasts. EWMA
                                   weights recent months more, tracking the drift. On the
                                   corrected sample it beats moving_avg on MAE, sMAPE AND bias:
                                   MAE 1400->1197 (-15%), bias -1083->-182 (-83%). The chronic
                                   negative (under-forecast) bias is the symptom EWMA cures.)
  - erratic               -> moving_avg  (CHANGED from EWMA. On the corrected full data the
                                   window mean has the LOWEST MAE for erratic in every split
                                   (combined 849 vs EWMA 887, +4.5%; clients-only and
                                   products-only agree on the direction), so the MAE-keyed
                                   selection rule — the backtest's documented primary metric —
                                   picks it. EWMA still wins erratic on sMAPE (0.792 vs 0.803)
                                   and slashes the bias (-37 vs -357); that trade-off is real,
                                   but the selector follows MAE for consistency, so erratic
                                   reverts to the window mean here.)
  - intermittent / lumpy  -> SBA  (Croston is the textbook intermittent forecaster; SBA removes
                                   Croston's documented positive bias. On the corrected data —
                                   which finally exposes a real intermittent/lumpy population the
                                   buggy 16% sample lacked — SBA/Croston keep the lowest MAE on
                                   both sporadic quadrants (intermittent 1429, lumpy 624) while
                                   trend/recency methods HURT them, so the literature prior holds.
                                   SBA is kept over Croston: marginally higher MAE but the
                                   bias-corrected variant is the principled intermittent choice.)
  - no_demand             -> moving_avg  (returns 0.0 either way; cheap and safe).

This map is what the backtest measures and, where the data disagrees, overrides: run_backtest
emits an empirically-tuned map, but this default is the already-validated choice so the service
is correct out of the box with no calibration step.

`forecast_method` in Settings selects the MODE:
  "auto"                                   -> use this per-segment selector (recommended).
  "moving_avg"|"croston"|"sba"|"ewma"|"damped_trend"
                                           -> force that single method for every series (A-B).
The response contract is unchanged: the selector only chooses which projection function runs.
"""

from __future__ import annotations

from app.services.forecasting import classify

# Backtest-proven segment -> method map (re-derived on the CORRECTED full-sales data). Methods
# are the demand.forecast_path dispatch names. smooth adopts EWMA (lowest MAE + near-zero bias);
# erratic reverts to moving_avg (lowest MAE on the corrected data, in every client/product split);
# intermittent/lumpy keep SBA (trend methods raise their MAE on the now-real sporadic population).
DEFAULT_SEGMENT_METHOD: dict[str, str] = {
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


def select_method(
    series: list[float],
    forecast_method: str,
    segment_method: dict[str, str] | None = None,
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
    return method_for_segment(segment, segment_method), segment
