"""FastAPI app — GBA Client Solvency Service (CreditScore-100, production shell).

Endpoints delegate to app.services.solvency.service (built by the engine phase). The service
module is imported lazily inside handlers so this shell stays importable during scaffolding.
"""
from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data.db import dispose, get_engine
from app.domain.models import (
    BatchScoreRequest,
    ScoreRequest,
    SolvencyCharts,
    SolvencyScore,
)

settings = get_settings()
log = get_logger("api")

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}


def _service():
    import sys
    mod = sys.modules.get("app.services.solvency.service")
    if mod is not None:
        return mod
    from app.services.solvency import service
    return service


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_release_safe("gba-solvency")
    get_engine()  # warm pool
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-solvency running OPEN — set INTERNAL_API_KEY")
    try:
        from app.data import solvency_repository as repo
        drift = repo.synthetic_line_drift_check()
        if not drift["ok"]:
            log.warning("synthetic_line_drift", **drift)
        else:
            log.info("synthetic_line_drift_ok", configured_ids=drift["configured_ids"])
    except Exception as exc:  # noqa: BLE001
        log.warning("synthetic_line_drift_check_failed", error=str(exc))
    log.info("service_starting", model_version=settings.model_version, port=settings.api_port)
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Client Solvency Service", version="0.1.0", lifespan=lifespan)
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
    latency = (time.time() - t) * 1000
    resp.headers["X-Process-Time-Ms"] = str(round(latency, 2))
    METRICS.record_request(latency, error=resp.status_code >= 500)
    return resp


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    synthetic_drift_ok = None
    if db_ok:
        try:
            from app.data import solvency_repository as repo
            synthetic_drift_ok = bool(repo.synthetic_line_drift_check()["ok"])
        except Exception:
            synthetic_drift_ok = None
    return {
        "status": "healthy" if db_ok and synthetic_drift_ok is not False else "degraded",
        "db_connected": db_ok,
        "redis_connected": cache.health(),
        "synthetic_drift_ok": synthetic_drift_ok,
        "version": "0.1.0",
        "model_version": settings.model_version,
    }


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/score", response_model=SolvencyScore)
def score(req: ScoreRequest) -> SolvencyScore:
    if req.client_id is None and req.client_net_uid is None:
        raise HTTPException(status_code=422, detail="client_id or client_net_uid required")
    try:
        return _service().score_client(
            client_id=req.client_id, client_net_uid=req.client_net_uid,
            as_of_date=req.as_of_date, window_months=req.window_months, use_cache=req.use_cache,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("score_failed", client_id=req.client_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"score_failed: {exc}") from exc


@app.post("/score/batch")
def score_batch(req: BatchScoreRequest) -> dict:
    """Per-client errors are isolated so one bad id doesn't fail the batch."""
    svc = _service()
    results, errors = [], []
    for cid in req.client_ids:
        try:
            results.append(svc.score_client(
                client_id=cid, client_net_uid=None, as_of_date=req.as_of_date,
                window_months=req.window_months, use_cache=req.use_cache,
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append({"client_id": cid, "error": str(exc)})
    return {"results": results, "errors": errors, "count": len(results), "failed": len(errors)}


@app.get("/charts/{client_id}", response_model=SolvencyCharts)
def charts(client_id: int, as_of_date: str | None = None, months: int = 12) -> SolvencyCharts:
    try:
        return _service().build_charts(
            client_id=client_id, as_of_date=as_of_date, window_months=months,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("charts_failed", client_id=client_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"charts_failed: {exc}") from exc


@app.delete("/cache/{client_id}")
def clear_cache(client_id: int) -> dict:
    return {"deleted": cache.invalidate_client(client_id)}
