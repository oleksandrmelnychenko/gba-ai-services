"""FastAPI app — GBA Pricing Service (A+B price/discount optimization, production shell).

Endpoints delegate to app.services.pricing.service (built by the engine phase). The service
module is imported lazily inside handlers so this shell stays importable during scaffolding.
"""
from __future__ import annotations

import hmac
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import pricing_repository as repo
from app.data.db import dispose, get_engine
from app.domain.models import (
    BatchPriceRequest,
    PriceRecommendation,
    PriceRequest,
)

settings = get_settings()
log = get_logger("api")

_OPEN_PATHS = {"/health", "/ready"}


def _is_valid_uid(value: str | None) -> bool:
    if value is None:
        return True
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _service():
    import sys
    mod = sys.modules.get("app.services.pricing.service")
    if mod is not None:
        return mod
    from app.services.pricing import service
    return service


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()  # warm pool
    from app.data import pricing_repository as repo
    repo.synthetic_product_id()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-pricing running OPEN — set INTERNAL_API_KEY")
    log.info("service_starting", model_version=settings.model_version, port=settings.api_port)
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Pricing Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST", "DELETE"],
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
    latency = (time.time() - t) * 1000
    resp.headers["X-Process-Time-Ms"] = str(round(latency, 2))
    METRICS.record_request(latency, error=resp.status_code >= 500)
    return resp


@app.get("/health")
def health() -> dict:
    return _health_snapshot()


def _health_snapshot() -> dict:
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
    }
    if db_ok:
        try:
            source = repo.source_readiness(settings.max_source_lag_days)
        except Exception as exc:  # noqa: BLE001
            log.warning("source_readiness_failed", error=str(exc))
            source = {
                "business_ready": False,
                "reasons": ["source_query_failed"],
            }
    business_ready = source.get("business_ready") is True
    healthy = db_ok and redis_ok and business_ready
    return {
        "status": "healthy" if healthy else "degraded",
        "business_ready": business_ready,
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        "source": source,
        "version": "0.1.0",
        "model_version": settings.model_version,
    }


@app.get("/ready")
def ready() -> JSONResponse:
    snapshot = _health_snapshot()
    is_ready = snapshot["status"] == "healthy"
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={**snapshot, "status": "ready" if is_ready else "not_ready"},
    )


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/price", response_model=PriceRecommendation)
def price(req: PriceRequest) -> PriceRecommendation:
    if req.product_id is None and req.product_net_uid is None:
        raise HTTPException(status_code=422, detail="product_id or product_net_uid required")
    if not _is_valid_uid(req.client_agreement_net_uid):
        raise HTTPException(status_code=422, detail="malformed client_agreement_net_uid")
    if not _is_valid_uid(req.product_net_uid):
        raise HTTPException(status_code=422, detail="malformed product_net_uid")
    try:
        return _service().recommend_price(
            product_id=req.product_id,
            product_net_uid=req.product_net_uid,
            client_agreement_net_uid=req.client_agreement_net_uid,
            culture=req.culture,
            with_vat=req.with_vat,
            target_margin_pct=req.target_margin_pct,
            as_of_date=req.as_of_date.isoformat() if req.as_of_date else None,
            use_cache=req.use_cache,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        eid = uuid.uuid4().hex
        log.error("price_failed", error_id=eid, product_id=req.product_id,
                  client_agreement_net_uid=req.client_agreement_net_uid, error=str(exc))
        raise HTTPException(status_code=500, detail=f"internal error (ref {eid})") from exc


@app.post("/price/batch")
def price_batch(req: BatchPriceRequest) -> dict:
    """Per-item errors are isolated so one bad pair doesn't fail the batch."""
    svc = _service()
    as_of = req.as_of_date.isoformat() if req.as_of_date else None
    results, errors = [], []
    for item in req.items:
        if not _is_valid_uid(item.client_agreement_net_uid):
            errors.append({
                "product_id": item.product_id,
                "product_net_uid": item.product_net_uid,
                "client_agreement_net_uid": item.client_agreement_net_uid,
                "error": "malformed client_agreement_net_uid",
            })
            continue
        if not _is_valid_uid(item.product_net_uid):
            errors.append({
                "product_id": item.product_id,
                "product_net_uid": item.product_net_uid,
                "client_agreement_net_uid": item.client_agreement_net_uid,
                "error": "malformed product_net_uid",
            })
            continue
        try:
            results.append(svc.recommend_price(
                product_id=item.product_id,
                product_net_uid=item.product_net_uid,
                client_agreement_net_uid=item.client_agreement_net_uid,
                culture=req.culture,
                with_vat=req.with_vat,
                target_margin_pct=req.target_margin_pct,
                as_of_date=as_of,
                use_cache=req.use_cache,
            ))
        except LookupError as exc:
            errors.append({
                "product_id": item.product_id,
                "product_net_uid": item.product_net_uid,
                "client_agreement_net_uid": item.client_agreement_net_uid,
                "error": str(exc),
            })
        except Exception as exc:  # noqa: BLE001
            eid = uuid.uuid4().hex
            log.error("price_batch_item_failed", error_id=eid,
                      product_id=item.product_id, product_net_uid=item.product_net_uid,
                      client_agreement_net_uid=item.client_agreement_net_uid, error=str(exc))
            errors.append({
                "product_id": item.product_id,
                "product_net_uid": item.product_net_uid,
                "client_agreement_net_uid": item.client_agreement_net_uid,
                "error": f"internal error (ref {eid})",
            })
    return {"results": results, "errors": errors, "count": len(results), "failed": len(errors)}


@app.delete("/cache/{product}/{client_agreement_net_uid}")
def clear_cache(product: str, client_agreement_net_uid: str) -> dict:
    # Keys are stored under the numeric product ID; a NetUID here would silently
    # match nothing and leave the stale price cached. Resolve it first.
    resolved: int | str = product
    if not product.isdigit():
        try:
            from app.data import pricing_repository as repo

            row = repo.resolve_product(None, product)
            if row:
                resolved = row["id"]
        except Exception as exc:  # noqa: BLE001
            log.warning("cache_clear_resolve_failed", product=product, error=str(exc))
    return {"deleted": cache.invalidate(resolved, client_agreement_net_uid)}
