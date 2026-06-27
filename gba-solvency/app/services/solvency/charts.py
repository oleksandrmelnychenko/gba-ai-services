from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from app.core.config import get_settings
from app.data import solvency_repository as repo
from app.domain.models import (
    AgingBar,
    DonutSlice,
    GaugeChart,
    ScorePoint,
    SolvencyCharts,
    TrendPoint,
    TurnoverExposurePoint,
)
from app.risk import dataset as risk_dataset
from app.risk.score_current import score_current
from app.services.solvency import score as scoring

_AGING_ORDER = {"0-30": 0, "31-60": 1, "61-90": 2, "90+": 3}

# Concurrency caps for the chart fan-out. The sparkline pool (_MAX_DB_WORKERS) carries the heavy
# per-month score computations; the direct-query pool (_DIRECT_WORKERS) carries the handful of fast
# chart queries that run alongside it.
#
# Each sparkline month now runs the v3 serving path (features_one -> score_current), and
# features_one itself fans its 6 feature-group queries over an internal pool (so up to
# _FEATURES_ONE_FANOUT pooled connections per month, vs the legacy compute_score's sequential
# 1-connection-at-a-time). To keep the whole chart build under the engine's connection ceiling
# (pool_size + max_overflow = 20), the sparkline pool is sized so that
#   _DIRECT_WORKERS + _MAX_DB_WORKERS * _FEATURES_ONE_FANOUT <= 20  (6 + 2*6 = 18),
# leaving headroom and never starving the pool for other concurrent requests.
_FEATURES_ONE_FANOUT = 6
_MAX_DB_WORKERS = 2
_DIRECT_WORKERS = 6


def _gauge(agreements: list[dict]) -> GaugeChart:
    util = scoring.max_limit_utilization(agreements)
    return GaugeChart(value=round(util, 4) if util is not None else 0.0)


def _donut(exposure: dict[str, int]) -> list[DonutSlice]:
    """TRUTH-based discipline donut from settlement reality (Debt/ClientInDebt), replacing the
    stale BaseSalePaymentStatus.NotPaid split that showed ~93% unpaid for nearly every client.
    Slices keep the {label, count} donut shape: settled (paid) / current (within grace) / overdue.
    """
    merged = {
        "settled": int(exposure.get("settled", 0)),
        "current": int(exposure.get("current", 0)),
        "overdue": int(exposure.get("overdue", 0)),
    }
    return [DonutSlice(label=k, count=v) for k, v in merged.items()]


def _aging_bars(buckets: list[dict]) -> list[AgingBar]:
    """TRUTH overdue aging from real Debt lines (count + EUR amount per bucket)."""
    by_bucket = {b["bucket"]: b for b in buckets}
    return [
        AgingBar(
            bucket=label,
            count=int(by_bucket.get(label, {}).get("count", 0)),
            amount_eur=round(float(by_bucket.get(label, {}).get("amount_eur", 0.0)), 2),
        )
        for label in sorted(_AGING_ORDER, key=_AGING_ORDER.get)
    ]


def _month_floor(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _turnover_vs_exposure(
    monthly: list[dict],
    overdue_eur: float,
    open_unpaid_count: int,
    total_sales_count: int,
) -> list[TurnoverExposurePoint]:
    """Per-month turnover vs a flat current-exposure reference line (point-in-time exposure,
    since per-month historical exposure needs the Debt aging-over-time data that is still pending).
    """
    exposure = overdue_eur
    if exposure <= 0 and total_sales_count > 0 and open_unpaid_count > 0:
        exposure = 0.0
    return [
        TurnoverExposurePoint(
            period=m["period"],
            turnover_eur=round(float(m.get("turnover_eur", 0.0)), 2),
            exposure_eur=round(exposure, 2),
        )
        for m in monthly
    ]


def _turnover_trend(monthly: list[dict]) -> list[TrendPoint]:
    return [
        TrendPoint(period=m["period"], turnover_eur=round(float(m.get("turnover_eur", 0.0)), 2))
        for m in monthly
    ]


def _sparkline_point(client_id: int, as_of: str, window_months: int, i: int) -> ScorePoint:
    """One point-in-time v3 score for the i-th month of the sparkline window.

    Computes the SAME supervised current-state scorecard the headline /score uses
    (features_one -> score_current), only as-of each historical month instead of today, so the
    sparkline is a real v3 score trajectory — NOT the legacy stale-discipline curve.

    Pure function of (client_id, as_of, window_months, i): the month grid and the per-point as-of
    (first day of the month AFTER period_month, clamped to `base`) are unchanged from the old loop
    body, so the i->period mapping and the final point's as-of (== `base`, matching the live
    /score) are preserved; only the data source changed. The points are bit-identical whether
    computed serially or fanned out.
    """
    base = datetime.strptime(as_of, "%Y-%m-%d").date()
    start = _month_floor(_add_months(base, -(window_months - 1)))
    period_month = _add_months(start, i)
    next_month = _add_months(period_month, 1)
    point_as_of = (next_month.replace(day=1)).isoformat()
    if datetime.strptime(point_as_of, "%Y-%m-%d").date() > base:
        point_as_of = base.isoformat()
    feats = risk_dataset.features_one(client_id, point_as_of, window_months)
    score = int(round(score_current(feats)["score"]))
    return ScorePoint(period=period_month.strftime("%Y-%m"), score=score)


def _score_sparkline(client_id: int, as_of: str, window_months: int) -> list[ScorePoint]:
    """Recompute the v3 current-state score month-by-month across the window.

    Each month is an independent point-in-time feature build + scorecard pass (features_one fans
    its own 6 group queries over a small pool internally), so the months are fanned out over a
    dedicated thread pool instead of run sequentially — this is the dominant cost of the chart
    build. `pool.map` preserves input order, so the emitted list is identical to the serial order.
    """
    workers = max(1, min(_MAX_DB_WORKERS, window_months))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(
                lambda i: _sparkline_point(client_id, as_of, window_months, i),
                range(window_months),
            )
        )


