"""Cached recommendation service facade — single path used by API and worker."""
from __future__ import annotations

from datetime import datetime

from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.domain.models import ProductRec, RecommendationResult, RecSource
from app.services.recommendations import recommender

log = get_logger("reco_service")


def get_recommendations(
    customer_id: int,
    as_of_date: str | None = None,
    top_n: int = 25,
    include_discovery: bool = True,
    use_cache: bool = True,
    region_scope: bool = False,
) -> RecommendationResult:
    started = datetime.now()
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")
    key = cache.make_key(customer_id, as_of, top_n, include_discovery, region_scope)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            cached["cached"] = True
            cached["recommendations"] = [
                ProductRec(
                    product_id=r["product_id"], score=r["score"], rank=r["rank"],
                    segment=r["segment"], source=RecSource(r["source"]),
                )
                for r in cached["recommendations"]
            ]
            result = RecommendationResult(**cached)
            METRICS.record_request((datetime.now() - started).total_seconds() * 1000)
            return result

    error = False
    try:
        result = recommender.recommend(
            customer_id=customer_id, as_of_date=as_of, top_n=top_n,
            include_discovery=include_discovery, region_scope=region_scope,
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
