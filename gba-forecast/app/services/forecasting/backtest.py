"""Point-in-time accuracy backtest — the forecaster's analog of solvency's missing AUC.

A forecaster has no single label to score against (it is not a classifier), so the correct
validation is OUT-OF-SAMPLE FORECAST ACCURACY measured by rolling-origin evaluation:

  For each origin month T in the trailing evaluation window, forecast months T+1..T+H using
  ONLY the dense history up to and including T, then compare each forecast to the ACTUAL
  realized value of that future month. No future data ever leaks into a forecast — every
  origin is a genuine point-in-time replay of what the live service would have produced.

Metrics (per method, aggregated per demand SEGMENT):

  MAE   = mean |forecast - actual|.  Absolute EUR error; robust, scale-dependent.
  sMAPE = mean( |f-a| / ((|f|+|a|)/2) ).  We use SYMMETRIC MAPE, not plain MAPE, because the
          series are intermittent: actual = 0 months are common, and plain MAPE divides by
          the actual and is undefined / explodes to infinity on a zero actual. sMAPE divides
          by the average magnitude of forecast and actual, stays finite (capped at 2.0 when
          one side is 0), and does not blow up on the zero months that dominate intermittent
          B2B demand. That is exactly why sMAPE is the standard accuracy metric for
          intermittent-demand studies.
  BIAS  = mean (forecast - actual).  Signed; positive => the method OVER-forecasts (the
          symptom SBA is designed to cure on Croston), negative => it UNDER-forecasts.

The series segment is the Syntetos-Boylan quadrant of the FULL series (classify.classify),
so accuracy is reported per smooth / erratic / intermittent / lumpy bucket — the table the
method selector is built on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.forecasting import classify, demand

# Methods the backtest evaluates head-to-head. Includes the trend/recency-aware candidates
# (ewma, damped_trend) added to attack the measured negative bias on smooth/erratic series
# (smooth adopts EWMA on the corrected data; erratic's lowest MAE is the window mean).
METHODS = ("moving_avg", "croston", "sba", "ewma", "damped_trend")


@dataclass
class Accumulator:
    """Running error sums for one (method, segment) cell. Aggregates over all forecast steps."""

    n: int = 0
    abs_err_sum: float = 0.0
    smape_sum: float = 0.0
    signed_err_sum: float = 0.0

    def add(self, forecast: float, actual: float) -> None:
        err = forecast - actual
        self.n += 1
        self.abs_err_sum += abs(err)
        self.signed_err_sum += err
        self.smape_sum += _smape_point(forecast, actual)

    @property
    def mae(self) -> float:
        return self.abs_err_sum / self.n if self.n else 0.0

    @property
    def smape(self) -> float:
        return self.smape_sum / self.n if self.n else 0.0

    @property
    def bias(self) -> float:
        return self.signed_err_sum / self.n if self.n else 0.0

    def summary(self) -> dict[str, float]:
        return {"n": self.n, "mae": self.mae, "smape": self.smape, "bias": self.bias}


@dataclass
class BacktestResult:
    """Per-(method, segment) accuracy plus a per-method overall (segment-agnostic) roll-up."""

    by_method_segment: dict[tuple[str, str], Accumulator] = field(default_factory=dict)
    by_method: dict[str, Accumulator] = field(default_factory=dict)
    segment_counts: dict[str, int] = field(default_factory=dict)
    n_series: int = 0
    n_origins: int = 0

    def cell(self, method: str, segment: str) -> Accumulator:
        return self.by_method_segment.setdefault((method, segment), Accumulator())

    def overall(self, method: str) -> Accumulator:
        return self.by_method.setdefault(method, Accumulator())


def _smape_point(forecast: float, actual: float) -> float:
    """Symmetric absolute percentage error for one point, in [0, 2]. 0 when both are 0."""
    denom = (abs(forecast) + abs(actual)) / 2.0
    if denom == 0:
        return 0.0
    return abs(forecast - actual) / denom


def _path_for(series: list[float], method: str, alpha: float, horizon: int) -> list[float]:
    """Per-step horizon path for a method on a (sub)series (the live projection contract).

    Mirrors demand.forecast_path so the backtest scores exactly what the service would emit:
    flat methods repeat their rate, damped_trend varies per step. Every value is >= 0.
    """
    return demand.forecast_path(series, horizon, method, alpha)


def backtest_series(
    series: list[float],
    result: BacktestResult,
    *,
    horizon: int,
    eval_origins: int,
    min_train: int,
    alpha: float,
) -> bool:
    """Rolling-origin replay of one dense monthly series into `result`.

    For the last `eval_origins` origin months (each needing >= `min_train` months of history
    before it and at least one realized month after it), forecast the next 1..`horizon`
    months with each method using only data <= the origin, and score every forecast against
    the realized actual. The whole series' Syntetos-Boylan segment labels every error it
    contributes. Returns True if the series produced at least one scored forecast.
    """
    n = len(series)
    segment = classify.classify(series)
    # Valid origins T (0-based index into series): need min_train months up to T inclusive,
    # and at least one realized month after T to compare against.
    last_origin = n - 2  # need series[T+1] to exist
    first_origin = max(min_train - 1, 0)
    if last_origin < first_origin:
        return False
    origins = list(range(first_origin, last_origin + 1))
    if eval_origins > 0:
        origins = origins[-eval_origins:]

    scored = False
    result.n_series += 1
    result.segment_counts[segment] = result.segment_counts.get(segment, 0) + 1
    for t in origins:
        train = series[: t + 1]
        actuals = series[t + 1 : t + 1 + horizon]
        if not actuals:
            continue
        result.n_origins += 1
        for method in METHODS:
            # Per-step path (the live contract): flat methods repeat their rate, damped_trend
            # varies per step. Score forecast step h against the realized actual at step h.
            path = _path_for(train, method, alpha, len(actuals))
            for forecast, actual in zip(path, actuals, strict=True):
                result.cell(method, segment).add(forecast, float(actual))
                result.overall(method).add(forecast, float(actual))
                scored = True
    return scored


def best_method_per_segment(
    result: BacktestResult, by: str = "mae"
) -> dict[str, str]:
    """Pick, per segment, the method with the lowest aggregate error (`by` = 'mae'|'smape').

    Only considers segments/methods that actually accumulated forecasts. The result is a
    segment -> method map suitable to hand to selection.method_for_segment as an empirical
    override of the literature prior.
    """
    metric = "mae" if by not in {"mae", "smape"} else by
    best: dict[str, tuple[str, float]] = {}
    for (method, segment), acc in result.by_method_segment.items():
        if acc.n == 0:
            continue
        score = getattr(acc, metric)
        if segment not in best or score < best[segment][1]:
            best[segment] = (method, score)
    return {segment: method for segment, (method, _) in best.items()}


def overall_mae(result: BacktestResult, method: str) -> float:
    """Segment-agnostic MAE for one method (the baseline a fixed default would achieve)."""
    acc = result.by_method.get(method)
    return acc.mae if acc else 0.0
