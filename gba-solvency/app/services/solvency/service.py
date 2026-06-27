from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import solvency_repository as repo
from app.domain.models import (
    Contribution,
    CurrencyExposure,
    ForwardRisk,
    ForwardRiskBand,
    Rating,
    SolvencyCharts,
    SolvencyScore,
)
from app.risk import dataset as risk_dataset
from app.risk.score_current import score_current
from app.risk.score_forward import _load as _forward_card
from app.risk.score_forward import score_forward
from app.services.solvency import charts as charts_builder

log = get_logger("solvency_service")

# The current-state scorecard band (A/B/C/D) maps 1:1 to the Rating enum.
_BAND_TO_RATING = {"A": Rating.A, "B": Rating.B, "C": Rating.C, "D": Rating.D}


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
    """Rebuild a v3 SolvencyScore from a cached JSON dict (pydantic validates the shape)."""
    return SolvencyScore.model_validate(data)


def _forward_risk(features: dict[str, float]) -> ForwardRisk:
    """Map score_forward()'s 6mo early-warning output to the v3 ForwardRisk{band, pd}.

    score_forward returns band "none"/pd 0 for buyers with no debt; per the v3 contract those
    are surfaced as band "low" with pd ~= the forward population base rate (genuinely low risk on
    a 6mo horizon). At-risk-with-debt buyers carry the behavioral-only band + PD verbatim.
    """
    fwd = score_forward(features)
    if fwd["band"] == "none":
        base = float(_forward_card().get("base_rate", 0.0) or 0.0)
        return ForwardRisk(band=ForwardRiskBand.LOW, pd=round(base, 4))
    return ForwardRisk(band=ForwardRiskBand(fwd["band"]), pd=float(fwd["pd_behavioral"]))


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


def _not_applicable(cid: int, as_of: str, window_months: int, settings) -> SolvencyScore:
    """The non-buyer gate result: applicable=false, everything below null."""
    return SolvencyScore(
        client_id=cid,
        applicable=False,
        score=None,
        rating=None,
        pd=None,
        contributions=None,
        forward_risk=None,
        sub_factors=None,
        caps_applied=[],
        debt_load_source=None,
        raw_score=None,
        currency_breakdown=None,
        as_of_date=as_of,
        window_months=window_months,
        model_version=settings.model_version,
    )


def _build_score(
    cid: int,
    features: dict[str, float],
    currency: list[CurrencyExposure] | None,
    as_of: str,
    window_months: int,
    settings,
) -> SolvencyScore:
    """Assemble the applicable-buyer SolvencyScore from a feature dict.

    This is the single shared code path for the score math so the per-client (/score) and
    set-based batch (/score/batch) routes are guaranteed bit-identical: same score_current,
    same forward scorecard, same rounding.
    """
    current = score_current(features)
    forward = _forward_risk(features)
    return SolvencyScore(
        client_id=cid,
        applicable=True,
        score=int(round(current["score"])),
        rating=_BAND_TO_RATING[current["band"]],
        pd=current["pd"],
        contributions=[
            Contribution(feature=c["feature"], value=c["value"], points=c["points"])
            for c in current["contributions"]
        ],
        forward_risk=forward,
        sub_factors=None,
        caps_applied=[],
        debt_load_source=None,
        raw_score=None,
        currency_breakdown=currency,
        as_of_date=as_of,
        window_months=window_months,
        model_version=settings.model_version,
    )


def score_client(
    client_id: int | None,
    client_net_uid: str | None,
    as_of_date: str | None,
    window_months: int,
    use_cache: bool,
) -> SolvencyScore:
    """v3 (creditscore-v3): WOE+logistic current-state scorecard + 6mo forward early-warning.

    Keeps the has_buyer_role gate (non-buyer -> applicable=false, everything below null). For an
    applicable buyer: build the single-client features as-of the resolved date, run score_current
    (-> score/pd/band/contributions) and the forward scorecard (-> forward_risk{band,pd}). The old
    5-factor sub_factors is DEPRECATED and emitted as null. Result is cached under the bumped
    creditscore-v3 namespace, so old v1/v2 cache entries never collide.
    """
    started = datetime.now()
    cid = _resolve_client_id(client_id, client_net_uid)
    as_of = _as_of(as_of_date)
    settings = get_settings()

    if not repo.has_buyer_role(cid):
        result = _not_applicable(cid, as_of, window_months, settings)
        latency = (datetime.now() - started).total_seconds() * 1000
        METRICS.record_request(latency)
        log.info("score_not_applicable", client_id=cid, latency_ms=round(latency, 2))
        return result

    key = cache.make_key(cid, as_of, window_months)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            result = _hydrate_score(cached)
            METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
            return result

    error = False
    try:
        features = risk_dataset.features_one(cid, as_of, window_months)
        fx_date = settings.resolve_fx_date(as_of_date)
        currency = _currency_breakdown(cid, as_of, window_months, fx_date)
        result = _build_score(cid, features, currency, as_of, window_months, settings)
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
        pd=result.pd,
        rating=result.rating.value if result.rating else None,
        forward_band=result.forward_risk.band.value if result.forward_risk else None,
        latency_ms=round(latency, 2),
    )
    return result


