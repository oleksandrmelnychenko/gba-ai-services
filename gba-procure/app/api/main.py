"""FastAPI app — GBA Procurement / Replenishment Service."""

from __future__ import annotations

import hmac
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.history import require_supported_as_of, rolling_coverage
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache, feedback, masters
from app.data.db import dispose, get_engine
from app.domain.models import (
    MODEL_VERSION,
    CartReplenishmentPlan,
    PlanCharts,
    ProducerPurchasePlan,
)
from app.services.replenishment import policy, worker
from app.services.replenishment.warm_requests import REQUEST_WARMS

log = get_logger("api")
settings = get_settings()
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"

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
    """Share the bounded job used by cold requests; startup owns no second build."""
    _queue_canonical_cart(_today())


def _cache_matches(payload: dict | None, source: dict | None) -> bool:
    return worker.plan_cost_metadata_is_ready(payload) and (
        source is None or payload.get("_source_fingerprint") == source.get("source_fingerprint")
    )


def _save_captured(key: str, plan, source: dict | None, ttl: int) -> None:
    payload = plan.model_dump(mode="json")
    if source is not None:
        payload["_source_fingerprint"] = source.get("source_fingerprint")
    if not worker.plan_cost_metadata_is_ready(payload):
        raise worker.ProcurementBusinessReadinessError("captured_cost_metadata_invalid")
    if not cache.set(key, payload, ttl=ttl):
        raise worker.ProcurementBusinessReadinessError("captured_plan_cache_write_failed")


def _queue_canonical_cart(as_of: str) -> None:
    key = worker.cart_cache_key(as_of)

    def build() -> None:
        source = worker.require_source_readiness(as_of, force=True)
        if not worker.canonical_cart_payload_is_ready(
            cache.get(key), source_fingerprint=source.get("source_fingerprint")
        ):
            worker.warm_cart(as_of=as_of, cart_limit=worker.CART_LIMIT)
        charts_key = cache.make_key("charts", f"all:{worker.CHARTS_TOP_N}", as_of)
        if not worker.canonical_charts_payload_is_ready(
            cache.get(charts_key), source_fingerprint=source.get("source_fingerprint")
        ):
            worker.warm_charts(as_of=as_of, top_n=worker.CHARTS_TOP_N)

    REQUEST_WARMS.submit(key, build)


def _report_mongo_orphans() -> None:
    """Startup warning for Mongo docs keyed on dead pre-wipe IDs (no natural keys to
    remap them to re-minted rows, so the data is retained and only surfaced here)."""
    try:
        report = masters.orphan_report()
        if not report:
            return
        for coll_name, r in report.items():
            if r["orphaned"]:
                log.warning(
                    "mongo_orphan_docs",
                    collection=coll_name,
                    **r,
                    note="dead pre-wipe IDs; no natural keys to remap; data retained",
                )
            else:
                log.info("mongo_orphan_check_clean", collection=coll_name, **r)
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo_orphan_check_failed", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-procure running OPEN — set INTERNAL_API_KEY")
    if settings.warm_on_startup:
        threading.Thread(target=_warm_cart_on_startup, daemon=True, name="cart-warm").start()
    threading.Thread(target=_report_mongo_orphans, daemon=True, name="mongo-orphan-check").start()
    log.info("service_starting", service="gba-procure")
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Procurement / Replenishment Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
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


class PlanRequest(BaseModel):
    producer_id: int = Field(..., strict=True, gt=0, description="dbo.Client.ID")
    as_of_date: date | None = None
    only_needed: bool = True

    @field_validator("as_of_date")
    @classmethod
    def validate_as_of_date(cls, value: date | None) -> date | None:
        if value is not None:
            require_supported_as_of(value)
        return value


class CartPlanRequest(BaseModel):
    as_of_date: date | None = None
    only_needed: bool = True
    limit: int | None = Field(default=None, strict=True, ge=0, le=1000)
    budget_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    method: Literal["greedy", "milp"] = "greedy"
    active_days: int | None = Field(default=None, strict=True, ge=1, le=730)

    @field_validator("as_of_date")
    @classmethod
    def validate_as_of_date(cls, value: date | None) -> date | None:
        if value is not None:
            require_supported_as_of(value)
        return value


class PlanChartsRequest(BaseModel):
    producer_id: int | None = Field(default=None, strict=True, gt=0)
    as_of_date: date | None = None
    top_n: int = Field(default=15, strict=True, ge=1, le=100)

    @field_validator("as_of_date")
    @classmethod
    def validate_as_of_date(cls, value: date | None) -> date | None:
        if value is not None:
            require_supported_as_of(value)
        return value


