"""FastAPI app — GBA Client Solvency Service (CreditScore-100, production shell).

Endpoints delegate to app.services.solvency.service (built by the engine phase). The service
module is imported lazily inside handlers so this shell stays importable during scaffolding.
"""
from __future__ import annotations

import hmac
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data.db import dispose, get_engine
from app.domain.models import (
    BatchScoreRequest,
    ClientIdentityMismatchError,
    ScoreRequest,
    SolvencyCharts,
    SolvencyScore,
)

settings = get_settings()
log = get_logger("api")
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health", "/ready"}


def _service():
    import sys
    mod = sys.modules.get("app.services.solvency.service")
    if mod is not None:
        return mod
    from app.services.solvency import service
    return service


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    # Background drift warmer: populate the in-process drift cache (off the request path) so
    # /health can surface model drift. drift_report() self-throttles via its own refresh window,
    # so the hourly nudge only recomputes when stale.
    import threading

    def _drift_warmer() -> None:
        while True:
            try:
                from app.risk import monitor
                monitor.drift_report()
            except Exception as warm_exc:  # noqa: BLE001
                log.warning("drift_warm_failed", error=str(warm_exc))
            time.sleep(3600)

    threading.Thread(target=_drift_warmer, name="drift-warmer", daemon=True).start()

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


def _drift_summary_cached() -> dict | None:
    """Last cached model-drift report (never recomputes here so /health stays fast).

    Schedules a background refresh when the cache is stale/empty; the refresh runs off the
    request path so /health never pays the DB sampling cost. Returns {drift_level, psi_score}
    or None until the first report lands.
    """
    try:
        from app.risk import monitor
    except Exception:  # noqa: BLE001
        return None
    report = monitor.cached_report()
    # kick a one-shot background refresh if the cache is empty or due (cheap, deduped by the
    # monitor's own time guard inside drift_report()).
    try:
        import threading
        threading.Thread(target=monitor.drift_report, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    if report is None:
        return None
    return {"drift_level": report.get("drift_level"), "psi_score": report.get("psi_score")}


# The synthetic-line check aggregates 1.9M OrderItem rows (~2.7s DB CPU) and /health
# is unauthenticated — cache it in-process so health probes can't saturate MSSQL.
_SYNTHETIC_CHECK_TTL_S = 6 * 3600.0
_synthetic_check_lock = threading.Lock()
_synthetic_check_state: dict = {"at": 0.0, "ok": None}


def _synthetic_drift_ok_cached() -> bool | None:
    now = time.monotonic()
    with _synthetic_check_lock:
        if now - _synthetic_check_state["at"] < _SYNTHETIC_CHECK_TTL_S:
            return _synthetic_check_state["ok"]
    try:
        from app.data import solvency_repository as repo
        ok = bool(repo.synthetic_line_drift_check()["ok"])
    except Exception:  # noqa: BLE001
        ok = None
    with _synthetic_check_lock:
        _synthetic_check_state["at"] = time.monotonic()
        _synthetic_check_state["ok"] = ok
    return ok


def _model_readiness() -> dict:
    from app.risk.score_current import current_model_readiness
    from app.risk.score_forward import forward_model_readiness

    return {
        "current_state": current_model_readiness(),
        "forward_6m": forward_model_readiness(),
    }


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
    synthetic_drift_ok = _synthetic_drift_ok_cached() if db_ok else None
    source = {
        "business_ready": False,
        "reasons": ["database_unavailable"],
    }
    if db_ok:
        try:
            from app.data import solvency_repository as repo

            source = repo.source_readiness(settings.max_source_lag_days)
        except Exception as exc:  # noqa: BLE001
            log.warning("source_readiness_failed", error=str(exc))
            source = {
                "business_ready": False,
                "reasons": ["source_query_failed"],
            }
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
    business_ready = source.get("business_ready") is True
    models = _model_readiness()
    serving_ready = (
        db_ok
        and redis_ok
        and business_ready
        and synthetic_drift_ok is True
        and models["current_state"]["ready"] is True
    )
    healthy = serving_ready and models["forward_6m"]["ready"] is True
    drift = _drift_summary_cached()
    return {
        "status": "healthy" if healthy else "degraded",
        "serving_ready": serving_ready,
        "business_ready": business_ready,
        "db_connected": db_ok,
        "redis_connected": redis_ok,
        "synthetic_drift_ok": synthetic_drift_ok,
        "source": source,
        "model_readiness": models,
        "source_history_start": source_history_start,
        "source_history_contract_ready": source_history_contract_ready,
        "model_drift": drift,
        "version": "0.1.0",
        "model_version": settings.model_version,
    }


@app.get("/ready")
def ready() -> JSONResponse:
    snapshot = _health_snapshot()
    # A statistically unsupported forward model degrades /health but must not remove the
    # independently validated current-state score from service.
    is_ready = snapshot["serving_ready"] is True
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={**snapshot, "status": "ready" if is_ready else "not_ready"},
    )


@app.get("/monitor")
def monitor_endpoint(force: bool = False) -> dict:
    """Full model-drift report (PSI of live score + feature distributions vs training baseline).

    Cached in-process (refreshed every few hours); pass ?force=true to recompute now. A WARNING
    is logged by the monitor whenever drift_level is not ok.
    """
    from app.risk import monitor
    try:
        return monitor.drift_report(force=force)
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("monitor_failed", error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"internal error (ref {err_id})") from exc


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.post("/score", response_model=SolvencyScore)
def score(req: ScoreRequest) -> SolvencyScore:
    try:
        return _service().score_client(
            client_id=req.client_id, client_net_uid=req.client_net_uid,
            as_of_date=req.as_of_date.isoformat() if req.as_of_date else None,
            window_months=req.window_months, use_cache=req.use_cache,
        )
    except ClientIdentityMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("score_failed", client_id=req.client_id, error_id=err_id, error=str(exc))
        raise HTTPException(
            status_code=500, detail=f"internal error (ref {err_id})"
        ) from exc


@app.post("/score/batch")
def score_batch(req: BatchScoreRequest) -> dict:
    """Per-client errors are isolated so one bad id doesn't fail the batch.

    Uses the set-based score_batch (a handful of queries for the whole id-list) instead of an
    N-pass per-client loop; results are bit-identical to score_client per id.
    """
    try:
        results, errors = _service().score_batch(
            client_ids=req.client_ids,
            as_of_date=req.as_of_date.isoformat() if req.as_of_date else None,
            window_months=req.window_months,
            use_cache=req.use_cache,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("score_batch_failed", clients=len(req.client_ids), error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"internal error (ref {err_id})") from exc
    return {"results": results, "errors": errors, "count": len(results), "failed": len(errors)}


@app.get("/charts/{client_id}", response_model=SolvencyCharts)
def charts(
    client_id: int,
    as_of_date: date | None = None,
    months: int = Query(default=12, ge=1, le=60),
) -> SolvencyCharts:
    try:
        return _service().build_charts(
            client_id=client_id,
            as_of_date=as_of_date.isoformat() if as_of_date else None,
            window_months=months,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("charts_failed", client_id=client_id, error_id=err_id, error=str(exc))
        raise HTTPException(
            status_code=500, detail=f"internal error (ref {err_id})"
        ) from exc


@app.delete("/cache/{client_id}")
def clear_cache(client_id: int) -> dict:
    try:
        return {"deleted": cache.invalidate_client(client_id)}
    except Exception as exc:  # noqa: BLE001
        err_id = uuid.uuid4().hex
        log.error("cache_clear_failed", client_id=client_id, error_id=err_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"internal error (ref {err_id})") from exc
