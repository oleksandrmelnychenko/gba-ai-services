from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.core.config import get_settings
from app.core.history import coverage, require_supported_as_of
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import solvency_repository as repo
from app.domain.models import (
    CapType,
    ClientIdentityMismatchError,
    Contribution,
    CurrencyExposure,
    DataSufficiency,
    ForwardRisk,
    ForwardRiskBand,
    ForwardRiskStatus,
    GaugeChart,
    Rating,
    SolvencyCharts,
    SolvencyScore,
)
from app.domain.money import round_cent
from app.risk import dataset as risk_dataset
from app.risk.score_current import score_current
from app.risk.score_forward import ForwardModelUnavailableError, score_forward
from app.services.solvency import charts as charts_builder

log = get_logger("solvency_service")

# The current-state scorecard band (A/B/C/D) maps 1:1 to the Rating enum.
_BAND_TO_RATING = {"A": Rating.A, "B": Rating.B, "C": Rating.C, "D": Rating.D}

_NO_SALES_24MO_DAYS = 730.0
def _data_sufficiency(features: dict[str, float]) -> tuple[DataSufficiency, str | None]:
    """Feature-coverage flag: with no sales in 24mo (never-bought recency sentinel included),
    no live debt lines and no credit-terms signal, every feature sits in the safest WOE bin and
    the scorecard emits 100/A by construction — flag it, never touch the score math."""
    no_sales_24mo = float(features.get("recency_days", 0.0)) > _NO_SALES_24MO_DAYS
    no_debt_history = (
        float(features.get("n_open_debt_lines", 0.0)) == 0.0
        and float(features.get("total_debt_eur", 0.0)) == 0.0
        and float(features.get("months_with_debt_last12", 0.0)) == 0.0
        and float(features.get("new_debt_eur_3mo", 0.0)) == 0.0
    )
    # A freshly synchronized agreement may enable the technical control flags while both
    # controlled values remain zero.  The flag alone is not evidence about the buyer's
    # creditworthiness: without a positive limit or grace period it carries no usable terms.
    no_credit_terms = (
        float(features.get("credit_limit_eur", 0.0)) <= 0.0
        and float(features.get("grace_days", 0.0)) <= 0.0
    )
    if no_sales_24mo and no_debt_history and no_credit_terms:
        history_start = get_settings().source_history_start_date.isoformat()
        return (
            DataSufficiency.INSUFFICIENT,
            f"no sales, debt history or non-zero credit terms in available data since "
            f"{history_start}",
        )
    return DataSufficiency.OK, None


def _resolve_client_id(client_id: int | None, client_net_uid: str | None) -> int:
    if client_id is not None:
        if not repo.client_exists(client_id):
            raise LookupError(f"client_id not found: {client_id}")
        if client_net_uid is not None:
            resolved = repo.resolve_client_id(client_net_uid)
            if resolved is None:
                raise LookupError(f"client_net_uid not found: {client_net_uid}")
            if resolved != client_id:
                raise ClientIdentityMismatchError("client_id and client_net_uid do not match")
        return client_id
    if client_net_uid is None:
        raise ValueError("client_id or client_net_uid required")
    resolved = repo.resolve_client_id(client_net_uid)
    if resolved is None:
        raise LookupError(f"client_net_uid not found: {client_net_uid}")
    return resolved


def _as_of(as_of_date: str | None) -> str:
    resolved = as_of_date or datetime.now().strftime("%Y-%m-%d")
    return require_supported_as_of(resolved).isoformat()


def _hydrate_score(data: dict, expected_client_id: int) -> SolvencyScore | None:
    """Rebuild a v3 SolvencyScore from a cached JSON dict (pydantic validates the shape).

    Entries cached before the data_sufficiency fields existed are treated as a miss (returns
    None) so a dormant client can't serve a stale ok-by-default flag until the TTL expires.
    """
    required_metadata = {
        "data_sufficiency",
        "forward_risk_status",
        "current_model_run_id",
        "source_history_start",
        "effective_start",
        "history_complete",
    }
    if not required_metadata.issubset(data):
        return None
    result = SolvencyScore.model_validate(data)
    if result.client_id != expected_client_id or result.as_of_date is None:
        return None
    expected_history = coverage(result.as_of_date, result.window_months)
    if (
        result.source_history_start != expected_history.source_history_start.isoformat()
        or result.effective_start != expected_history.effective_start.isoformat()
        or result.history_complete != expected_history.history_complete
    ):
        return None
    return result


def _hydrate_charts(
    data: dict,
    expected_client_id: int,
    as_of: str,
    window_months: int,
) -> SolvencyCharts | None:
    required_metadata = {
        "source_history_start",
        "effective_start",
        "history_complete",
    }
    if not required_metadata.issubset(data):
        return None
    result = SolvencyCharts.model_validate(data)
    expected_history = coverage(as_of, window_months)
    if (
        result.client_id != expected_client_id
        or result.as_of_date != as_of
        or result.window_months != window_months
        or result.source_history_start
        != expected_history.source_history_start.isoformat()
        or result.effective_start != expected_history.effective_start.isoformat()
        or result.history_complete != expected_history.history_complete
    ):
        return None
    return result