@app.get("/health")
def health() -> dict:
    coverage = rolling_coverage(_today(), settings.history_days)
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
        ) = _canonical_cart_readiness(_today())
    source_history_start = settings.source_history_start_date.isoformat()
    source_history_contract_ready = source_history_start == _EXPECTED_SOURCE_HISTORY_START
    source_readiness = {
        **(source_readiness or {}),
        "source_history_start": source_history_start,
    }
    if not source_history_contract_ready:
        source_readiness["ready"] = False
        source_readiness["reason"] = "source_history_start_mismatch"
        business_ready = False
        business_reason = "source_history_start_mismatch"
    return {
        "status": "healthy" if db_ok and redis_ok and business_ready else "degraded",
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        "business_ready": business_ready,
        "business_reason": business_reason or None,
        "canonical_cart_items": canonical_cart_items,
        "source_readiness": source_readiness,
        **coverage.as_metadata(),
        "history_scope": "demand",
        "history_not_applicable": ["inventory", "reservations", "purchase_costs"],
        "source_history_contract_ready": source_history_contract_ready,
        "version": "0.1.0",
        "model_version": MODEL_VERSION,
    }


@app.get("/ready")
def ready() -> JSONResponse:
    snapshot = health()
    is_ready = snapshot["status"] == "healthy"
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={**snapshot, "status": "ready" if is_ready else "not_ready"},
    )


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/plan/producer", response_model=ProducerPurchasePlan)
def plan_producer(req: PlanRequest) -> ProducerPurchasePlan:
    started = time.time()
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = worker.require_source_readiness(as_of) if req.as_of_date is None else None
        key = cache.make_key("producer" if req.only_needed else "producerfull", req.producer_id, as_of)
        cached = cache.get(key)
        if _cache_matches(cached, source):
            return ProducerPurchasePlan.model_validate(cached)

        def build() -> None:
            current = worker.require_source_readiness(as_of, force=True) if req.as_of_date is None else None
            plan = policy.build_plan(req.producer_id, as_of, only_needed=req.only_needed)
            _save_captured(key, plan, current, 691200 if req.only_needed else 3600)

        REQUEST_WARMS.submit(key, build)
        raise worker.ProcurementBusinessReadinessError("captured_plan_preparing")
    except worker.ProcurementBusinessReadinessError as exc:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        raise HTTPException(status_code=503, detail="procurement_business_data_not_ready") from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("plan_failed", error_id=err_id, failure_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail=f"plan_failed:{err_id}") from exc


@app.post("/plan/cart", response_model=CartReplenishmentPlan)
def plan_cart(req: CartPlanRequest) -> CartReplenishmentPlan:
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = worker.require_source_readiness(as_of) if req.as_of_date is None else None
        # Existing warehouse requests use zero to mean unbounded, not a zero ceiling.
        budget = req.budget_eur if req.budget_eur and req.budget_eur > 0 else None
        canonical = (
            req.as_of_date is None
            and budget is None
            and req.active_days is None
            and req.only_needed
            and req.limit == worker.CART_LIMIT
        )
        key = (
            worker.cart_cache_key(as_of)
            if canonical
            else cache.make_key(
                "cartbudget",
                f"{req.limit if req.limit is not None else 'all'}:"
                f"{budget}:{req.method}:{req.active_days}:{int(req.only_needed)}",
                as_of,
            )
        )
        cached = cache.get(key)
        if _cache_matches(cached, source) and (
            not canonical
            or worker.canonical_cart_payload_is_ready(
                cached, source_fingerprint=source.get("source_fingerprint") if source else None
            )
        ):
            return CartReplenishmentPlan.model_validate(cached)
        # Budget/limit transforms retain the captured input and its original read window.
        from_canonical = req.as_of_date is None and req.active_days is None and req.only_needed
        if from_canonical:
            captured = cache.get(worker.cart_cache_key(as_of))
            if worker.canonical_cart_payload_is_ready(
                captured, source_fingerprint=source.get("source_fingerprint") if source else None
            ):
                plan = policy.captured_cart_variant(
                    CartReplenishmentPlan.model_validate(captured),
                    budget_eur=budget,
                    method=req.method,
                    limit=req.limit,
                )
                _save_captured(key, plan, source, 3600)
                return plan
            _queue_canonical_cart(as_of)
        else:

            def build() -> None:
                current = (
                    worker.require_source_readiness(as_of, force=True) if req.as_of_date is None else None
                )
                plan = policy.build_cart_plan(
                    as_of,
                    only_needed=req.only_needed,
                    limit=req.limit,
                    budget_eur=budget,
                    method=req.method,
                    active_days=req.active_days,
                    source_fingerprint=current.get("source_fingerprint") if current else None,
                )
                _save_captured(key, plan, current, 3600)

            REQUEST_WARMS.submit(key, build)
        raise worker.ProcurementBusinessReadinessError("captured_plan_preparing")
    except worker.ProcurementBusinessReadinessError as exc:
        raise HTTPException(status_code=503, detail="cart_business_data_not_ready") from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("cart_plan_failed", error_id=err_id, failure_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail=f"cart_plan_failed:{err_id}") from exc