def score_batch(
    client_ids: list[int],
    as_of_date: str | None,
    window_months: int,
    use_cache: bool,
) -> tuple[list[SolvencyScore], list[dict]]:
    """Score many clients in a handful of set-based queries instead of N per-client passes.

    Identical result to calling score_client() for each id (same gate, cache, score math,
    currency breakdown) — only the data-access path differs: the 6 feature groups are pulled
    ONCE for the whole applicable id-list via risk_dataset.features_many (a constant number of
    round-trips regardless of N), then score_current / forward scorecard run per client.

    Resolution order per id, mirroring score_client:
      1. unknown client_id -> error (LookupError text), isolated.
      2. non-buyer -> applicable=false result (the gate).
      3. cache hit (when use_cache) -> hydrate cached result.
      4. else -> set-based features -> _build_score -> cache.set.

    Returns (results, errors) where errors is [{client_id, error}]. Per-client errors are
    isolated so one bad id never fails the batch.
    """
    started = datetime.now()
    settings = get_settings()
    fx_date = settings.resolve_fx_date(as_of_date)
    as_of = _as_of(as_of_date)

    results: list[SolvencyScore] = []
    errors: list[dict] = []
    # preserve caller order; de-dupe so a repeated id isn't scored twice
    ordered_ids = list(dict.fromkeys(int(c) for c in client_ids))

    # Phase 1 — resolve gate + cache per id (cheap point lookups). Collect the ids that still
    # need a fresh feature pull, keyed so we can reassemble in caller order at the end.
    pending: list[int] = []
    resolved: dict[int, SolvencyScore] = {}
    for cid in ordered_ids:
        try:
            if not repo.client_exists(cid):
                raise LookupError(f"client_id not found: {cid}")
            if not repo.has_buyer_role(cid):
                resolved[cid] = _not_applicable(cid, as_of, window_months, settings)
                continue
            if use_cache:
                cached = cache.get(cache.make_key(cid, as_of, window_months))
                if cached is not None:
                    resolved[cid] = _hydrate_score(cached)
                    continue
            pending.append(cid)
        except Exception as exc:  # noqa: BLE001
            errors.append({"client_id": cid, "error": str(exc)})

    # Phase 2 — ONE set-based feature pull for every applicable, uncached buyer.
    if pending:
        features_by_cid = risk_dataset.features_many(pending, as_of, window_months)
        # Per-currency breakdown is still a per-client query; fan out over a thread pool so it
        # doesn't reintroduce an N-serial bottleneck.
        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            currency_by_cid = dict(
                zip(
                    pending,
                    pool.map(
                        lambda c: _currency_breakdown(c, as_of, window_months, fx_date),
                        pending,
                    ),
                    strict=True,
                )
            )
        for cid in pending:
            try:
                result = _build_score(
                    cid, features_by_cid[cid], currency_by_cid[cid],
                    as_of, window_months, settings,
                )
                if use_cache:
                    cache.set(
                        cache.make_key(cid, as_of, window_months),
                        result.model_dump(mode="json"),
                    )
                resolved[cid] = result
            except Exception as exc:  # noqa: BLE001
                errors.append({"client_id": cid, "error": str(exc)})

    # reassemble in caller order
    for cid in ordered_ids:
        if cid in resolved:
            results.append(resolved[cid])

    latency = (datetime.now() - started).total_seconds() * 1000
    METRICS.record_request(latency, error=bool(errors))
    log.info(
        "score_batch",
        n=len(ordered_ids),
        scored=len(results),
        failed=len(errors),
        latency_ms=round(latency, 2),
    )
    return results, errors


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
