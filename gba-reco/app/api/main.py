"""FastAPI app — GBA Client Recommendation Service (production shell)."""
from __future__ import annotations

import hmac
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal, Self

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import get_settings
from app.core.history import require_supported_as_of
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import sales_repository as repo
from app.data.db import dispose, get_engine
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
)
from app.services.recommendations import copurchase, service

settings = get_settings()
log = get_logger("api")
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"

_COPURCHASE_TTL = 24 * 3600

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}
PositiveId = Annotated[int, Field(gt=0)]


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input,
        # turning garbage headers into 500s instead of a clean 401.
        if not hmac.compare_digest(provided.encode("utf-8"), settings.internal_api_key.encode("utf-8")):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def timing(request: Request, call_next):
    t = time.time()
    resp = await call_next(request)
    resp.headers["X-Process-Time-Ms"] = str(round((time.time() - t) * 1000, 2))
    return resp


class RecommendRequest(BaseModel):
    customer_id: PositiveId = Field(..., description="dbo.Client.ID")
    top_n: int = Field(default=25, ge=1, le=200)
    as_of_date: date | None = None
    include_discovery: bool = True
    use_cache: bool = True
    region_scope: bool = Field(
        default=False,
        description="byRegion toggle: scope discovery candidates to the client's oblast "
        "(Client.RegionID). Opt-in; off = identical to prior behaviour. Measured neutral on "
        "the offline eval (see docs/eval-baseline.md) — do not enable as a default.",
    )
    product_ids: list[PositiveId] | None = Field(
        default=None, max_length=50,
        description="copurchase only: explicit co-occurrence seeds (per-cart-line cross-sell) "
        "instead of the client's own purchase history",
    )

    @field_validator("as_of_date")
    @classmethod
    def supported_history_date(cls, value: date | None) -> date | None:
        if value is not None:
            require_supported_as_of(value)
        return value

    @model_validator(mode="after")
    def unique_product_ids(self) -> Self:
        if self.product_ids and len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("product_ids must be unique")
        return self


class BatchRequest(BaseModel):
    customer_ids: list[PositiveId] = Field(..., min_length=1, max_length=500)
    top_n: int = Field(default=25, ge=1, le=200)
    as_of_date: date | None = None
    include_discovery: bool = True
    use_cache: bool = True
    region_scope: bool = False

    @field_validator("as_of_date")
    @classmethod
    def supported_history_date(cls, value: date | None) -> date | None:
        if value is not None:
            require_supported_as_of(value)
        return value

    @model_validator(mode="after")
    def unique_customer_ids(self) -> Self:
        if len(self.customer_ids) != len(set(self.customer_ids)):
            raise ValueError("customer_ids must be unique")
        return self


class FeedbackRequest(BaseModel):
    customer_id: PositiveId = Field(..., description="dbo.Client.ID")
    product_ids: list[PositiveId] = Field(..., min_length=1, max_length=200)
    kind: Literal["reject"] = Field(default="reject", description="negative feedback signal type")

    @model_validator(mode="after")
    def unique_product_ids(self) -> Self:
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("product_ids must be unique")
        return self


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    redis_ok = cache.health()
    source = {
        "business_ready": False,
        "reasons": ["database_unavailable"],
        "latest_sale_at": None,
        "stocked_product_count": 0,
        "sellable_storage_count": 0,
        "synthetic_product_count": 0,
    }
    if db_ok:
        try:
            source = repo.source_readiness(settings.max_source_lag_days)
        except Exception as exc:  # noqa: BLE001
            log.error("source_readiness_failed", error=str(exc))
            source["reasons"] = ["source_readiness_failed"]
    source_history_start = settings.source_history_start_date.isoformat()
    source_history_contract_ready = (
        source_history_start == _EXPECTED_SOURCE_HISTORY_START
    )
    source = {**source, "source_history_start": source_history_start}
    if not source_history_contract_ready:
        source["business_ready"] = False
        source["reasons"] = [
            *list(source.get("reasons") or []),
            "source_history_start_mismatch",
        ]
    healthy = db_ok and redis_ok and bool(source["business_ready"])
    return {
        "status": "healthy" if healthy else "degraded",
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        **source,
        "source_history_contract_ready": source_history_contract_ready,
        "version": "0.1.0",
        "model_version": cache._MODEL_VERSION,
        "source_history_start": settings.source_history_start_date.isoformat(),
    }


@app.get("/ready")
def ready() -> JSONResponse:
    payload = health()
    is_ready = payload["status"] == "healthy"
    payload["status"] = "ready" if is_ready else "not_ready"
    return JSONResponse(status_code=200 if is_ready else 503, content=payload)


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/recommend", response_model=RecommendationResult)
def recommend(req: RecommendRequest) -> RecommendationResult:
    try:
        return service.get_recommendations(
            customer_id=req.customer_id,
            as_of_date=req.as_of_date.isoformat() if req.as_of_date else None,
            top_n=req.top_n,
            include_discovery=req.include_discovery, use_cache=req.use_cache,
            region_scope=req.region_scope,
        )
    except service.UnknownCustomerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        error_id = uuid.uuid4().hex
        log.error("recommend_failed", customer_id=req.customer_id, error_id=error_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"recommendation_failed (ref {error_id})") from exc