@app.post("/plan/charts", response_model=PlanCharts)
def plan_charts(req: PlanChartsRequest) -> PlanCharts:
    try:
        as_of = req.as_of_date.isoformat() if req.as_of_date else _today()
        source = worker.require_source_readiness(as_of) if req.as_of_date is None else None
        producer_key = req.producer_id if req.producer_id is not None else "all"
        key = cache.make_key("charts", f"{producer_key}:{req.top_n}", as_of)
        cached = cache.get(key)
        if _cache_matches(cached, source):
            charts = PlanCharts.model_validate(cached)
            if charts.population is None:
                plan_key = worker.cart_cache_key(as_of) if req.producer_id is None else cache.make_key("producerfull", req.producer_id, as_of)
                captured = cache.get(plan_key)
                if (_cache_matches(captured, source) and captured.get("_source_fingerprint") == cached.get("_source_fingerprint")
                        and bool(cached.get("_source_fingerprint"))):
                    plan_type = CartReplenishmentPlan if req.producer_id is None else ProducerPurchasePlan
                    charts = policy.enrich_captured_charts(charts, plan_type.model_validate(captured))
            return charts

        def build() -> None:
            current = worker.require_source_readiness(as_of, force=True) if req.as_of_date is None else None
            plan_input = None
            if req.producer_id is None:
                captured = cache.get(worker.cart_cache_key(as_of))
                if worker.canonical_cart_payload_is_ready(
                    captured, source_fingerprint=current.get("source_fingerprint") if current else None
                ):
                    plan_input = CartReplenishmentPlan.model_validate(captured)
                elif req.as_of_date is None:
                    raise worker.ProcurementBusinessReadinessError("canonical_cart_preparing")
            else:
                captured = cache.get(cache.make_key("producerfull", req.producer_id, as_of))
                if _cache_matches(captured, current):
                    plan_input = ProducerPurchasePlan.model_validate(captured)
            charts = policy.build_charts(
                req.producer_id,
                as_of,
                top_n=req.top_n,
                source_fingerprint=current.get("source_fingerprint") if current else None,
                plan_input=plan_input,
            )
            _save_captured(key, charts, current, 691200)

        if (
            req.producer_id is None
            and req.as_of_date is None
            and not worker.canonical_cart_payload_is_ready(
                cache.get(worker.cart_cache_key(as_of)),
                source_fingerprint=source.get("source_fingerprint") if source else None,
            )
        ):
            _queue_canonical_cart(as_of)
        REQUEST_WARMS.submit(key, build)
        raise worker.ProcurementBusinessReadinessError("captured_plan_preparing")
    except worker.ProcurementBusinessReadinessError as exc:
        raise HTTPException(status_code=503, detail="procurement_business_data_not_ready") from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("plan_charts_failed", error_id=err_id, failure_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail=f"plan_charts_failed:{err_id}") from exc


class ProducerProfileUpdate(BaseModel):
    producer_id: int = Field(strict=True, gt=0)
    service_level_target: float | None = Field(default=None, gt=0, lt=1, allow_inf_nan=False)
    lead_time_override_days: float | None = Field(default=None, ge=0, le=730, allow_inf_nan=False)
    ordering_cost_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    holding_rate_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
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
            req.producer_id,
            req.product_id,
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
        result = feedback.record(
            req.producer_id, req.product_id, req.suggested_qty, req.final_qty, req.action, req.abc, _today()
        )
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
            producer_id,
            settings.feedback_min_samples,
            settings.override_factor_min,
            settings.override_factor_max,
        ),
    }


def _today() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