def _forward_risk(
    features: dict[str, float],
) -> tuple[ForwardRisk | None, ForwardRiskStatus, str | None]:
    """Map score_forward()'s 6mo early-warning output to the v3 ForwardRisk{band, pd}.

    The forward model's declared population is ``total_debt_eur > 0`` and *not already SEV180*.
    An already-defaulted buyer is outside that population: returning the behavioral model's
    usually-low PD would be actively misleading, so the forward signal is absent while the
    current-state score continues to carry the C/D risk. Buyers with no debt are outside the
    model's declared population too, so they have no forward signal. At-risk-with-debt buyers
    carry the behavioral band + PD.
    """
    total_debt = float(features.get("total_debt_eur", 0.0) or 0.0)
    severe_overdue = float(features.get("overdue_eur_180plus", 0.0) or 0.0)
    if total_debt <= 0.0:
        return (
            None,
            ForwardRiskStatus.NOT_APPLICABLE,
            "no current debt: outside forward model population",
        )
    if severe_overdue >= risk_dataset.SEV180_MIN_EUR:
        return (
            None,
            ForwardRiskStatus.NOT_APPLICABLE,
            "already SEV180: outside forward model population",
        )
    try:
        fwd = score_forward(features)
    except ForwardModelUnavailableError as exc:
        return None, ForwardRiskStatus.MODEL_UNAVAILABLE, str(exc)
    if fwd["band"] == "none":
        return None, ForwardRiskStatus.NOT_APPLICABLE, "outside forward model population"
    return (
        ForwardRisk(band=ForwardRiskBand(fwd["band"]), pd=float(fwd["pd_behavioral"])),
        ForwardRiskStatus.AVAILABLE,
        None,
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
            turnover_eur=round_cent(r["turnover_eur"]),
            exposure_eur=0.0,
        )
        for r in rows
    ]


def _not_applicable(
    cid: int,
    client_net_uid: str | None,
    as_of: str,
    window_months: int,
    settings,
) -> SolvencyScore:
    """The non-buyer gate result: applicable=false, everything below null."""
    history = coverage(as_of, window_months)
    return SolvencyScore(
        client_id=cid,
        client_net_uid=client_net_uid,
        applicable=False,
        score=None,
        rating=None,
        pd=None,
        contributions=None,
        forward_risk=None,
        forward_risk_status=ForwardRiskStatus.NOT_APPLICABLE,
        forward_risk_reason="client has no buyer role",
        sub_factors=None,
        caps_applied=[],
        debt_load_source=None,
        raw_score=None,
        currency_breakdown=None,
        data_sufficiency=DataSufficiency.INSUFFICIENT,
        data_sufficiency_reason="client has no buyer role (score not applicable)",
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
        as_of_date=as_of,
        window_months=window_months,
        model_version=settings.model_version,
        current_model_run_id=None,
    )


def _not_applicable_charts(cid: int, as_of: str, window_months: int) -> SolvencyCharts:
    """The non-buyer gate result for /charts: applicable=false, every series empty.

    Mirrors score_client's non-buyer gate — a provider-only client has no buyer-side signal, so
    the charts (score sparkline, discipline donut, aging) would be built from no-data fallbacks and
    render a misleading flat-100 trajectory. Return empty series instead of a fabricated one.
    """
    history = coverage(as_of, window_months)
    return SolvencyCharts(
        client_id=cid,
        applicable=False,
        limit_utilization_gauge=GaugeChart(value=0.0),
        payment_discipline_donut=[],
        open_invoice_aging_bars=[],
        turnover_vs_exposure=[],
        score_sparkline=[],
        turnover_trend=[],
        aging_over_time_heatmap="not_applicable",
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
        as_of_date=as_of,
        window_months=window_months,
    )


