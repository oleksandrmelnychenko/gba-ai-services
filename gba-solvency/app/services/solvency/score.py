from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.data import solvency_repository as repo
from app.domain.models import (
    CapType,
    DebtLoadSource,
    Rating,
    SubFactor,
    SubFactors,
)

W_DISCIPLINE = 0.35
W_DEBT_LOAD = 0.25
W_ACTIVITY = 0.20
W_TENURE = 0.10
W_RETURN_QUALITY = 0.10

RECENCY_HORIZON_DAYS = 90.0
FREQUENCY_TARGET_ORDERS = 24.0
TENURE_TARGET_MONTHS = 24.0

UTILIZATION_HARD_THRESHOLD = 1.0
UTILIZATION_SOFT_THRESHOLD = 0.9
UTILIZATION_HARD_CAP = 40
UTILIZATION_SOFT_CAP = 60
BLOCKED_MULTIPLIER = 0.5

DEBT_LOAD_HEALTHY_FLOOR = 0.25
DEBT_LOAD_TAIL_SPAN = 1.25

BAND_A_MIN = 80
BAND_B_MIN = 65
BAND_C_MIN = 45


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _sub_factor(value: float, weight: float) -> SubFactor:
    v = _clamp(value)
    return SubFactor(value=v, points=round(weight * v * 100.0, 4), weight=weight)


def discipline_value(regular: dict[str, int], retail: dict[str, int]) -> float:
    """(1) PaymentDiscipline = (paid + overpaid + 0.5*partial) / (paid+overpaid+partial+notpaid).

    Refund is EXCLUDED entirely. Regular and retail counts are merged after each enum is mapped
    by its own status type in the repository (never one enum for both sale types).
    """
    paid = regular.get("paid", 0) + retail.get("paid", 0)
    overpaid = regular.get("overpaid", 0)
    partial = regular.get("partial", 0) + retail.get("partial", 0)
    notpaid = regular.get("notpaid", 0) + retail.get("notpaid", 0)
    denom = paid + overpaid + partial + notpaid
    if denom <= 0:
        return 1.0
    return (paid + overpaid + 0.5 * partial) / denom


def debt_load_value(
    sync_live: bool,
    overdue_eur: float,
    turnover_eur: float,
    open_unpaid_count: int,
    total_sales_count: int,
) -> float:
    """(2) Sync-aware DebtLoad.

    Debt table live  -> clamp(1 - max(0, ratio - FLOOR) / SPAN, 0, 1)
                        where ratio = overdue_eur / turnover_eur, FLOOR = DEBT_LOAD_HEALTHY_FLOOR,
                        SPAN = DEBT_LOAD_TAIL_SPAN. The first FLOOR of overdue-vs-turnover is
                        treated as healthy carried trade credit; debt_load decays linearly to 0
                        only at ratio = FLOOR + SPAN, so the ratio>=1 tail keeps spread.
    Debt sync-blocked -> clamp(1 - open_unpaid_count / total_sales_count, 0, 1)
    """
    if sync_live:
        if turnover_eur <= 0:
            return 1.0 if overdue_eur <= 0 else 0.0
        ratio = overdue_eur / turnover_eur
        return _clamp(1.0 - max(0.0, ratio - DEBT_LOAD_HEALTHY_FLOOR) / DEBT_LOAD_TAIL_SPAN)
    if total_sales_count <= 0:
        return 1.0
    return _clamp(1.0 - open_unpaid_count / total_sales_count)


def activity_value(order_count: int, recency_days: int | None) -> float:
    """(3) Activity = 0.5*recency_factor + 0.5*frequency_factor."""
    recency_factor = (
        0.0 if recency_days is None else _clamp(1.0 - recency_days / RECENCY_HORIZON_DAYS)
    )
    frequency_factor = _clamp(order_count / FREQUENCY_TARGET_ORDERS)
    return 0.5 * recency_factor + 0.5 * frequency_factor


def tenure_value(tenure_months: int) -> float:
    """(4) Tenure = clamp(tenure_months/24, 0, 1)."""
    return _clamp(tenure_months / TENURE_TARGET_MONTHS)


def return_quality_value(return_qty_rate: float) -> float:
    """(5) ReturnQuality = clamp(1 - return_qty_rate*2, 0, 1)."""
    return _clamp(1.0 - return_qty_rate * 2.0)


def band_for(score: int) -> Rating:
    if score >= BAND_A_MIN:
        return Rating.A
    if score >= BAND_B_MIN:
        return Rating.B
    if score >= BAND_C_MIN:
        return Rating.C
    return Rating.D


def max_limit_utilization(agreements: list[dict]) -> float | None:
    """Worst (highest) utilization across the client's controlled agreements (gauge + caps)."""
    utils = [
        a["limit_utilization"]
        for a in agreements
        if a.get("is_control_amount")
        and a.get("amount_debt", 0.0) > 0
        and a.get("limit_utilization") is not None
    ]
    return max(utils) if utils else None


