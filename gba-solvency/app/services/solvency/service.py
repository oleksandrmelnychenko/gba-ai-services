from __future__ import annotations

from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import solvency_repository as repo
from app.domain.models import (
    CapType,
    CurrencyExposure,
    DebtLoadSource,
    Rating,
    SolvencyCharts,
    SolvencyScore,
    SubFactor,
    SubFactors,
)
from app.services.solvency import charts as charts_builder
from app.services.solvency import score as scoring

log = get_logger("solvency_service")


def _resolve_client_id(client_id: int | None, client_net_uid: str | None) -> int:
    if client_id is not None:
        if not repo.client_exists(client_id):
            raise LookupError(f"client_id not found: {client_id}")
        return client_id
    if client_net_uid is None:
        raise ValueError("client_id or client_net_uid required")
    resolved = repo.resolve_client_id(client_net_uid)
    if resolved is None:
        raise LookupError(f"client_net_uid not found: {client_net_uid}")
    return resolved


def _as_of(as_of_date: str | None) -> str:
    return as_of_date or datetime.now().strftime("%Y-%m-%d")


def _hydrate_score(data: dict) -> SolvencyScore:
    sf = data["sub_factors"]
    sub_factors = SubFactors(
        discipline=SubFactor(**sf["discipline"]),
        debt_load=SubFactor(**sf["debt_load"]),
        activity=SubFactor(**sf["activity"]),
        tenure=SubFactor(**sf["tenure"]),
        return_quality=SubFactor(**sf["return_quality"]),
    )
    currency = data.get("currency_breakdown")
    return SolvencyScore(
        client_id=data["client_id"],
        score=data["score"],
        rating=Rating(data["rating"]),
        sub_factors=sub_factors,
        caps_applied=[CapType(c) for c in data.get("caps_applied", [])],
        debt_load_source=DebtLoadSource(data["debt_load_source"]),
        raw_score=data["raw_score"],
        currency_breakdown=(
            [CurrencyExposure(**c) for c in currency] if currency else None
        ),
        as_of_date=data.get("as_of_date"),
        window_months=data.get("window_months", 12),
        model_version=data.get("model_version", get_settings().model_version),
    )


def _currency_breakdown(
    client_id: int, as_of: str, window_months: int, fx_date: str
) -> list[CurrencyExposure] | None:
    rows = repo.turnover_eur_by_currency(client_id, as_of, window_months, fx_date)
    if len(rows) <= 1:
        return None
    return [
        CurrencyExposure(
            currency_id=r["currency_id"] if r["currency_id"] is not None else 0,
            turnover_eur=round(float(r["turnover_eur"]), 2),
            exposure_eur=0.0,
        )
        for r in rows
    ]


def score_client(
    client_id: int | None,
    client_net_uid: str | None,
    as_of_date: str | None,
    window_months: int,
    use_cache: bool,
) -> SolvencyScore:
    started = datetime.now()
    cid = _resolve_client_id(client_id, client_net_uid)
    as_of = _as_of(as_of_date)
    key = cache.make_key(cid, as_of, window_months)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            result = _hydrate_score(cached)
            METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
            return result

    error = False
    try:
        comp = scoring.compute_score(cid, as_of, window_months)
        settings = get_settings()
        fx_date = settings.resolve_fx_date(as_of_date)
        currency = _currency_breakdown(cid, as_of, window_months, fx_date)
        result = SolvencyScore(
            client_id=cid,
            score=comp.score,
            rating=comp.rating,
            sub_factors=comp.sub_factors,
            caps_applied=comp.caps_applied,
            debt_load_source=comp.debt_load_source,
            raw_score=comp.raw_score,
            currency_breakdown=currency,
            as_of_date=as_of,
            window_months=window_months,
            model_version=settings.model_version,
        )
    except Exception:
        error = True
        METRICS.record_request((datetime.now() - started).total_seconds() * 1000, error=True)
        raise

    if use_cache:
        cache.set(key, result.model_dump(mode="json"))

    latency = (datetime.now() - started).total_seconds() * 1000
    METRICS.record_request(latency, error=error)
    log.info(
        "score",
        client_id=cid,
        score=result.score,
        rating=result.rating.value,
        debt_load_source=result.debt_load_source.value,
        caps=[c.value for c in result.caps_applied],
        latency_ms=round(latency, 2),
    )
    return result


def build_charts(
    client_id: int,
    as_of_date: str | None,
    window_months: int,
) -> SolvencyCharts:
    started = datetime.now()
    if not repo.client_exists(client_id):
        raise LookupError(f"client_id not found: {client_id}")
    as_of = _as_of(as_of_date)
    key = cache.make_charts_key(client_id, as_of, window_months)

    cached = cache.get(key)
    if cached is not None:
        result = SolvencyCharts(**cached)
        METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
        return result

    result = charts_builder.build_charts(client_id, as_of, window_months)
    cache.set(key, result.model_dump(mode="json"))

    latency = (datetime.now() - started).total_seconds() * 1000
    METRICS.record_request(latency)
    log.info("charts", client_id=client_id, latency_ms=round(latency, 2))
    return result