def _build_score(
    cid: int,
    client_net_uid: str | None,
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
    history = coverage(as_of, window_months)
    sufficiency, sufficiency_reason = _data_sufficiency(features)
    if sufficiency == DataSufficiency.INSUFFICIENT:
        return SolvencyScore(
            client_id=cid,
            client_net_uid=client_net_uid,
            applicable=True,
            score=None,
            rating=None,
            pd=None,
            contributions=None,
            forward_risk=None,
            forward_risk_status=ForwardRiskStatus.NOT_APPLICABLE,
            forward_risk_reason=sufficiency_reason,
            sub_factors=None,
            caps_applied=[],
            debt_load_source=None,
            raw_score=None,
            currency_breakdown=None,
            data_sufficiency=sufficiency,
            data_sufficiency_reason=sufficiency_reason,
            source_history_start=history.source_history_start.isoformat(),
            effective_start=history.effective_start.isoformat(),
            history_complete=history.history_complete,
            as_of_date=as_of,
            window_months=window_months,
            model_version=settings.model_version,
            current_model_run_id=None,
        )

    current = score_current(features)
    forward, forward_status, forward_reason = _forward_risk(features)
    return SolvencyScore(
        client_id=cid,
        client_net_uid=client_net_uid,
        applicable=True,
        score=int(round(current["score"])),
        rating=_BAND_TO_RATING[current["band"]],
        pd=current["pd"],
        contributions=[
            Contribution(feature=c["feature"], value=c["value"], points=c["points"])
            for c in current["contributions"]
        ],
        forward_risk=forward,
        forward_risk_status=forward_status,
        forward_risk_reason=forward_reason,
        sub_factors=None,
        caps_applied=[
            CapType(value) for value in current.get("policy_overrides", [])
        ],
        debt_load_source=None,
        raw_score=None,
        currency_breakdown=currency,
        data_sufficiency=sufficiency,
        data_sufficiency_reason=sufficiency_reason,
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
        as_of_date=as_of,
        window_months=window_months,
        model_version=settings.model_version,
        current_model_run_id=current["model_version"],
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
    as_of = _as_of(as_of_date)
    cid = _resolve_client_id(client_id, client_net_uid)
    settings = get_settings()

    if not repo.has_buyer_role(cid):
        result = _not_applicable(
            cid,
            client_net_uid.lower() if client_net_uid is not None else None,
            as_of,
            window_months,
            settings,
        )
        latency = (datetime.now() - started).total_seconds() * 1000
        METRICS.record_request(latency)
        log.info("score_not_applicable", client_id=cid, latency_ms=round(latency, 2))
        return result

    key = cache.make_key(cid, as_of, window_months)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            result = _hydrate_score(cached, cid)
            if result is not None:
                if client_net_uid is not None:
                    result = result.model_copy(
                        update={"client_net_uid": client_net_uid.lower()}
                    )
                METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
                return result

    error = False
    try:
        features = risk_dataset.features_one(cid, as_of, window_months)
        fx_date = settings.resolve_fx_date(as_of_date)
        currency = _currency_breakdown(cid, as_of, window_months, fx_date)
        result = _build_score(
            cid,
            client_net_uid.lower() if client_net_uid is not None else None,
            features,
            currency,
            as_of,
            window_months,
            settings,
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
        pd=result.pd,
        rating=result.rating.value if result.rating else None,
        forward_band=result.forward_risk.band.value if result.forward_risk else None,
        forward_status=result.forward_risk_status.value,
        data_sufficiency=result.data_sufficiency.value,
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
                resolved[cid] = _not_applicable(
                    cid, None, as_of, window_months, settings
                )
                continue
            if use_cache:
                cached = cache.get(cache.make_key(cid, as_of, window_months))
                if cached is not None:
                    hydrated = _hydrate_score(cached, cid)
                    if hydrated is not None:
                        resolved[cid] = hydrated
                        continue
            pending.append(cid)
        except LookupError as exc:
            errors.append({"client_id": cid, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            err_id = uuid.uuid4().hex
            log.error("score_batch_item_failed", client_id=cid, error_id=err_id, error=str(exc))
            errors.append({"client_id": cid, "error": f"internal error (ref {err_id})"})

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
                    cid, None, features_by_cid[cid], currency_by_cid[cid],
                    as_of, window_months, settings,
                )
                if use_cache:
                    cache.set(
                        cache.make_key(cid, as_of, window_months),
                        result.model_dump(mode="json"),
                    )
                resolved[cid] = result
            except Exception as exc:  # noqa: BLE001
                err_id = uuid.uuid4().hex
                log.error(
                    "score_batch_item_failed", client_id=cid, error_id=err_id, error=str(exc)
                )
                errors.append({"client_id": cid, "error": f"internal error (ref {err_id})"})

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
    as_of = _as_of(as_of_date)
    if not repo.client_exists(client_id):
        raise LookupError(f"client_id not found: {client_id}")

    if not repo.has_buyer_role(client_id):
        result = _not_applicable_charts(client_id, as_of, window_months)
        METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
        log.info("charts_not_applicable", client_id=client_id)
        return result

    key = cache.make_charts_key(client_id, as_of, window_months)

    cached = cache.get(key)
    if cached is not None:
        result = _hydrate_charts(cached, client_id, as_of, window_months)
        if result is not None:
            METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
            return result

    result = charts_builder.build_charts(client_id, as_of, window_months)
    cache.set(key, result.model_dump(mode="json"))

    latency = (datetime.now() - started).total_seconds() * 1000
    METRICS.record_request(latency)
    log.info("charts", client_id=client_id, latency_ms=round(latency, 2))
    return result
