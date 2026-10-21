from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.config import get_settings
from app.core.history import coverage
from app.core.serving_context import (
    HISTORICAL_SCORE_REASON,
    ServingContext,
    observation_fields,
    resolve_current_context,
)
from app.data import solvency_repository as repo
from app.domain.models import (
    AgingBar,
    DonutSlice,
    GaugeChart,
    SolvencyCharts,
    TrendPoint,
    TurnoverExposurePoint,
)
from app.domain.money import round_cent
from app.services.solvency import score as scoring

_AGING_ORDER = {"0-30": 0, "31-60": 1, "61-90": 2, "90+": 3}

# Only current chart queries use the pool; historical scores are not reconstructed.
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
            amount_eur=round_cent(by_bucket.get(label, {}).get("amount_eur", 0)),
        )
        for label in sorted(_AGING_ORDER, key=_AGING_ORDER.get)
    ]


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
            turnover_eur=round_cent(m.get("turnover_eur", 0)),
            exposure_eur=round_cent(exposure),
        )
        for m in monthly
    ]


def _turnover_trend(monthly: list[dict]) -> list[TrendPoint]:
    return [
        TrendPoint(period=m["period"], turnover_eur=round_cent(m.get("turnover_eur", 0)))
        for m in monthly
    ]


# Keep current-query builds within the existing connection budget.
_BUILD_SEMAPHORE = threading.Semaphore(1)


def build_charts(
    client_id: int,
    as_of_date: str | None,
    window_months: int,
    *,
    context: ServingContext | None = None,
) -> SolvencyCharts:
    # The service passes the context captured before its cache/client lookup. Do not read
    # the clock again after waiting for the semaphore, especially across Kyiv midnight.
    context = context or resolve_current_context(as_of_date, window_months, scoring=False)
    with _BUILD_SEMAPHORE:
        return _build_charts_locked(client_id, context, window_months)


def _build_charts_locked(
    client_id: int, context: ServingContext, window_months: int,
) -> SolvencyCharts:
    settings = get_settings()
    as_of = context.as_of
    history = coverage(as_of, window_months)
    fx_date = context.fx_date

    # Transaction charts keep their display window. Current debt and current agreement
    # terms cannot prove past feature states, so no monthly feature/scorer calls are made.
    with ThreadPoolExecutor(max_workers=_DIRECT_WORKERS) as pool:
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

    return SolvencyCharts(
        client_id=client_id,
        limit_utilization_gauge=_gauge(agreements),
        payment_discipline_donut=_donut(exposure),
        open_invoice_aging_bars=_aging_bars(aging),
        turnover_vs_exposure=_turnover_vs_exposure(
            monthly, overdue, int(open_stats.get("open_count", 0)), total_sales
        ),
        score_sparkline=[],
        score_sparkline_status="unavailable",
        score_sparkline_reason_code="historical_state_not_recorded",
        score_sparkline_reason=HISTORICAL_SCORE_REASON,
        turnover_trend=_turnover_trend(monthly),
        aging_over_time_heatmap="pending",
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
        as_of_date=as_of,
        **observation_fields(as_of),
        model_version=settings.model_version,
        window_months=window_months,
    )
