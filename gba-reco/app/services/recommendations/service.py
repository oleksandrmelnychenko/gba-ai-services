"""Cached recommendation service facade — single path used by API and worker."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.core.history import require_supported_as_of
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import sales_repository as repo
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
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")
    require_supported_as_of(as_of)
    if not repo.client_exists(customer_id):
        raise UnknownCustomerError(f"customer {customer_id} was not found")
    client_agreement_net_id = require_agreement(customer_id, client_agreement_net_id)
    scope = {"client_agreement_net_id": client_agreement_net_id} if client_agreement_net_id else {}
    key = cache.make_key(customer_id, as_of, top_n, include_discovery, region_scope, **scope)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            cached["cached"] = True
            cached["recommendations"] = [
                ProductRec(
                    product_id=r["product_id"], score=r["score"], rank=r["rank"],
                    segment=r["segment"], source=RecSource(r["source"]),
                    source_detail=RecSourceDetail(r["source_detail"]),
                )
                for r in cached["recommendations"]
            ]
            result = RecommendationResult(**cached)
            if (result.customer_id != customer_id or result.as_of_date != as_of
                    or result.client_agreement_net_id != client_agreement_net_id):
                raise ValueError("cached recommendation identity mismatch")
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
