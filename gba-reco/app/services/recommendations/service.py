"""Cached recommendation service facade — single path used by API and worker."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.core.config import get_settings
from app.core.history import require_supported_as_of
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import sales_repository as repo
from app.data import source_rules as rules
from app.data.db import SOURCE_UNAVAILABLE, unavailable_reason
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
)
from app.services.recommendations import recommender

log = get_logger("reco_service")


class UnknownCustomerError(ValueError):
    """The requested current client identity does not exist."""


class UnknownAgreementError(ValueError):
    """No active agreement belonging to the requested client."""


def require_agreement(customer_id: int, agreement_uid: str | None) -> str | None:
    if agreement_uid is None:
        return None  # Legacy internal consumers (NBA/batch) use client-level signals.
    normalized = str(UUID(agreement_uid))
    if normalized not in repo.active_agreements(customer_id):
        raise UnknownAgreementError("active client agreement was not found for this customer")
    return normalized


def read_cached(key: str, as_of: str) -> dict | None:
    cached = cache.get(key)
    if cached is None and get_settings().serve_latest_fallback:
        cached = cache.get_latest(key, as_of)
    return cached


def served_date_matches(served: str | None, as_of: str) -> bool:
    if served == as_of:
        return True
    return bool(
        get_settings().serve_latest_fallback
        and served
        and cache.fallback_floor(as_of) <= served < as_of
    )


def from_cache(
    cached: dict, customer_id: int, as_of: str, agreement_uid: str | None,
    degraded_reason: str | None = None,
) -> RecommendationResult:
    """A cached payload as a response: identity-checked, marked stale when it was computed
    for an earlier day, and marked degraded when it is served because the database is not
    answering."""
    cached = dict(cached)
    cached["cached"] = True
    cached["recommendations"] = [
        ProductRec(
            product_id=r["product_id"], score=r["score"], rank=r["rank"],
            segment=r["segment"], source=RecSource(r["source"]),
            source_detail=RecSourceDetail(r["source_detail"]),
        )
        for r in cached["recommendations"]
    ]
    cached.pop("_fleet_refresh", None)
    result = RecommendationResult(**cached)
    if (result.customer_id != customer_id
            or not served_date_matches(result.as_of_date, as_of)
            or result.client_agreement_net_id != agreement_uid):
        raise ValueError("cached recommendation identity mismatch")
    result.stale = result.as_of_date != as_of
    if degraded_reason:
        result.degraded = True
        result.degraded_reason = degraded_reason
    return result


def cached_while_source_is_down(
    key: str, customer_id: int, as_of: str, agreement_uid: str | None, exc: BaseException,
) -> RecommendationResult | None:
    try:
        cached = read_cached(key, as_of)
        if cached is None:
            return None
        result = from_cache(cached, customer_id, as_of, agreement_uid, unavailable_reason(exc))
    except Exception:  # noqa: BLE001
        return None
    log.warning("served_cached_without_database", customer_id=customer_id,
                reason=result.degraded_reason, as_of_date=result.as_of_date)
    return result


def get_recommendations(
    customer_id: int,
    as_of_date: str | None = None,
    top_n: int = 25,
    include_discovery: bool = True,
    use_cache: bool = True,
    region_scope: bool = False,
    client_agreement_net_id: str | None = None,
) -> RecommendationResult:
    started = datetime.now()
    as_of = as_of_date or rules.today()
    require_supported_as_of(as_of)
    try:
        if not repo.client_exists(customer_id):
            raise UnknownCustomerError(f"customer {customer_id} was not found")
        client_agreement_net_id = require_agreement(customer_id, client_agreement_net_id)
    except SOURCE_UNAVAILABLE as exc:
        requested = str(UUID(client_agreement_net_id)) if client_agreement_net_id else None
        key = cache.make_key(
            customer_id, as_of, top_n, include_discovery, region_scope,
            **({"client_agreement_net_id": requested} if requested else {}),
        )
        result = (
            cached_while_source_is_down(key, customer_id, as_of, requested, exc)
            if use_cache else None
        )
        METRICS.record_request(
            (datetime.now() - started).total_seconds() * 1000, error=result is None,
        )
        if result is None:
            raise
        return result
    scope = {"client_agreement_net_id": client_agreement_net_id} if client_agreement_net_id else {}
    key = cache.make_key(customer_id, as_of, top_n, include_discovery, region_scope, **scope)

    if use_cache:
        cached = read_cached(key, as_of)
        if cached is not None:
            result = from_cache(cached, customer_id, as_of, client_agreement_net_id)
            METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
            return result

    error = False
    try:
        result = recommender.recommend(
            customer_id=customer_id, as_of_date=as_of, top_n=top_n,
            include_discovery=include_discovery, region_scope=region_scope, **scope,
        )
    except Exception:
        error = True
        METRICS.record_request((datetime.now() - started).total_seconds() * 1000, error=True)
        raise

    if use_cache:
        cache.set(key, result.model_dump(mode="json"))

    latency = (datetime.now() - started).total_seconds() * 1000
    METRICS.record_request(latency, error=error)
    log.info("recommend", customer_id=customer_id, segment=result.segment,
             count=result.count, discovery=result.discovery_count, latency_ms=result.latency_ms)
    return result
