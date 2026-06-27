"""FastAPI app — GBA Procurement / Replenishment Service."""
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
from app.data import feedback
from app.data import masters
from app.data.db import dispose, get_engine
from app.domain.models import CartReplenishmentPlan, PlanCharts, ProducerPurchasePlan
from app.services.replenishment import policy

log = get_logger("api")
settings = get_settings()

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-procure running OPEN — set INTERNAL_API_KEY")
    log.info("service_starting", service="gba-procure")
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Procurement / Replenishment Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


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


class PlanRequest(BaseModel):
    producer_id: int = Field(..., description="dbo.SupplyOrganization.ID")
    as_of_date: str | None = None
    only_needed: bool = True


class CartPlanRequest(BaseModel):
    as_of_date: str | None = None
    only_needed: bool = True
    limit: int | None = 200
    budget_eur: float | None = None
    method: str = "greedy"
    active_days: int | None = None


class PlanChartsRequest(BaseModel):
    producer_id: int | None = None
    as_of_date: str | None = None
    top_n: int = 15


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "healthy" if db_ok else "degraded", "db_connected": db_ok,
            "redis_connected": cache.health(), "version": "0.1.0", "model_version": "procure-hist120-v1"}


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/plan/producer", response_model=ProducerPurchasePlan)
def plan_producer(req: PlanRequest) -> ProducerPurchasePlan:
    started = time.time()
    try:
        as_of = req.as_of_date or _today()
        key = cache.make_key("producer", req.producer_id, as_of) if req.only_needed else None
        if key is not None:
            cached = cache.get(key)
            if cached is not None:
                METRICS.record_request((time.time() - started) * 1000)
                log.info("plan_cache_hit", producer_id=req.producer_id, items=cached.get("item_count"))
                return ProducerPurchasePlan.model_validate(cached)
        plan = policy.build_plan(req.producer_id, as_of, only_needed=req.only_needed)
        if key is not None:
            cache.set(key, plan.model_dump(mode="json"), ttl=691200)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("plan_built", producer_id=req.producer_id, items=plan.item_count)
        return plan
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("plan_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=500, detail="plan_failed") from exc


@app.post("/plan/cart", response_model=CartReplenishmentPlan)
def plan_cart(req: CartPlanRequest) -> CartReplenishmentPlan:
    started = time.time()
    try:
        as_of = req.as_of_date or _today()
        limit = req.limit if req.limit is not None else 200
        budget = req.budget_eur
        key = (cache.make_key("cart", limit, as_of) if budget is None and req.active_days is None
               else cache.make_key("cartbudget", f"{limit}:{budget}:{req.method}:{req.active_days}", as_of))
        cached = cache.get(key)
        if cached is not None:
            METRICS.record_request((time.time() - started) * 1000)
            log.info("cart_plan_cache_hit", items=cached.get("item_count"))
            return CartReplenishmentPlan.model_validate(cached)
        plan = policy.build_cart_plan(as_of, only_needed=req.only_needed, limit=limit,
                                      budget_eur=budget, method=req.method, active_days=req.active_days)
        cache.set(key, plan.model_dump(mode="json"), ttl=691200)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("cart_plan_built", items=plan.item_count)
        return plan
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("cart_plan_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="cart_plan_failed") from exc


@app.post("/plan/charts", response_model=PlanCharts)
def plan_charts(req: PlanChartsRequest) -> PlanCharts:
    started = time.time()
    try:
        as_of = req.as_of_date or _today()
        top_n = req.top_n
        producer_key = req.producer_id if req.producer_id is not None else "all"
        key = cache.make_key("charts", f"{producer_key}:{top_n}", as_of)
        cached = cache.get(key)
        if cached is not None:
            METRICS.record_request((time.time() - started) * 1000)
            log.info("plan_charts_cache_hit", producer_id=req.producer_id, top_n=top_n)
            return PlanCharts.model_validate(cached)
        charts = policy.build_charts(req.producer_id, as_of, top_n=top_n)
        cache.set(key, charts.model_dump(mode="json"), ttl=691200)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("plan_charts_built", producer_id=req.producer_id, top_n=top_n,
                 top_items=len(charts.top_items), series=len(charts.demand_series))
        return charts
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("plan_charts_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=500, detail="plan_charts_failed") from exc


class ProducerProfileUpdate(BaseModel):
    producer_id: int
    service_level_target: float | None = None
    lead_time_override_days: float | None = None
    ordering_cost_eur: float | None = None
    holding_rate_pct: float | None = None
    autonomy_level: str | None = None
    auto_place_max_eur: float | None = None


class ProductTermsUpdate(BaseModel):
    producer_id: int
    product_id: int
    moq: float | None = None
    order_multiple: float | None = None
    unit_cost_override: float | None = None


@app.get("/masters/producer")
def get_producer_profile(producer_id: int) -> dict:
    return masters.producer_profile(producer_id) or {"producer_id": producer_id}


@app.post("/masters/producer")
def set_producer_profile(req: ProducerProfileUpdate) -> dict:
    try:
        return masters.upsert_producer_profile(req.producer_id, req.model_dump(exclude_none=True))
    except Exception as exc:  # noqa: BLE001
        log.error("producer_profile_upsert_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


@app.post("/masters/seed-terms")
def seed_terms(min_orders: int = 3, overwrite: bool = False) -> dict:
    try:
        return masters.seed_derived_terms(min_orders=min_orders, overwrite=overwrite)
    except Exception as exc:  # noqa: BLE001
        log.error("seed_terms_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


@app.get("/masters/product-terms")
def get_product_terms(producer_id: int) -> dict:
    return {"producer_id": producer_id, "terms": masters.list_product_terms(producer_id)}


@app.post("/masters/product-terms")
def set_product_terms(req: ProductTermsUpdate) -> dict:
    try:
        return masters.upsert_product_terms(
            req.producer_id, req.product_id,
            req.model_dump(exclude_none=True, exclude={"producer_id", "product_id"}),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("product_terms_upsert_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


class FeedbackRequest(BaseModel):
    producer_id: int
    product_id: int
    suggested_qty: float
    final_qty: float
    action: str = Field(description="accept | edit | dismiss")
    abc: str | None = None


@app.post("/feedback")
def record_feedback(req: FeedbackRequest) -> dict:
    try:
        return feedback.record(req.producer_id, req.product_id, req.suggested_qty,
                               req.final_qty, req.action, req.abc, _today())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("feedback_record_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="feedback_store_unavailable") from exc


@app.get("/feedback/learned")
def get_learned_factors(producer_id: int) -> dict:
    return {
        "producer_id": producer_id,
        "factors": feedback.learned_factors(
            producer_id, settings.feedback_min_samples,
            settings.override_factor_min, settings.override_factor_max),
    }


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")
