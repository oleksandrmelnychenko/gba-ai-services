"""FastAPI app — GBA Procurement / Replenishment Service."""
from __future__ import annotations

import hmac
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache, feedback, masters
from app.data.db import dispose, get_engine
from app.domain.models import CartReplenishmentPlan, PlanCharts, ProducerPurchasePlan
from app.services.replenishment import policy, worker

log = get_logger("api")
settings = get_settings()

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}


def _canonical_cart_readiness(
    as_of: str,
) -> tuple[bool, str, int | None, dict | None]:
    try:
        source = worker.get_source_readiness(as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("source_readiness_failed", as_of=as_of, error=str(exc))
        return False, "source_readiness_unavailable", None, None
    if source.get("ready") is not True:
        return (
            False,
            str(source.get("reason") or "procurement_source_not_ready"),
            None,
            source,
        )

    marker = cache.get_cart_not_ready(as_of)
    if marker is not None:
        # Source state has recovered since the short negative marker was written.
        cache.clear_cart_not_ready(as_of)

    payload = cache.get(worker.cart_cache_key(as_of))
    if payload is None:
        return False, "canonical_cart_missing", None, source
    item_count = payload.get("item_count")
    if not worker.canonical_cart_payload_is_ready(
        payload,
        source_fingerprint=source.get("source_fingerprint"),
    ):
        return False, "canonical_cart_stale_or_invalid", item_count, source
    return True, "", item_count, source


def _warm_cart_on_startup() -> None:
    """Warm the canonical cart plan in the background so an API restart self-heals:
    the full all-producer build is ~70s cold and would 503 the first /plan/cart
    (past the gba-server proxy timeout). The scheduler warms it daily; this closes
    the gap on every API (re)start without blocking boot."""
    try:
        as_of = _today()
        key = worker.cart_cache_key(as_of)
        with _CART_BUILD_LOCK:
            source = worker.get_source_readiness(as_of, force=True)
            cached = cache.get(key)
            if (
                source.get("ready") is not True
                or not worker.canonical_cart_payload_is_ready(
                    cached,
                    source_fingerprint=source.get("source_fingerprint"),
                )
            ):
                if cached is not None:
                    cache.delete(key)
                stats = worker.warm_cart(as_of=as_of, cart_limit=worker.CART_LIMIT)
                log.info("cart_warm_on_startup_done", **stats)
            else:
                log.info("cart_warm_on_startup_skipped", key=key, reason="already_cached")
            charts_key = cache.make_key("charts", f"all:{worker.CHARTS_TOP_N}", as_of)
            charts_payload = cache.get(charts_key)
            if (
                charts_payload is None
                or charts_payload.get("_source_fingerprint")
                != source.get("source_fingerprint")
            ):
                cstats = worker.warm_charts(as_of=as_of, top_n=worker.CHARTS_TOP_N)
                log.info("charts_warm_on_startup_done", **cstats)
            else:
                log.info("charts_warm_on_startup_skipped", key=charts_key, reason="already_cached")
    except Exception as exc:  # noqa: BLE001
        log.warning("cart_warm_on_startup_failed", error=str(exc))


def _report_mongo_orphans() -> None:
    """Startup warning for Mongo docs keyed on dead pre-wipe IDs (no natural keys to
    remap them to re-minted rows, so the data is retained and only surfaced here)."""
    try:
        report = masters.orphan_report()
        if not report:
            return
        for coll_name, r in report.items():
            if r["orphaned"]:
                log.warning("mongo_orphan_docs", collection=coll_name, **r,
                            note="dead pre-wipe IDs; no natural keys to remap; data retained")
            else:
                log.info("mongo_orphan_check_clean", collection=coll_name, **r)
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo_orphan_check_failed", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-procure running OPEN — set INTERNAL_API_KEY")
    threading.Thread(target=_warm_cart_on_startup, daemon=True, name="cart-warm").start()
    threading.Thread(target=_report_mongo_orphans, daemon=True, name="mongo-orphan-check").start()
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
    producer_id: int = Field(..., strict=True, gt=0, description="dbo.Client.ID")
    as_of_date: date | None = None
    only_needed: bool = True


