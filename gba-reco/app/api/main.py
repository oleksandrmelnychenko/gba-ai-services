"""FastAPI app — GBA Client Recommendation Service (production shell)."""
from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data.db import dispose, get_engine
from app.domain.models import ProductRec, RecommendationResult, RecSource
from app.services.recommendations import copurchase, service

settings = get_settings()
log = get_logger("api")

_COPURCHASE_TTL = 24 * 3600

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_release_safe("gba-reco")
    get_engine()  # warm pool
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-reco running OPEN — set INTERNAL_API_KEY")
    log.info("service_starting", model_version=cache._MODEL_VERSION)
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Client Recommendation Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST"], allow_headers=["*"],
)


@app.middleware("http")
async def require_internal_key(request: Request, call_next):
    if settings.internal_api_key and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("X-Internal-Api-Key", "")
        if not hmac.compare_digest(provided, settings.internal_api_key):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def timing(request: Request, call_next):
    t = time.time()
    resp = await call_next(request)
    resp.headers["X-Process-Time-Ms"] = str(round((time.time() - t) * 1000, 2))
    return resp


class RecommendRequest(BaseModel):
    customer_id: int = Field(..., description="dbo.ClientAgreement.ClientID")
    top_n: int = Field(default=25, ge=1, le=200)
    as_of_date: str | None = None
    include_discovery: bool = True
    use_cache: bool = True
    region_scope: bool = Field(
        default=False,
        description="byRegion toggle: scope discovery candidates to the client's oblast "
        "(Client.RegionID). Opt-in; off = identical to prior behaviour. Measured neutral on "
        "the offline eval (see docs/eval-baseline.md) — do not enable as a default.",
    )


class BatchRequest(BaseModel):
    customer_ids: list[int] = Field(..., min_length=1, max_length=500)
    top_n: int = Field(default=25, ge=1, le=200)
    as_of_date: str | None = None
    include_discovery: bool = True
    use_cache: bool = True
    region_scope: bool = False


class FeedbackRequest(BaseModel):
    customer_id: int = Field(..., description="dbo.ClientAgreement.ClientID")
    product_ids: list[int] = Field(..., min_length=1, max_length=200)
    kind: str = Field(default="reject", description="negative feedback signal type")


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return {
        "status": "healthy" if db_ok else "degraded",
        "db_connected": db_ok,
        "redis_connected": cache.health(),
        "version": "0.1.0",
        "model_version": cache._MODEL_VERSION,
    }


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/recommend", response_model=RecommendationResult)
def recommend(req: RecommendRequest) -> RecommendationResult:
    try:
        return service.get_recommendations(
            customer_id=req.customer_id, as_of_date=req.as_of_date, top_n=req.top_n,
            include_discovery=req.include_discovery, use_cache=req.use_cache,
            region_scope=req.region_scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("recommend_failed", customer_id=req.customer_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"recommendation_failed: {exc}") from exc


@app.post("/recommend/copurchase", response_model=RecommendationResult)
def recommend_copurchase(req: RecommendRequest) -> RecommendationResult:
    """Item-item co-purchase recommender — the discovery source for cross-sell (faster than the
    v3.2 user-Jaccard and competitive in eval). Synthetic/ubiquitous lines already excluded."""
    as_of = req.as_of_date or time.strftime("%Y-%m-%d")
    key = cache.make_copurchase_key(req.customer_id, as_of, req.top_n)
    if req.use_cache:
        cached = cache.get(key)
        if cached is not None:
            cached["cached"] = True
            cached["recommendations"] = [
                ProductRec(product_id=r["product_id"], score=r["score"], rank=r["rank"],
                           segment=r["segment"], source=RecSource(r["source"]))
                for r in cached["recommendations"]
            ]
            return RecommendationResult(**cached)
    try:
        result = copurchase.recommend(req.customer_id, as_of, top_n=req.top_n, include_owned=False)
    except Exception as exc:  # noqa: BLE001
        log.error("copurchase_failed", customer_id=req.customer_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"copurchase_failed: {exc}") from exc
    if req.use_cache:
        cache.set(key, result.model_dump(mode="json"), ttl=_COPURCHASE_TTL)
    return result


@app.post("/recommend/batch")
def recommend_batch(req: BatchRequest) -> dict:
    """Batch endpoint (maps to .NET RecommendationsBatchEndpoint). Per-customer errors
    are isolated so one bad id doesn't fail the batch."""
    results, errors = [], []
    for cid in req.customer_ids:
        try:
            results.append(service.get_recommendations(
                customer_id=cid, as_of_date=req.as_of_date, top_n=req.top_n,
                include_discovery=req.include_discovery, use_cache=req.use_cache,
                region_scope=req.region_scope,
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append({"customer_id": cid, "error": str(exc)})
    return {"results": results, "errors": errors, "count": len(results), "failed": len(errors)}


@app.post("/feedback")
def feedback(req: FeedbackRequest) -> dict:
    """Record negative feedback (products a downstream consumer judged a bad recommendation for a
    customer) so the recommender excludes them. Invalidates the customer's copurchase cache so the
    exclusion takes effect on the next call. Used by gba-nba when a manager dismisses / fails to
    sell a cross-sell task."""
    added = cache.add_negatives(req.customer_id, req.product_ids, ttl=settings.feedback_ttl)
    cache.invalidate_copurchase(req.customer_id)
    log.info("feedback", customer_id=req.customer_id, kind=req.kind,
             products=len(req.product_ids), added=added)
    total = len(cache.get_negatives(req.customer_id))
    return {"customer_id": req.customer_id, "added": added, "total_negatives": total}


@app.delete("/cache/{customer_id}")
def clear_cache(customer_id: int) -> dict:
    return {"deleted": cache.invalidate_customer(customer_id)}
