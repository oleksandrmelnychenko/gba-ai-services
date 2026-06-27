"""ABC × XYZ × lifecycle classification. Pure logic (DB-free) + a monthly-series helper.

XYZ/lifecycle are per-product (computed from the demand series); ABC is portfolio-relative
(cumulative trailing revenue share) and is assigned by the portfolio builder after ranking.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime

from app.core.config import Settings
from app.domain.models import AbcClass, LifecycleStage, XyzClass


def month_labels(as_of: str, months: int) -> list[str]:
    """The `months` trailing 'YYYY-MM' labels ending in as_of's month (oldest first)."""
    d = datetime.strptime(as_of, "%Y-%m-%d").date()
    out: list[str] = []
    y, m = d.year, d.month
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def series_from(units_by_ym: dict[str, float], labels: list[str]) -> list[float]:
    """Dense monthly series over labels; missing months are 0 (intermittent demand counts)."""
    return [float(units_by_ym.get(lbl, 0.0)) for lbl in labels]


def demand_cv(series: list[float]) -> float:
    """Coefficient of variation of the dense monthly series (classic XYZ measure).

    0 when there is no demand. Over an intermittent B2B grid (mostly zeros) this saturates
    high and collapses almost everything to Z — see `interval_demand_cv` for the alternative.
    """
    if not series:
        return 0.0
    mean = statistics.fmean(series)
    if mean <= 0:
        return 0.0
    return statistics.pstdev(series) / mean


def interval_demand_cv(series: list[float]) -> float:
    """Croston-style CV of inter-demand intervals (gaps between consecutive non-zero months).

    Measures the REGULARITY of demand cadence rather than the size of the zero-runs, so a SKU
    that sells a steady trickle on a fixed rhythm reads as stable even though its monthly grid
    is full of zeros. Fewer than two demand months cannot establish a cadence, so we return a
    sentinel above any reasonable cut (-> Z: a single/no sale in the year IS intermittent).
    """
    idx = [i for i, u in enumerate(series) if u > 0]
    if len(idx) < 2:
        return float("inf")
    gaps = [idx[i + 1] - idx[i] for i in range(len(idx) - 1)]
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return 0.0
    sd = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
    return sd / mean


def demand_variability(series: list[float], cfg: Settings) -> float:
    """The XYZ variability statistic selected by `cfg.xyz_method` ('cv' | 'interval')."""
    if cfg.xyz_method == "interval":
        return interval_demand_cv(series)
    return demand_cv(series)


def xyz_classify(variability: float, cfg: Settings) -> XyzClass:
    """Map a variability statistic (from `demand_variability`) to X/Y/Z via the configured cuts."""
    if variability < cfg.xyz_x_cut:
        return XyzClass.X
    if variability < cfg.xyz_y_cut:
        return XyzClass.Y
    return XyzClass.Z


def lifecycle_classify(days_since_first: int | None, recent_units: float, prior_units: float,
                       sold_in_dead_window: bool, cfg: Settings,
                       months_since_last: int | None = None) -> LifecycleStage:
    """Lifecycle stage from a recent-vs-prior demand split.

    DECLINING is reserved for genuine decline: either a downward trend with demand still in the
    recent window, or a long dormancy (last sale >= cfg.lifecycle_dormant_decline_months ago).
    A SKU with no recent demand but a sale earlier in the window and only a short lull is MATURE,
    not DECLINING — this is what fixes the old recent=0/prior=0 catch-all that dumped every
    "sold-earlier-this-year" SKU into DECLINING.

    `months_since_last` is months since the most recent sale (0 = current month). When omitted
    the dormancy split cannot be made and the function falls back to the legacy recent/prior rule.
    """
    if not sold_in_dead_window:
        return LifecycleStage.DEAD
    if days_since_first is not None and days_since_first <= cfg.lifecycle_new_days:
        return LifecycleStage.NEW
    if recent_units <= 0:
        # No demand in the trend window. Long fade => DECLINING; recent lull => MATURE.
        if months_since_last is None:
            return LifecycleStage.GROWING if recent_units > 0 else LifecycleStage.DECLINING
        return (LifecycleStage.DECLINING
                if months_since_last >= cfg.lifecycle_dormant_decline_months
                else LifecycleStage.MATURE)
    if prior_units <= 0:
        return LifecycleStage.GROWING
    if recent_units >= prior_units * cfg.lifecycle_growing_factor:
        return LifecycleStage.GROWING
    if recent_units <= prior_units * cfg.lifecycle_declining_factor:
        return LifecycleStage.DECLINING
    return LifecycleStage.MATURE


def months_since_last_sale(series: list[float]) -> int | None:
    """Months since the most recent non-zero month (0 = last/current month); None if never sold."""
    idx = [i for i, u in enumerate(series) if u > 0]
    return None if not idx else (len(series) - 1 - idx[-1])


def lifecycle_from_series(series: list[float], days_since_first: int | None,
                          sold_in_dead_window: bool, cfg: Settings) -> LifecycleStage:
    """Series-first lifecycle: splits recent/prior over cfg.lifecycle_trend_months and supplies
    the dormancy signal. This is the entry point callers should use (computes the split for you).
    """
    recent, prior = split_recent_prior(series, cfg.lifecycle_trend_months)
    return lifecycle_classify(days_since_first, recent, prior, sold_in_dead_window, cfg,
                              months_since_last=months_since_last_sale(series))


def abc_classify(cumulative_share: float, cfg: Settings) -> AbcClass:
    if cumulative_share <= cfg.abc_a_share:
        return AbcClass.A
    if cumulative_share <= cfg.abc_b_share:
        return AbcClass.B
    return AbcClass.C


def split_recent_prior(series: list[float], recent_months: int) -> tuple[float, float]:
    """Sum of the last `recent_months` vs the preceding `recent_months` (for the lifecycle trend)."""
    recent = sum(series[-recent_months:]) if recent_months > 0 else 0.0
    prior = sum(series[-2 * recent_months:-recent_months]) if recent_months > 0 else 0.0
    return recent, prior


def days_since(d: date | datetime | None, as_of: str) -> int | None:
    if d is None:
        return None
    ref = datetime.strptime(as_of, "%Y-%m-%d").date()
    dd = d.date() if isinstance(d, datetime) else d
    return (ref - dd).days
