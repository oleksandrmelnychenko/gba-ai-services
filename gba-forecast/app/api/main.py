"""FastAPI app — GBA Sales Forecast Service (client / product monthly sales projection)."""

from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import signals_repository as sig
from app.data.db import dispose, get_engine
from app.services import forecast as fc

log = get_logger("api")
settings = get_settings()

_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_configuration()
    get_engine()
    if not settings.internal_api_key:
        log.warning(
            "internal_api_key_not_set",
            note="gba-forecast running OPEN because ALLOW_OPEN_INTERNAL_API=true",
        )
    log.info("service_starting", service="gba-forecast")
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Sales Forecast Service", version="0.1.0", lifespan=lifespan)
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


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@app.get("/health")
def health() -> dict:
    status, db_ok = _database_health()
    return {
        "status": status,
        "db_connected": db_ok,
        "cache_connected": cache.health(),
        "version": "0.1.0",
        "model_version": settings.model_version,
    }


@app.get("/ready")
def ready() -> JSONResponse:
    status, db_ok = _database_health()
    cache_ok = cache.health()
    ready_ok = status == "healthy"
    body = {
        "status": "ready" if ready_ok else "not_ready",
        "db_connected": db_ok,
        "cache_connected": cache_ok,
        "model_version": settings.model_version,
    }
    return JSONResponse(status_code=200 if ready_ok else 503, content=body)


def _database_health() -> tuple[str, bool]:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return ("healthy" if db_ok else "degraded", db_ok)


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.get("/forecast/sales")
def forecast_sales(
    client_net_id: UUID | None = None,
    product_net_id: UUID | None = None,
    months: int | None = Query(default=None, ge=1),
) -> dict:
    """Monthly sales forecast (EUR) for a client, a product, or both.

    Computes only the series whose id is supplied:
      - client_net_id        -> ByClient
      - product_net_id       -> ByProduct
      - both                 -> ByClient, ByProduct, and ByClientAndProduct (that client buying
                                that product)
    Each populated key is an array of {SaleAmount, MonthNameUK} for the next N months
    (`months`, default config). A series with too little history yields [] for its key
    (the console shows «немає даних») — never crashes. Internal-key gated when configured.
    """
    started = time.time()
    horizon = _resolve_horizon(months)
    as_of_str = _today()
    cache_key = _sales_cache_key(client_net_id, product_net_id, horizon, as_of_str)
    cached = cache.get(cache_key)
    if cached is not None:
        METRICS.record_request((time.time() - started) * 1000)
        return cached

    as_of = datetime.now(UTC).date()
    out: dict[str, list] = {"ByClient": [], "ByProduct": [], "ByClientAndProduct": []}
    try:
        client_id = sig.client_id_for_netuid(str(client_net_id)) if client_net_id else None
        product_id = sig.product_id_for_netuid(str(product_net_id)) if product_net_id else None

        if client_id is not None:
            hist = sig.to_series(sig.monthly_sales_by_client(client_id, as_of_str, settings.history_months))
            out["ByClient"] = fc.forecast_points(hist, as_of, settings, horizon)

        if product_id is not None:
            hist = sig.to_series(sig.monthly_sales_by_product(product_id, as_of_str, settings.history_months))
            out["ByProduct"] = fc.forecast_points(hist, as_of, settings, horizon)

        if client_id is not None and product_id is not None:
            hist = sig.to_series(
                sig.monthly_sales_by_client_and_product(
                    client_id, product_id, as_of_str, settings.history_months
                )
            )
            out["ByClientAndProduct"] = fc.forecast_points(hist, as_of, settings, horizon)

        METRICS.record_request((time.time() - started) * 1000)
        cache.set(cache_key, out)
        return out
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error(
            "forecast_sales_failed",
            client_net_id=client_net_id,
            product_net_id=product_net_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="forecast_sales_failed") from exc


def _resolve_horizon(months: int | None) -> int:
    horizon = months or settings.forecast_horizon_months
    if horizon > settings.max_forecast_horizon_months:
        raise HTTPException(
            status_code=422,
            detail=f"months must be <= {settings.max_forecast_horizon_months}",
        )
    return horizon


def _sales_cache_key(
    client_net_id: UUID | None,
    product_net_id: UUID | None,
    horizon: int,
    as_of: str,
) -> str:
    client_part = str(client_net_id).lower() if client_net_id else "none"
    product_part = str(product_net_id).lower() if product_net_id else "none"
    entity = f"{client_part}:{product_part}"
    return cache.make_key(
        "sales",
        entity,
        horizon,
        settings.model_version,
        settings.forecast_method,
        settings.history_months,
        settings.min_history_months,
        as_of,
    )