def build_charts(client_id: int, as_of_date: str | None, window_months: int) -> SolvencyCharts:
    settings = get_settings()
    as_of = as_of_date or settings.resolve_fx_date(None)
    fx_date = settings.resolve_fx_date(as_of_date)

    # The direct chart queries and the score-sparkline are independent and each open their own
    # pooled connection, so they are fanned out concurrently instead of run sequentially. The
    # handful of direct queries share one pool; the sparkline (the dominant cost — window_months
    # full score computations) runs alongside on its own dedicated pool via _score_sparkline, so
    # the two fan-outs overlap. Keeping the sparkline behind the _score_sparkline seam (rather
    # than inlining its tasks here) preserves it as a single replaceable unit and avoids nesting
    # a pool inside a pool task. Only the access path changes — the per-query SQL and the result
    # wiring below are untouched, so the output is bit-identical to the serial version.
    #
    # `overdue_amount_eur` is the lone dependent query (it only runs when the debt sync is live),
    # so it is resolved after sync_live is known; it is a single fast point query.
    #
    # The discipline donut and the open-invoice aging bars are both sourced from settlement
    # reality (Debt/ClientInDebt via debt_exposure_donut_truth / debt_aging_buckets_truth) — the
    # SAME source the model's aging features and overdue_amount_eur use — NOT the stale
    # BaseSalePaymentStatus.NotPaid enum (only 3.3% of NotPaid sales have a live debt row, so the
    # old enum donut read ~93% unpaid and the enum aging was ~30x inflated for nearly every client).
    #
    # Connection budget: the direct pool (<= _DIRECT_WORKERS) plus the sparkline pool, where each
    # sparkline month may hold up to _FEATURES_ONE_FANOUT connections (features_one's own internal
    # fan-out), stay under the engine's pool ceiling — see the _MAX_DB_WORKERS sizing note above
    # (_DIRECT_WORKERS + _MAX_DB_WORKERS * _FEATURES_ONE_FANOUT <= pool_size + max_overflow = 20).
    with ThreadPoolExecutor(max_workers=_DIRECT_WORKERS + 1) as pool:
        f_spark = pool.submit(_score_sparkline, client_id, as_of, window_months)
        f_donut = pool.submit(repo.debt_exposure_donut_truth, client_id, as_of, window_months,
                              fx_date)
        f_agreements = pool.submit(repo.credit_limit_utilization, client_id)
        f_aging = pool.submit(repo.debt_aging_buckets_truth, client_id, as_of, fx_date)
        f_monthly = pool.submit(repo.monthly_turnover_series, client_id, as_of, window_months,
                                fx_date)
        f_sync = pool.submit(repo.debt_sync_is_live)
        f_open = pool.submit(repo.open_unpaid_stats, client_id, as_of, window_months)
        f_total = pool.submit(repo.total_sales_count, client_id, as_of, window_months)

        sync_live = f_sync.result()
        overdue = (
            repo.overdue_amount_eur(client_id, as_of, fx_date) if sync_live else 0.0
        )

        exposure = f_donut.result()
        agreements = f_agreements.result()
        aging = f_aging.result()
        monthly = f_monthly.result()
        open_stats = f_open.result()
        total_sales = f_total.result()
        sparkline = f_spark.result()

    return SolvencyCharts(
        client_id=client_id,
        limit_utilization_gauge=_gauge(agreements),
        payment_discipline_donut=_donut(exposure),
        open_invoice_aging_bars=_aging_bars(aging),
        turnover_vs_exposure=_turnover_vs_exposure(
            monthly, overdue, int(open_stats.get("open_count", 0)), total_sales
        ),
        score_sparkline=sparkline,
        turnover_trend=_turnover_trend(monthly),
        aging_over_time_heatmap="pending",
        as_of_date=as_of,
        window_months=window_months,
    )