class CartPlanRequest(BaseModel):
    as_of_date: date | None = None
    only_needed: bool = True
    limit: int | None = Field(default=None, strict=True, ge=0, le=1000)
    budget_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    method: Literal["greedy", "milp"] = "greedy"
    active_days: int | None = Field(default=None, strict=True, ge=1, le=730)


class PlanChartsRequest(BaseModel):
    producer_id: int | None = Field(default=None, strict=True, gt=0)
    as_of_date: date | None = None
    top_n: int = Field(default=15, strict=True, ge=1, le=100)


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    redis_ok = cache.health()
    business_ready = False
    business_reason = "db_unavailable" if not db_ok else "redis_unavailable"
    canonical_cart_items = None
    source_readiness = None
    if db_ok and redis_ok:
        (
            business_ready,
            business_reason,
            canonical_cart_items,
            source_readiness,
        ) = (
            _canonical_cart_readiness(_today())
        )
    return {
        "status": "healthy" if db_ok and redis_ok and business_ready else "degraded",
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        "business_ready": business_ready,
        "business_reason": business_reason or None,
        "canonical_cart_items": canonical_cart_items,
        "source_readiness": source_readiness,
        "version": "0.1.0",
        "model_version": "procure-hist120-v1",
    }


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/plan/producer", response_model=ProducerPurchasePlan)
def plan_producer(req: PlanRequest) -> ProducerPurchasePlan:
    started = time.time()
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = (
            worker.require_source_readiness(as_of)
            if req.as_of_date is None
            else None
        )
        key = cache.make_key("producer", req.producer_id, as_of) if req.only_needed else None
        if key is not None:
            cached = cache.get(key)
            if (
                cached is not None
                and (
                    source is None
                    or cached.get("_source_fingerprint")
                    == source.get("source_fingerprint")
                )
            ):
                METRICS.record_request((time.time() - started) * 1000)
                log.info("plan_cache_hit", producer_id=req.producer_id, items=cached.get("item_count"))
                return ProducerPurchasePlan.model_validate(cached)
            if cached is not None:
                cache.delete(key)
        plan = policy.build_plan(req.producer_id, as_of, only_needed=req.only_needed)
        if key is not None:
            payload = plan.model_dump(mode="json")
            if source is not None:
                payload["_source_fingerprint"] = source.get("source_fingerprint")
            cache.set(key, payload, ttl=691200)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("plan_built", producer_id=req.producer_id, items=plan.item_count)
        return plan
    except worker.ProcurementBusinessReadinessError as exc:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        raise HTTPException(status_code=503, detail="procurement_business_data_not_ready") from exc
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        err_id = uuid.uuid4().hex
        log.error("plan_failed", producer_id=req.producer_id, error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"plan_failed:{err_id}") from exc


# Single-flight: a cold cart build is tens of seconds of DB work — when the as_of
# rolls at midnight (or Redis was flushed), concurrent requests must not each run
# their own build. One builds, the rest wait on the lock and re-read the cache.
_CART_BUILD_LOCK = threading.Lock()