def apply_caps(
    raw_score: float,
    max_utilization: float | None,
    is_blocked: bool,
) -> tuple[float, list[CapType]]:
    """Credit-policy caps applied AFTER the weighted sum.

    utilization > 1.0 -> hard-cap at 40; > 0.9 -> cap at 60; IsBlocked -> *0.5.
    """
    caps: list[CapType] = []
    capped = raw_score
    if max_utilization is not None:
        if max_utilization > UTILIZATION_HARD_THRESHOLD:
            capped = min(capped, float(UTILIZATION_HARD_CAP))
            caps.append(CapType.UTILIZATION_HARD_40)
        elif max_utilization > UTILIZATION_SOFT_THRESHOLD:
            capped = min(capped, float(UTILIZATION_SOFT_CAP))
            caps.append(CapType.UTILIZATION_SOFT_60)
    if is_blocked:
        capped *= BLOCKED_MULTIPLIER
        caps.append(CapType.BLOCKED_HALF)
    return capped, caps


@dataclass
class ScoreComputation:
    sub_factors: SubFactors
    raw_score: float
    score: int
    rating: Rating
    caps_applied: list[CapType]
    debt_load_source: DebtLoadSource


def compute_sub_factors(
    *,
    regular_counts: dict[str, int],
    retail_counts: dict[str, int],
    sync_live: bool,
    overdue_eur: float,
    turnover_eur: float,
    open_unpaid_count: int,
    total_sales_count: int,
    order_count: int,
    recency_days: int | None,
    tenure_months: int,
    return_qty_rate: float,
) -> SubFactors:
    return SubFactors(
        discipline=_sub_factor(discipline_value(regular_counts, retail_counts), W_DISCIPLINE),
        debt_load=_sub_factor(
            debt_load_value(
                sync_live, overdue_eur, turnover_eur, open_unpaid_count, total_sales_count
            ),
            W_DEBT_LOAD,
        ),
        activity=_sub_factor(activity_value(order_count, recency_days), W_ACTIVITY),
        tenure=_sub_factor(tenure_value(tenure_months), W_TENURE),
        return_quality=_sub_factor(return_quality_value(return_qty_rate), W_RETURN_QUALITY),
    )


def finalize(
    sub_factors: SubFactors,
    max_utilization: float | None,
    is_blocked: bool,
    debt_load_source: DebtLoadSource,
) -> ScoreComputation:
    raw_score = (
        sub_factors.discipline.points
        + sub_factors.debt_load.points
        + sub_factors.activity.points
        + sub_factors.tenure.points
        + sub_factors.return_quality.points
    )
    capped, caps = apply_caps(raw_score, max_utilization, is_blocked)
    score = int(round(_clamp(capped, 0.0, 100.0)))
    return ScoreComputation(
        sub_factors=sub_factors,
        raw_score=round(raw_score, 4),
        score=score,
        rating=band_for(score),
        caps_applied=caps,
        debt_load_source=debt_load_source,
    )


def compute_score(client_id: int, as_of_date: str | None, window_months: int) -> ScoreComputation:
    """Pull every signal from the repository, compute the 5 sub-factors, the weighted sum, caps,
    band, and the recorded debt_load_source. Sync-aware DebtLoad via repo.debt_sync_is_live().
    """
    settings = get_settings()
    as_of = as_of_date or settings.resolve_fx_date(None)
    fx_date = settings.resolve_fx_date(as_of_date)

    regular = repo.payment_status_counts(client_id, as_of, window_months)
    retail = repo.retail_payment_status_counts(client_id, as_of, window_months)

    sync_live = repo.debt_sync_is_live()
    debt_load_source = DebtLoadSource.DEBT_TABLE if sync_live else DebtLoadSource.LIVE_PROXY

    turnover = repo.turnover_eur(client_id, as_of, window_months, fx_date)
    open_stats = repo.open_unpaid_stats(client_id, as_of, window_months)
    total_sales = repo.total_sales_count(client_id, as_of, window_months)
    overdue = (
        repo.overdue_amount_eur(client_id, as_of, fx_date) if sync_live else 0.0
    )

    activity = repo.activity_stats(client_id, as_of, window_months)
    ret_rate = repo.return_qty_rate(client_id, as_of, window_months)

    agreements = repo.credit_limit_utilization(client_id)
    flags = repo.client_flags(client_id)

    sub_factors = compute_sub_factors(
        regular_counts=regular,
        retail_counts=retail,
        sync_live=sync_live,
        overdue_eur=overdue,
        turnover_eur=turnover,
        open_unpaid_count=int(open_stats.get("open_count", 0)),
        total_sales_count=total_sales,
        order_count=int(activity.get("order_count", 0)),
        recency_days=activity.get("recency_days"),
        tenure_months=int(activity.get("tenure_months", 0)),
        return_qty_rate=ret_rate,
    )

    return finalize(
        sub_factors=sub_factors,
        max_utilization=max_limit_utilization(agreements),
        is_blocked=bool(flags.get("is_blocked")),
        debt_load_source=debt_load_source,
    )