@app.post("/recommend/copurchase", response_model=RecommendationResult)
def recommend_copurchase(req: RecommendRequest) -> RecommendationResult:
    """Item-item co-purchase recommender — the discovery source for cross-sell (faster than the
    v3.2 user-Jaccard and competitive in eval). Synthetic/ubiquitous lines already excluded."""
    as_of = req.as_of_date.isoformat() if req.as_of_date else time.strftime("%Y-%m-%d")
    if not repo.client_exists(req.customer_id):
        raise HTTPException(status_code=404, detail=f"customer {req.customer_id} was not found")
    seeds = sorted(set(req.product_ids)) if req.product_ids else None
    if seeds:
        missing = sorted(set(seeds) - repo.active_product_ids(seeds))
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"message": "seed products were not found", "product_ids": missing},
            )
    key = cache.make_copurchase_key(req.customer_id, as_of, req.top_n)
    if seeds:
        key = f"{key}:s{','.join(map(str, seeds))}"
    if req.use_cache:
        cached = cache.get(key)
        if cached is not None:
            cached["cached"] = True
            cached["recommendations"] = [
                ProductRec(product_id=r["product_id"], score=r["score"], rank=r["rank"],
                           segment=r["segment"], source=RecSource(r["source"]),
                           source_detail=RecSourceDetail(r["source_detail"]))
                for r in cached["recommendations"]
            ]
            result = RecommendationResult(**cached)
            if result.customer_id != req.customer_id or result.as_of_date != as_of:
                raise HTTPException(status_code=503, detail="cached recommendation identity mismatch")
            return result
    try:
        result = copurchase.recommend(req.customer_id, as_of, top_n=req.top_n, include_owned=False,
                                      seed_product_ids=seeds)
    except Exception as exc:  # noqa: BLE001
        error_id = uuid.uuid4().hex
        log.error("copurchase_failed", customer_id=req.customer_id, error_id=error_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"copurchase_failed (ref {error_id})") from exc
    if req.use_cache:
        cache.set(key, result.model_dump(mode="json"), ttl=_COPURCHASE_TTL)
    return result


_BATCH_BUDGET_S = 60.0


@app.post("/recommend/batch")
def recommend_batch(req: BatchRequest) -> dict:
    """Batch endpoint (maps to .NET RecommendationsBatchEndpoint). Per-customer errors
    are isolated so one bad id doesn't fail the batch. A wall-clock budget bounds the
    request: uncached compute is 3-6s/client, so an unbounded 500-id batch would hold a
    threadpool worker for tens of minutes; leftover ids are reported as errors instead."""
    results, errors = [], []
    as_of = req.as_of_date.isoformat() if req.as_of_date else None
    started = time.monotonic()
    for index, cid in enumerate(req.customer_ids):
        if time.monotonic() - started > _BATCH_BUDGET_S:
            for rest in req.customer_ids[index:]:
                errors.append({"customer_id": rest, "error": "batch_budget_exhausted"})
            log.warning("recommend_batch_budget_exhausted", processed=index,
                        total=len(req.customer_ids), budget_s=_BATCH_BUDGET_S)
            break
        try:
            results.append(service.get_recommendations(
                customer_id=cid, as_of_date=as_of, top_n=req.top_n,
                include_discovery=req.include_discovery, use_cache=req.use_cache,
                region_scope=req.region_scope,
            ))
        except Exception as exc:  # noqa: BLE001
            error_id = uuid.uuid4().hex
            log.error("recommend_batch_item_failed", customer_id=cid, error_id=error_id,
                      error=str(exc))
            errors.append({"customer_id": cid, "error": f"recommendation_failed (ref {error_id})",
                           "error_id": error_id})
    return {"results": results, "errors": errors, "count": len(results), "failed": len(errors)}


@app.post("/feedback")
def feedback(req: FeedbackRequest) -> dict:
    """Record negative feedback (products a downstream consumer judged a bad recommendation for a
    customer) so the recommender excludes them. Invalidates the customer's copurchase cache so the
    exclusion takes effect on the next call. Used by gba-nba when a manager dismisses / fails to
    sell a cross-sell task.

    Stored under the Client.NetUID / Product.VendorCode natural keys (survives catalog re-mints);
    `added`/`total_negatives` therefore count distinct VendorCodes, not raw ids."""
    if not repo.client_exists(req.customer_id):
        raise HTTPException(status_code=404, detail=f"customer {req.customer_id} was not found")
    missing = sorted(set(req.product_ids) - repo.active_product_ids(req.product_ids))
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"message": "products were not found", "product_ids": missing},
        )
    added = cache.add_negatives(req.customer_id, req.product_ids, ttl=settings.feedback_ttl)
    cache.invalidate_copurchase(req.customer_id)
    log.info("feedback", customer_id=req.customer_id, kind=req.kind,
             products=len(req.product_ids), added=added)
    total = len(cache.get_negative_vendor_codes(req.customer_id))
    return {"customer_id": req.customer_id, "added": added, "total_negatives": total}


@app.delete("/cache/{customer_id}")
def clear_cache(customer_id: int = Path(gt=0)) -> dict:
    return {"deleted": cache.invalidate_customer(customer_id)}