@app.post("/plan/cart", response_model=CartReplenishmentPlan)
def plan_cart(req: CartPlanRequest) -> CartReplenishmentPlan:
    started = time.time()
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = (
            worker.require_source_readiness(as_of)
            if req.as_of_date is None
            else None
        )
        limit = req.limit
        # budget <= 0 means "no budget limit" (the warehouse lens sends 0) — treat it as
        # canonical so it hits the scheduler-warmed key instead of a cold per-request build
        # (the full all-producer plan is ~70s cold, past the gba-server 60s proxy timeout).
        budget = req.budget_eur if req.budget_eur and req.budget_eur > 0 else None
        # Canonical = exactly what the scheduler warms (only_needed=True, no budget/window).
        # Variants carry every plan-shaping parameter in the key (a shared key once served
        # the wrong only_needed plan for 8 days) and live 1h so the slider can't grow Redis.
        canonical = (
            req.as_of_date is None
            and budget is None
            and req.active_days is None
            and req.only_needed
            and limit == worker.CART_LIMIT
        )
        key = (worker.cart_cache_key(as_of, limit) if canonical
               else cache.make_key(
                   "cartbudget",
                   f"{limit if limit is not None else 'all'}:"
                   f"{budget}:{req.method}:{req.active_days}:{int(req.only_needed)}",
                   as_of,
               ))
        cached = cache.get(key)
        cache_valid = cached is not None and (
            source is None
            or cached.get("_source_fingerprint") == source.get("source_fingerprint")
        )
        if canonical and cache_valid:
            cache_valid = worker.canonical_cart_payload_is_ready(
                cached,
                source_fingerprint=source.get("source_fingerprint") if source else None,
            )
        if cached is not None and not cache_valid:
            log.warning(
                "stale_or_invalid_cart_cache_discarded",
                as_of=as_of,
                items=cached.get("item_count"),
            )
            cache.delete(key)
            cached = None
        if cached is not None:
            METRICS.record_request((time.time() - started) * 1000)
            log.info("cart_plan_cache_hit", items=cached.get("item_count"))
            return CartReplenishmentPlan.model_validate(cached)
        with _CART_BUILD_LOCK:
            if req.as_of_date is None:
                source = worker.require_source_readiness(as_of, force=True)
            cached = cache.get(key)
            cache_valid = cached is not None and (
                source is None
                or cached.get("_source_fingerprint") == source.get("source_fingerprint")
            )
            if canonical and cache_valid:
                cache_valid = worker.canonical_cart_payload_is_ready(
                    cached,
                    source_fingerprint=source.get("source_fingerprint") if source else None,
                )
            if cached is not None and not cache_valid:
                cache.delete(key)
                cached = None
            if cached is not None:
                METRICS.record_request((time.time() - started) * 1000)
                log.info("cart_plan_cache_hit", items=cached.get("item_count"))
                return CartReplenishmentPlan.model_validate(cached)
            plan = policy.build_cart_plan(as_of, only_needed=req.only_needed, limit=limit,
                                          budget_eur=budget, method=req.method,
                                          active_days=req.active_days,
                                          source_fingerprint=(
                                              source.get("source_fingerprint")
                                              if source else None
                                          ))
            if canonical:
                worker.cache_canonical_cart_plan(
                    plan,
                    as_of,
                    cart_limit=limit,
                    source_snapshot=source,
                )
            else:
                payload = plan.model_dump(mode="json")
                if source is not None:
                    payload["_source_fingerprint"] = source.get("source_fingerprint")
                cache.set(key, payload, ttl=3600)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("cart_plan_built", items=plan.item_count)
        return plan
    except worker.ProcurementBusinessReadinessError as exc:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.warning("cart_plan_business_not_ready", as_of=as_of, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="cart_business_data_not_ready",
        ) from exc
    except HTTPException:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        raise
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        err_id = uuid.uuid4().hex
        log.error("cart_plan_failed", error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"cart_plan_failed:{err_id}") from exc


@app.post("/plan/charts", response_model=PlanCharts)
def plan_charts(req: PlanChartsRequest) -> PlanCharts:
    started = time.time()
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = (
            worker.require_source_readiness(as_of)
            if req.as_of_date is None
            else None
        )
        top_n = req.top_n
        producer_key = req.producer_id if req.producer_id is not None else "all"
        key = cache.make_key("charts", f"{producer_key}:{top_n}", as_of)
        cached = cache.get(key)
        if (
            cached is not None
            and (
                source is None
                or cached.get("_source_fingerprint")
                == source.get("source_fingerprint")
            )
        ):
            METRICS.record_request((time.time() - started) * 1000)
            log.info("plan_charts_cache_hit", producer_id=req.producer_id, top_n=top_n)
            return PlanCharts.model_validate(cached)
        if cached is not None:
            cache.delete(key)
        charts = policy.build_charts(
            req.producer_id,
            as_of,
            top_n=top_n,
            source_fingerprint=source.get("source_fingerprint") if source else None,
        )
        payload = charts.model_dump(mode="json")
        if source is not None:
            payload["_source_fingerprint"] = source.get("source_fingerprint")
        cache.set(key, payload, ttl=691200)
        METRICS.record_request((time.time() - started) * 1000)
        log.info("plan_charts_built", producer_id=req.producer_id, top_n=top_n,
                 top_items=len(charts.top_items), series=len(charts.demand_series))
        return charts
    except worker.ProcurementBusinessReadinessError as exc:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        raise HTTPException(status_code=503, detail="procurement_business_data_not_ready") from exc
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        err_id = uuid.uuid4().hex
        log.error("plan_charts_failed", producer_id=req.producer_id, error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"plan_charts_failed:{err_id}") from exc


