"""FastAPI app — GBA AI Sales Cockpit (NBA task engine)."""
from __future__ import annotations

import hmac
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import history
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.core.money import cents, decimal_value
from app.data import mongo, signals_repository
from app.data.db import get_engine
from app.domain.models import Outcome, TaskStatus, Urgency
from app.services import lifecycle

log = get_logger("api")
settings = get_settings()
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}
PositiveId = Annotated[int, Field(gt=0)]
MoneyAmount = Annotated[
    Decimal,
    Field(ge=0, max_digits=18, decimal_places=2),
]


def _kyiv_today() -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _current_task_as_of(value: date | None) -> str:
    """Task state has no historical event snapshot, so mixed historical/current dashboards
    are forbidden. Accept only today's Kyiv business date and fail closed otherwise."""
    today = _kyiv_today()
    requested = value or today
    _require_source_as_of(requested)
    if value is not None and requested != today:
        raise HTTPException(
            status_code=422,
            detail="historical_as_of_not_supported_for_current_task_state",
        )
    return today.isoformat()


def _require_source_as_of(value: date) -> date:
    try:
        return history.require_as_of(value)
    except history.SourceHistoryBoundaryError as exc:
        raise HTTPException(
            status_code=422,
            detail="as_of_before_source_history_start",
        ) from exc


def _debt_dash_warmer() -> None:
    """Keep the all-managers debt dashboard cache warm so no request ever pays the
    ~44s cold aggregation (which exceeds the gba-server 30s proxy timeout)."""
    while True:
        try:
            _debt_dashboards_cached(_kyiv_today().isoformat(), allow_stale=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("debt_dash_warm_failed", error=str(exc))
        time.sleep(_DEBT_DASH_TTL_S * 0.9)


def _team_snap_warmer() -> None:
    """Keep the head team/targets snapshot warm so the console's 60s poll and the first paint
    read precomputed data instead of paying the per-manager target SQL fan-out per call."""
    while True:
        try:
            _team_snapshot_cached(allow_stale=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("team_snap_warm_failed", error=str(exc))
        time.sleep(_TEAM_SNAP_TTL_S * 0.9)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        mongo.ensure_indexes()
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo_index_setup_failed", error=str(exc))
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-nba running OPEN — set INTERNAL_API_KEY")
    threading.Thread(target=_debt_dash_warmer, daemon=True, name="debt-dash-warmer").start()
    threading.Thread(target=_team_snap_warmer, daemon=True, name="team-snap-warmer").start()
    log.info("service_starting", service="gba-nba")
    yield
    mongo.close()
    log.info("service_stopped")


app = FastAPI(title="GBA AI Sales Cockpit (NBA)", version="0.1.0", lifespan=lifespan)
# Server-to-server only (the gba-server proxy is the sole client); a browser never calls this directly.
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def require_internal_key(request: Request, call_next):
    if settings.internal_api_key and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("X-Internal-Api-Key", "")
        if not hmac.compare_digest(
            provided.encode("utf-8"), settings.internal_api_key.encode("utf-8")
        ):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def timing(request: Request, call_next):
    t = time.time()
    resp = await call_next(request)
    resp.headers["X-Process-Time-Ms"] = str(round((time.time() - t) * 1000, 2))
    return resp


class StatusRequest(BaseModel):
    to: TaskStatus
    by: PositiveId = Field(..., description="manager User.ID performing the action")
    reason: str | None = Field(default=None, max_length=1000)
    sold: bool | None = None
    amount: MoneyAmount | None = None
    snooze_until: datetime | None = None


class NoteRequest(BaseModel):
    author_id: PositiveId
    text: str = Field(min_length=1, max_length=4000)


class CockpitStatusRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=500)
    to: TaskStatus
    reason: str | None = Field(default=None, max_length=1000)
    sold: bool | None = None
    amount: MoneyAmount | None = None
    snooze_until: datetime | None = None


class CockpitNoteRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=4000)


