from __future__ import annotations

from datetime import date, datetime

from app.core.config import get_settings
from app.data import solvency_repository as repo
from app.domain.models import (
    AgingBar,
    DonutSlice,
    ExposureSource,
    GaugeChart,
    ScorePoint,
    SolvencyCharts,
    TrendPoint,
    TurnoverExposurePoint,
)
from app.services.solvency import score as scoring

_AGING_ORDER = {"0-30": 0, "31-60": 1, "61-90": 2, "90+": 3}


def _gauge(agreements: list[dict]) -> GaugeChart:
    util = scoring.max_limit_utilization(agreements)
    if util is None:
        return GaugeChart(value=None, has_controlled_limit=False)
    return GaugeChart(value=round(util, 4), has_controlled_limit=True)


def _donut(regular: dict[str, int], retail: dict[str, int]) -> list[DonutSlice]:
    merged = {
        "paid": regular.get("paid", 0) + retail.get("paid", 0),
        "overpaid": regular.get("overpaid", 0),
        "partial": regular.get("partial", 0) + retail.get("partial", 0),
        "notpaid": regular.get("notpaid", 0) + retail.get("notpaid", 0),
        "refund": regular.get("refund", 0),
    }
    return [DonutSlice(label=k, count=v) for k, v in merged.items()]


def _aging_bars(buckets: list[dict]) -> list[AgingBar]:
    by_bucket = {b["bucket"]: int(b.get("count", 0)) for b in buckets}
    return [
        AgingBar(bucket=label, count=by_bucket.get(label, 0))
        for label in sorted(_AGING_ORDER, key=_AGING_ORDER.get)
    ]


def _month_floor(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _turnover_vs_exposure(
    monthly: list[dict],
    exposure_eur: float | None,
    exposure_source: ExposureSource,
) -> list[TurnoverExposurePoint]:
    """Per-month turnover vs a flat current-exposure reference line (point-in-time exposure,
    since per-month historical exposure needs the Debt aging-over-time data that is still pending).

    When Debt sync is not live, the score uses the live open-unpaid-count proxy for debt load, but
    that proxy is not an EUR exposure amount. Return exposure_eur=None so the UI can hide/label the
    line instead of reading a fabricated zero as "no exposure".
    """
    return [
        TurnoverExposurePoint(
            period=m["period"],
            turnover_eur=round(float(m.get("turnover_eur", 0.0)), 2),
            exposure_eur=round(exposure_eur, 2) if exposure_eur is not None else None,
            exposure_source=exposure_source,
        )
        for m in monthly
    ]


def _turnover_trend(monthly: list[dict]) -> list[TrendPoint]:
    return [
        TrendPoint(period=m["period"], turnover_eur=round(float(m.get("turnover_eur", 0.0)), 2))
        for m in monthly
    ]


def _score_sparkline(client_id: int, as_of: str, window_months: int) -> list[ScorePoint]:
    """Recompute the score month-by-month across the window (trailing 12-month each point)."""
    base = datetime.strptime(as_of, "%Y-%m-%d").date()
    start = _month_floor(_add_months(base, -(window_months - 1)))
    points: list[ScorePoint] = []
    for i in range(window_months):
        period_month = _add_months(start, i)
        next_month = _add_months(period_month, 1)
        point_as_of = (next_month.replace(day=1)).isoformat()
        if datetime.strptime(point_as_of, "%Y-%m-%d").date() > base:
            point_as_of = base.isoformat()
        comp = scoring.compute_score(client_id, point_as_of, window_months)
        points.append(ScorePoint(period=period_month.strftime("%Y-%m"), score=comp.score))
    return points


def build_charts(client_id: int, as_of_date: str | None, window_months: int) -> SolvencyCharts:
    settings = get_settings()
    as_of = as_of_date or settings.resolve_fx_date(None)
    fx_date = settings.resolve_fx_date(as_of_date)

    regular = repo.payment_status_counts(client_id, as_of, window_months)
    retail = repo.retail_payment_status_counts(client_id, as_of, window_months)
    agreements = repo.credit_limit_utilization(client_id)
    aging = repo.open_unpaid_aging_buckets(client_id, as_of, window_months)
    monthly = repo.monthly_turnover_series(client_id, as_of, window_months, fx_date)

    sync_live = repo.debt_sync_is_live()
    exposure_source = ExposureSource.DEBT_TABLE if sync_live else ExposureSource.UNAVAILABLE
    exposure_eur = repo.overdue_amount_eur(client_id, as_of, fx_date) if sync_live else None

    return SolvencyCharts(
        client_id=client_id,
        limit_utilization_gauge=_gauge(agreements),
        payment_discipline_donut=_donut(regular, retail),
        open_invoice_aging_bars=_aging_bars(aging),
        turnover_vs_exposure=_turnover_vs_exposure(
            monthly, exposure_eur, exposure_source
        ),
        score_sparkline=_score_sparkline(client_id, as_of, window_months),
        turnover_trend=_turnover_trend(monthly),
        aging_over_time_heatmap="pending",
        as_of_date=as_of,
        window_months=window_months,
    )