class ProducerProfileUpdate(BaseModel):
    producer_id: int = Field(strict=True, gt=0)
    service_level_target: float | None = Field(
        default=None, gt=0, lt=1, allow_inf_nan=False
    )
    lead_time_override_days: float | None = Field(
        default=None, ge=0, le=730, allow_inf_nan=False
    )
    ordering_cost_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    holding_rate_pct: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    autonomy_level: int | None = Field(default=None, strict=True, ge=0, le=2)
    auto_place_max_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ProductTermsUpdate(BaseModel):
    producer_id: int = Field(strict=True, gt=0)
    product_id: int = Field(strict=True, gt=0)
    moq: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    order_multiple: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit_cost_override: float | None = Field(default=None, ge=0, allow_inf_nan=False)


@app.get("/masters/producer")
def get_producer_profile(producer_id: int = Query(gt=0)) -> dict:
    return masters.producer_profile(producer_id) or {"producer_id": producer_id}


@app.post("/masters/producer")
def set_producer_profile(req: ProducerProfileUpdate) -> dict:
    try:
        result = masters.upsert_producer_profile(req.producer_id, req.model_dump(exclude_none=True))
        # Cached plans embed the old profile for up to 8 days — drop them so the
        # edit is visible on the very next plan request.
        cache.invalidate_plans(req.producer_id)
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("producer_profile_upsert_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


@app.post("/masters/seed-terms")
def seed_terms(
    min_orders: int = Query(default=3, ge=1, le=1000),
    overwrite: bool = False,
) -> dict:
    try:
        result = masters.seed_derived_terms(min_orders=min_orders, overwrite=overwrite)
        cache.invalidate_plans()
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("seed_terms_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


@app.get("/masters/product-terms")
def get_product_terms(producer_id: int = Query(gt=0)) -> dict:
    return {"producer_id": producer_id, "terms": masters.list_product_terms(producer_id)}


@app.post("/masters/product-terms")
def set_product_terms(req: ProductTermsUpdate) -> dict:
    try:
        result = masters.upsert_product_terms(
            req.producer_id, req.product_id,
            req.model_dump(exclude_none=True, exclude={"producer_id", "product_id"}),
        )
        cache.invalidate_plans(req.producer_id)
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("product_terms_upsert_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="masters_store_unavailable") from exc


class FeedbackRequest(BaseModel):
    producer_id: int = Field(strict=True, gt=0)
    product_id: int = Field(strict=True, gt=0)
    suggested_qty: float = Field(ge=0, allow_inf_nan=False)
    final_qty: float = Field(ge=0, allow_inf_nan=False)
    action: Literal["accept", "edit", "dismiss"]
    abc: Literal["A", "B", "C"] | None = None


@app.post("/feedback")
def record_feedback(req: FeedbackRequest) -> dict:
    try:
        result = feedback.record(req.producer_id, req.product_id, req.suggested_qty,
                                 req.final_qty, req.action, req.abc, _today())
        cache.invalidate_plans(req.producer_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("feedback_record_failed", producer_id=req.producer_id, error=str(exc))
        raise HTTPException(status_code=503, detail="feedback_store_unavailable") from exc


@app.get("/feedback/learned")
def get_learned_factors(producer_id: int = Query(gt=0)) -> dict:
    return {
        "producer_id": producer_id,
        "factors": feedback.learned_factors(
            producer_id, settings.feedback_min_samples,
            settings.override_factor_min, settings.override_factor_max),
    }


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")