def _resolve_manager(manager_net_uid: str) -> int:
    try:
        uuid.UUID(str(manager_net_uid))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="unknown_manager") from None
    manager_id = signals_repository.manager_id_for_netuid(manager_net_uid)
    if manager_id is None:
        raise HTTPException(status_code=404, detail="unknown_manager")
    return manager_id


def _canonical_net_uid(manager_net_uid: str) -> str:
    return str(uuid.UUID(str(manager_net_uid)))


def _as_of(as_of_date: date | None) -> str | None:
    return _require_source_as_of(as_of_date).isoformat() if as_of_date else None


def _resolved_as_of(as_of_date: date | None) -> str:
    """Resolve an optional API business date in the configured Kyiv calendar."""
    return _as_of(as_of_date) or _require_source_as_of(_kyiv_today()).isoformat()


def _guard_generation(stats: dict) -> None:
    total = stats.get("generators_total", 0)
    if total and stats.get("generators_failed", 0) >= total:
        raise HTTPException(status_code=502, detail="generation_failed: all task generators errored")


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    mongo_ok = mongo.ping()
    source = {
        "source_ready": False,
        "source_reasons": ["database_unavailable"],
        "latest_sale_at": None,
        "manager_count": 0,
        "synthetic_product_count": 0,
        "source_history_start": history.source_history_start().isoformat(),
        "effective_start": history.source_history_start().isoformat(),
        "history_complete": True,
    }
    generation = {
        "generation_ready": False,
        "generation_reasons": ["mongo_unavailable"],
        "last_generation_at": None,
        "last_generation_managers": 0,
        "last_generation_ok": 0,
        "last_generation_failed": 0,
        "task_count": 0,
        "active_task_count": 0,
        "latest_task_refresh_at": None,
    }
    if db_ok:
        try:
            source.update(signals_repository.source_readiness(settings.max_source_lag_days))
        except Exception as exc:  # noqa: BLE001
            log.error("nba_source_readiness_failed", error=str(exc))
            source["source_reasons"] = ["source_readiness_failed"]
    if mongo_ok:
        try:
            generation = mongo.generation_readiness(settings.max_generation_lag_hours)
        except Exception as exc:  # noqa: BLE001
            log.error("nba_generation_readiness_failed", error=str(exc))
            generation["generation_reasons"] = ["generation_readiness_failed"]
    source_history_start = settings.source_history_start_date.isoformat()
    source_history_contract_ready = (
        source_history_start == _EXPECTED_SOURCE_HISTORY_START
    )
    source = {**source, "source_history_start": source_history_start}
    if not source_history_contract_ready:
        source["source_ready"] = False
        source["source_reasons"] = [
            *list(source.get("source_reasons") or []),
            "source_history_start_mismatch",
        ]
    try:
        from app.ml.score_task import model_compatibility

        model = model_compatibility()
    except Exception as exc:  # noqa: BLE001
        log.error("nba_model_readiness_failed", error=str(exc))
        model = {
            "model_compatible": False,
            "model_reasons": ["model_readiness_failed"],
            "model_source_history_start": None,
            "model_training_window_days": None,
            "model_training_vintages": [],
        }
    business_ready = (
        bool(source["source_ready"])
        and bool(generation["generation_ready"])
        and bool(model["model_compatible"])
    )
    healthy = db_ok and mongo_ok and business_ready
    return {
        "status": "healthy" if healthy else "degraded",
        "db_connected": db_ok,
        "mongo_connected": mongo_ok,
        "business_ready": business_ready,
        **source,
        **generation,
        **model,
        "source_history_contract_ready": source_history_contract_ready,
        "version": "0.1.0",
        "model_version": settings.model_version,
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


@app.get("/tasks/manager/{manager_id}")
def get_inbox(
    manager_id: int = Path(gt=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    started = time.time()
    try:
        items = lifecycle.inbox(manager_id, limit=limit)
        for it in items:
            it["_id"] = str(it["_id"])
        METRICS.record_request((time.time() - started) * 1000)
        return {"manager_id": manager_id, "count": len(items), "tasks": items}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("inbox_failed", manager_id=manager_id, error=str(exc))
        raise HTTPException(status_code=500, detail="inbox_failed") from exc


@app.post("/tasks/{task_key}/status")
def set_status(
    req: StatusRequest,
    task_key: str = Path(min_length=1, max_length=500),
) -> dict:
    outcome = None
    if req.to == TaskStatus.DONE and (req.sold is not None or req.amount is not None):
        outcome = Outcome(sold=bool(req.sold), amount=req.amount)
    try:
        doc = lifecycle.change_status(task_key, req.to, by=req.by, reason=req.reason,
                                      outcome=outcome, snooze_until=req.snooze_until)
        doc["_id"] = str(doc["_id"])
        return doc
    except lifecycle.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/tasks/{task_key}/notes")
def add_note(
    req: NoteRequest,
    task_key: str = Path(min_length=1, max_length=500),
) -> dict:
    try:
        doc = lifecycle.add_note(task_key, req.author_id, req.text)
        doc["_id"] = str(doc["_id"])
        return doc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/generate/manager/{manager_id}")
def generate(
    manager_id: int = Path(gt=0),
    as_of_date: date | None = None,
) -> dict:
    from app.services import orchestrator

    # Resolve and validate outside the broad generation error boundary: a caller asking for a
    # date before the declared source history must receive the canonical 422, not a wrapped 500.
    resolved_as_of = _resolved_as_of(as_of_date)
    started = time.time()
    try:
        stats = orchestrator.generate_for_manager(manager_id, resolved_as_of)
        METRICS.record_request((time.time() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        error_id = uuid.uuid4().hex
        log.error("generation_failed", manager_id=manager_id, error_id=error_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"generation_failed ({error_id})") from exc
    _guard_generation(stats)
    return stats


@app.get("/cockpit/inbox")
def cockpit_inbox(
    manager_net_uid: str,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, max_length=300),
) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    items = lifecycle.inbox(manager_id, limit=limit, statuses=statuses)
    for it in items:
        it["_id"] = str(it["_id"])
    return {"manager_id": manager_id, "manager_net_uid": _canonical_net_uid(manager_net_uid),
            "count": len(items), "tasks": items}


@app.get("/cockpit/count")
def cockpit_count(manager_net_uid: str) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    by_urgency = lifecycle.count_active_by_urgency(manager_id)
    return {"manager_id": manager_id, "manager_net_uid": _canonical_net_uid(manager_net_uid),
            "active_count": by_urgency["total"],
            "by_urgency": {k: by_urgency[k] for k in ("critical", "high", "normal", "low")}}


@app.post("/cockpit/status")
def cockpit_status(manager_net_uid: str, req: CockpitStatusRequest) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    canonical_net_uid = _canonical_net_uid(manager_net_uid)
    task = lifecycle.get_task(req.task_key)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["manager_id"] != manager_id:
        raise HTTPException(status_code=403, detail="forbidden")
    outcome = None
    if req.to == TaskStatus.DONE and (req.sold is not None or req.amount is not None):
        outcome = Outcome(sold=bool(req.sold), amount=req.amount)
    try:
        doc = lifecycle.change_status(req.task_key, req.to, by=manager_id, reason=req.reason,
                                      outcome=outcome, snooze_until=req.snooze_until)
        doc["_id"] = str(doc["_id"])
        doc["manager_net_uid"] = canonical_net_uid
        return doc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cockpit/notes")
def cockpit_notes(manager_net_uid: str, req: CockpitNoteRequest) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    canonical_net_uid = _canonical_net_uid(manager_net_uid)
    task = lifecycle.get_task(req.task_key)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["manager_id"] != manager_id:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        doc = lifecycle.add_note(req.task_key, manager_id, req.text)
    except lifecycle.TaskNotFoundError as exc:
        # Race with sweep_expired: the task passed the ownership check above but was
        # purged before the note landed — a 404, not a 500.
        raise HTTPException(status_code=404, detail="task not found") from exc
    doc["_id"] = str(doc["_id"])
    doc["manager_net_uid"] = canonical_net_uid
    return doc


@app.post("/cockpit/generate")
def cockpit_generate(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    from app.services import orchestrator
    manager_id = _resolve_manager(manager_net_uid)
    requested_as_of = _as_of(as_of_date)
    resolved_as_of = _resolved_as_of(as_of_date)
    coverage = history.rolling_days(resolved_as_of, 365)
    stats = orchestrator.generate_for_manager(manager_id, resolved_as_of)
    if stats.get("manager_id") != manager_id or stats.get("as_of") != resolved_as_of:
        log.error(
            "generation_identity_mismatch",
            requested_manager_id=manager_id,
            returned_manager_id=stats.get("manager_id"),
            requested_as_of=resolved_as_of,
            returned_as_of=stats.get("as_of"),
        )
        raise HTTPException(status_code=502, detail="generation_identity_mismatch")
    _guard_generation(stats)
    return {
        **stats,
        "manager_net_uid": _canonical_net_uid(manager_net_uid),
        "requested_as_of": requested_as_of,
        **coverage.metadata(),
    }


@app.get("/cockpit/target")
def cockpit_target(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    """The manager's monthly minimum target + daily pace (shipped & paid) for their dashboard."""
    from app.services import targets
    manager_id = _resolve_manager(manager_net_uid)
    result = targets.compute_target(manager_id, as_of=_as_of(as_of_date))
    result["manager_name"] = signals_repository.manager_names([manager_id]).get(manager_id)
    result["manager_net_uid"] = _canonical_net_uid(manager_net_uid)
    return result


# The all-managers debt aggregation is the dominant dashboard cost (~44s live — past the
# gba-server 30s proxy timeout, so uncached the head charts are effectively broken).
# Cache it in-process per as_of; the single-manager dashboard slices the same result
# (per-manager DTOs are exactly equal — same rows, same fold). Compute is serialized so
# concurrent misses wait for one build instead of stacking 44s queries.
_DEBT_DASH_TTL_S = 600.0
_debt_dash_lock = threading.Lock()
_debt_dash_state: dict = {
    "at": 0.0,
    "as_of": None,
    "source_history_start": None,
    "values": {},
}

_EMPTY_DEBT_DASH = {
    "value_at_risk_eur": 0.0,
    "debt_aging": [
        {"bucket": "0-30", "amount_eur": 0.0, "count": 0},
        {"bucket": "31-60", "amount_eur": 0.0, "count": 0},
        {"bucket": "61-90", "amount_eur": 0.0, "count": 0},
        {"bucket": "90+", "amount_eur": 0.0, "count": 0},
    ],
}


_debt_dash_compute_lock = threading.Lock()


def _debt_dashboards_cached(as_of: str, *, allow_stale: bool = True) -> dict[int, dict]:
    """Stale-while-revalidate: requests use cached data for the same as_of (the
    background warmer refreshes every ~9 min) and NEVER wait out the ~44-70s
    recompute. Inline compute happens only when that as_of has no cached value."""
    source_start = history.source_history_start().isoformat()
    with _debt_dash_lock:
        same_as_of = (
            _debt_dash_state["as_of"] == as_of
            and _debt_dash_state.get("source_history_start") == source_start
        )
        fresh = same_as_of and time.monotonic() - _debt_dash_state["at"] < _DEBT_DASH_TTL_S
        values = _debt_dash_state["values"]
        if fresh or (allow_stale and same_as_of and values):
            return values
    with _debt_dash_compute_lock:
        with _debt_dash_lock:
            same_as_of = (
                _debt_dash_state["as_of"] == as_of
                and _debt_dash_state.get("source_history_start") == source_start
            )
            fresh = same_as_of and time.monotonic() - _debt_dash_state["at"] < _DEBT_DASH_TTL_S
            if _debt_dash_state["values"] and (fresh or (allow_stale and same_as_of)):
                return _debt_dash_state["values"]
        computed = signals_repository.debt_dashboards_for_all_managers(as_of)
        with _debt_dash_lock:
            _debt_dash_state.update(
                {
                    "at": time.monotonic(),
                    "as_of": as_of,
                    "source_history_start": source_start,
                    "values": computed,
                }
            )
        return computed


@app.get("/cockpit/dashboard")
def cockpit_dashboard(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    """Chart-ready manager dashboard DTO (snake_case), computed from the SAME signals the cockpit
    uses — the MongoDB task store (task_type/urgency/status mix) and the EUR-correct debt
    aggregation (value_at_risk + aging). No scores are recomputed. Internal-key gated."""
    manager_id = _resolve_manager(manager_net_uid)
    as_of = _current_task_as_of(as_of_date)
    coverage = history.rolling_days(as_of, settings.debt_max_age_days)
    try:
        counts = lifecycle.dashboard_counts(manager_id, as_of)
        debt = _debt_dashboards_cached(as_of).get(manager_id) or _EMPTY_DEBT_DASH
    except Exception as exc:  # noqa: BLE001
        error_id = uuid.uuid4().hex
        log.error("dashboard_failed", manager_id=manager_id, error_id=error_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"dashboard_failed ({error_id})") from exc
    return {
        "manager_id": manager_id,
        "manager_net_uid": _canonical_net_uid(manager_net_uid),
        "as_of": as_of,
        **coverage.metadata(),
        "task_type_mix": counts["task_type_mix"],
        "urgency_mix": counts["urgency_mix"],
        "value_at_risk_eur": debt["value_at_risk_eur"],
        "debt_aging": debt["debt_aging"],
        "completed_vs_open": counts["completed_vs_open"],
    }


@app.get("/cockpit/head/dashboard")
def cockpit_head_dashboard(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    """Chart-ready head/team dashboard DTO (snake_case): per-manager open tasks / critical / debt
    value-at-risk (EUR), plus the escalation count and department value-at-risk. Reuses the same
    head/team role gate and per-manager aggregations. Non-head caller -> benign {is_head: false}
    (200, NOT 403 — the console treats any 403 as a session expiry). Unknown caller -> 404."""
    _resolve_manager(manager_net_uid)
    requested_net_uid = _canonical_net_uid(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "requested_manager_net_uid": requested_net_uid,
                "as_of": None, "teams": [],
                "escalated_count": 0, "total_value_at_risk_eur": 0.0}
    as_of = _current_task_as_of(as_of_date)
    coverage = history.rolling_days(as_of, settings.debt_max_age_days)
    teams = []
    total_var = Decimal("0")
    debt_by_manager = _debt_dashboards_cached(as_of)
    for mid in signals_repository.all_managers():
        var = debt_by_manager.get(mid, {}).get("value_at_risk_eur", 0.0)
        total_var += decimal_value(var)
        teams.append({"manager_id": mid,
                      "open_tasks": lifecycle.active_count(mid),
                      "critical": lifecycle.critical_active_count(mid),
                      "value_at_risk_eur": var})
    return {"is_head": True, "requested_manager_net_uid": requested_net_uid,
            "as_of": as_of, **coverage.metadata(), "teams": teams,
            "escalated_count": lifecycle.escalated_count(),
            "total_value_at_risk_eur": cents(total_var)}


def _summarize_metric(metric: dict) -> dict:
    return {
        key: metric[key]
        for key in (
            "target",
            "mtd",
            "expected_to_date",
            "attainment_pct",
            "pace_status",
        )
    }


def _empty_team_totals() -> dict:
    """Complete typed zero shape used for a legitimate non-head response."""
    return {
        "shipped_target": 0.0,
        "shipped_mtd": 0.0,
        "paid_target": 0.0,
        "paid_mtd": 0.0,
        "generated_month": 0,
        "done_month": 0,
        "sold_month": 0,
        "dismissed_month": 0,
        "revenue_month": 0.0,
        "close_rate": 0.0,
        "conversion_rate": 0.0,
    }


def _guard_team_stats(manager_id: int, stats: dict) -> None:
    """Reject internally inconsistent manager task aggregates before they reach team totals."""
    try:
        done_month = stats["done_month"]
        sold_month = stats["sold_month"]
        revenue_month = decimal_value(stats["revenue_month"])
        counters_are_ints = all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (done_month, sold_month)
        )
        is_valid = (
            counters_are_ints
            and 0 <= sold_month <= done_month
            and revenue_month >= 0
            and not (sold_month == 0 and revenue_month != 0)
        )
    except (KeyError, TypeError, ValueError):
        is_valid = False
    if is_valid:
        return
    log.error("team_stats_invariant_failed", manager_id=manager_id, stats=stats)
    raise HTTPException(status_code=502, detail="team_stats_invariant_failed")


# /head/team and /targets/overview both fan out targets.compute_target over every manager (2 SQL
# aggregations each) plus team_stats; the console polls /head/team every 60s. Precompute ONE shared
# snapshot (same warmer pattern as the debt dashboard above) so the poll and the first paint read
# cached data. Explicit dates are current-only, so they share the same date-keyed cache safely.
_TEAM_SNAP_TTL_S = 90.0
_team_snap_lock = threading.Lock()
_team_snap_compute_lock = threading.Lock()
_team_snap_state: dict = {
    "at": 0.0,
    "as_of": None,
    "source_history_start": None,
    "value": None,
}


def _build_team_snapshot(as_of_str: str) -> dict:
    from app.services import targets
    team = []
    overview_rows = []
    totals = {"shipped_target": Decimal("0"), "shipped_mtd": Decimal("0"),
              "paid_target": Decimal("0"), "paid_mtd": Decimal("0"),
              "generated_month": 0, "done_month": 0, "sold_month": 0, "dismissed_month": 0,
              "revenue_month": Decimal("0")}
    as_of = as_of_str
    coverage = history.rolling_days(as_of, 365)
    mids = signals_repository.all_managers()
    names = signals_repository.manager_names(mids)
    for mid in mids:
        # A team response is an accounting/management aggregate: silently skipping one failed
        # manager would make every total look valid while being incomplete. Fail the endpoint
        # closed and let the proxy surface the upstream error instead.
        target = targets.compute_target(mid, as_of=as_of_str)
        if target.get("as_of") != as_of_str or target.get("month") != as_of_str[:7]:
            log.error(
                "target_period_mismatch",
                manager_id=mid,
                requested_as_of=as_of_str,
                returned_as_of=target.get("as_of"),
                returned_month=target.get("month"),
            )
            raise HTTPException(status_code=502, detail="target_period_mismatch")
        overview_rows.append(target)
        tasks = lifecycle.team_stats(mid, as_of_str)
        _guard_team_stats(mid, tasks)
        team.append({"manager_id": mid, "manager_name": names.get(mid),
                     "target": {"shipped": _summarize_metric(target["shipped"]),
                                "paid": _summarize_metric(target["paid"])},
                     "tasks": tasks})
        totals["shipped_target"] += decimal_value(target["shipped"]["target"])
        totals["shipped_mtd"] += decimal_value(target["shipped"]["mtd"])
        totals["paid_target"] += decimal_value(target["paid"]["target"])
        totals["paid_mtd"] += decimal_value(target["paid"]["mtd"])
        for key in ("generated_month", "done_month", "sold_month", "dismissed_month"):
            totals[key] += tasks[key]
        totals["revenue_month"] += decimal_value(tasks["revenue_month"])
    for k in ("shipped_target", "shipped_mtd", "paid_target", "paid_mtd", "revenue_month"):
        totals[k] = cents(totals[k])
    # department-level KPI (effectiveness), derived from the totals
    totals["close_rate"] = lifecycle.close_rate(totals["done_month"], totals["dismissed_month"])
    totals["conversion_rate"] = lifecycle.conversion_rate(totals["sold_month"], totals["done_month"])
    expected_count = len(mids)
    return {
        "overview": {
            "count": len(overview_rows),
            "expected_manager_count": expected_count,
            "returned_manager_count": len(overview_rows),
            **coverage.metadata(),
            "managers": overview_rows,
        },
        "team": {
            "is_head": True,
            "as_of": as_of,
            **coverage.metadata(),
            "expected_manager_count": expected_count,
            "returned_manager_count": len(team),
            "team": team,
            "totals": totals,
        },
    }


def _team_snapshot_cached(as_of: str | None = None, *, allow_stale: bool = True) -> dict:
    """Stale-while-revalidate snapshot for the DEFAULT (as_of=today) head views; the background
    warmer refreshes every ~80s so requests virtually never pay the compute."""
    key = as_of or _kyiv_today().isoformat()
    source_start = history.source_history_start().isoformat()
    with _team_snap_lock:
        same = (
            _team_snap_state["as_of"] == key
            and _team_snap_state.get("source_history_start") == source_start
        )
        fresh = same and time.monotonic() - _team_snap_state["at"] < _TEAM_SNAP_TTL_S
        value = _team_snap_state["value"]
        if value is not None and (fresh or (allow_stale and same)):
            return value
    with _team_snap_compute_lock:
        with _team_snap_lock:
            same = (
                _team_snap_state["as_of"] == key
                and _team_snap_state.get("source_history_start") == source_start
            )
            fresh = same and time.monotonic() - _team_snap_state["at"] < _TEAM_SNAP_TTL_S
            if _team_snap_state["value"] is not None and (fresh or (allow_stale and same)):
                return _team_snap_state["value"]
        computed = _build_team_snapshot(key)
        with _team_snap_lock:
            _team_snap_state.update(
                {
                    "at": time.monotonic(),
                    "as_of": key,
                    "source_history_start": source_start,
                    "value": computed,
                }
            )
        return computed


@app.get("/targets/overview")
def targets_overview(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    """Head-of-sales view: target + pace for every active manager. Non-head caller -> benign
    {is_head: false} (200, not 403 — same contract as every other /head route)."""
    _resolve_manager(manager_net_uid)
    requested_net_uid = _canonical_net_uid(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "requested_manager_net_uid": requested_net_uid,
                "count": 0, "expected_manager_count": 0,
                "returned_manager_count": 0, "managers": []}
    as_of = _current_task_as_of(as_of_date)
    result = dict(_team_snapshot_cached(as_of)["overview"])
    result["requested_manager_net_uid"] = requested_net_uid
    return result


@app.get("/head/team")
def head_team(manager_net_uid: str, as_of_date: date | None = None) -> dict:
    """Head-of-sales dashboard: target/attainment/pace + task throughput for every manager.
    gba-nba is the authority on role. Unknown caller -> 404. A non-head caller gets a benign
    {is_head: false} with NO team data (200) — not a 403, because the console treats any 403 as a
    session expiry; the page renders 'лише для керівника' when is_head is false."""
    _resolve_manager(manager_net_uid)
    requested_net_uid = _canonical_net_uid(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "requested_manager_net_uid": requested_net_uid,
                "as_of": None, "expected_manager_count": 0,
                "returned_manager_count": 0,
                "team": [], "totals": _empty_team_totals()}
    as_of_str = _current_task_as_of(as_of_date)
    result = dict(_team_snapshot_cached(as_of_str)["team"])
    result["requested_manager_net_uid"] = requested_net_uid
    return result


class TeamBoardTask(BaseModel):
    task_key: str
    manager_id: int
    manager_name: str | None = None
    client_id: int | None = None
    client_name: str | None = None
    task_type: str | None = None
    title: str | None = None
    status: str
    urgency: str | None = None
    priority: float = 0.0
    p_outcome: float = 0.0
    expected_value: float = 0.0
    ev_score: float = 0.0
    in_progress_since: datetime | None = None
    generated_at: datetime | None = None
    updated_at: datetime | None = None
    sla_breached: bool = False


class TeamBoardManager(BaseModel):
    manager_id: int
    name: str | None = None


class TeamBoardResponse(BaseModel):
    is_head: bool = True
    requested_manager_net_uid: str
    requested_statuses: list[str]
    requested_manager_id: int | None = None
    requested_urgency: str | None = None
    skip: int
    limit: int
    returned_count: int
    total: int
    tasks: list[TeamBoardTask]
    by_status: dict[str, int]
    managers: list[TeamBoardManager]


def _empty_team_board_statuses() -> dict[str, int]:
    return {
        TaskStatus.OPEN.value: 0,
        TaskStatus.IN_PROGRESS.value: 0,
        TaskStatus.DONE.value: 0,
        TaskStatus.SNOOZED.value: 0,
        TaskStatus.DISMISSED.value: 0,
    }


@app.get("/head/tasks", response_model=TeamBoardResponse)
def head_tasks(
    manager_net_uid: str,
    statuses: str = "open,in_progress",
    manager_id: Annotated[int | None, Query(gt=0)] = None,
    urgency: str | None = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> TeamBoardResponse:
    """Head-of-sales team-wide live board: ALL managers' tasks (optionally filtered to one manager via
    manager_id), status $in the csv `statuses` (default open,in_progress), optional urgency. Sorted to
    surface actively-worked + most-urgent first. Unknown caller -> 404. A non-head caller gets a
    benign empty board with is_head=false (200, NOT 403 — the console treats 403 as session expiry
    and the gba-server proxy mislabels it as ai_auth_misconfigured; the board mounts before the
    role resolves, so non-heads DO hit this route). Returns the page, the total over the filter,
    a by_status rollup, and the full manager list (for the board's filter dropdown)."""
    _resolve_manager(manager_net_uid)
    requested_net_uid = _canonical_net_uid(manager_net_uid)
    status_list = list(dict.fromkeys(s.strip() for s in statuses.split(",") if s.strip()))
    if not status_list:
        status_list = [TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]
    allowed_statuses = {status.value for status in TaskStatus}
    if any(status not in allowed_statuses for status in status_list):
        raise HTTPException(status_code=422, detail="invalid_task_status_filter")
    if urgency is not None and urgency not in {value.value for value in Urgency}:
        raise HTTPException(status_code=422, detail="invalid_urgency_filter")
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return TeamBoardResponse(
            is_head=False,
            requested_manager_net_uid=requested_net_uid,
            requested_statuses=status_list,
            requested_manager_id=manager_id,
            requested_urgency=urgency,
            skip=skip,
            limit=limit,
            returned_count=0,
            total=0,
            tasks=[],
            by_status=_empty_team_board_statuses(),
            managers=[],
        )
    mids = signals_repository.all_managers()
    if manager_id is not None and manager_id not in mids:
        raise HTTPException(status_code=422, detail="manager_not_in_active_team_scope")
    mgr_filter = [manager_id] if manager_id is not None else mids
    if mgr_filter:
        tasks, total, by_status = lifecycle.team_tasks(
            status_list,
            manager_ids=mgr_filter,
            urgency=urgency,
            skip=skip,
            limit=limit,
        )
    else:
        tasks, total, by_status = [], 0, _empty_team_board_statuses()
    names = signals_repository.manager_names(mids)
    managers = [{"manager_id": mid, "name": names.get(mid)} for mid in mids]
    return TeamBoardResponse(
        requested_manager_net_uid=requested_net_uid,
        requested_statuses=status_list,
        requested_manager_id=manager_id,
        requested_urgency=urgency,
        skip=skip,
        limit=limit,
        returned_count=len(tasks),
        total=total,
        tasks=tasks,
        by_status=by_status,
        managers=managers,
    )


@app.get("/head/escalated")
def head_escalated(
    manager_net_uid: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    """Head-of-sales escalation queue: SLA-breached high/critical tasks escalated by the sweep.
    Same role gate as /head/team — unknown caller -> 404; a non-head caller gets a benign
    {is_head: false} with NO tasks (200, not 403, because the console treats any 403 as a session
    expiry)."""
    _resolve_manager(manager_net_uid)
    requested_net_uid = _canonical_net_uid(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {
            "is_head": False,
            "requested_manager_net_uid": requested_net_uid,
            "requested_limit": limit,
            "count": 0,
            "tasks": [],
        }
    items = lifecycle.escalated_tasks(limit=limit)
    for it in items:
        it["_id"] = str(it["_id"])
    return {
        "is_head": True,
        "requested_manager_net_uid": requested_net_uid,
        "requested_limit": limit,
        "count": len(items),
        "tasks": items,
    }
