"""FastAPI app — GBA AI Sales Cockpit (NBA task engine)."""
from __future__ import annotations

import hmac
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import mongo, signals_repository
from app.data.db import get_engine
from app.domain.models import Outcome, TaskStatus
from app.services import lifecycle

log = get_logger("api")
settings = get_settings()

# Routes reachable without the internal key (operational endpoints).
_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        mongo.ensure_indexes()
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo_index_setup_failed", error=str(exc))
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-nba running OPEN — set INTERNAL_API_KEY")
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
        if not hmac.compare_digest(provided, settings.internal_api_key):
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
    by: int = Field(..., description="manager User.ID performing the action")
    reason: str | None = None
    sold: bool | None = None
    amount: float | None = None
    snooze_until: datetime | None = None


class NoteRequest(BaseModel):
    author_id: int
    text: str


class CockpitStatusRequest(BaseModel):
    task_key: str
    to: TaskStatus
    reason: str | None = None
    sold: bool | None = None
    amount: float | None = None
    snooze_until: datetime | None = None


class CockpitNoteRequest(BaseModel):
    task_key: str
    text: str


def _resolve_manager(manager_net_uid: str) -> int:
    try:
        uuid.UUID(str(manager_net_uid))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="unknown_manager") from None
    manager_id = signals_repository.manager_id_for_netuid(manager_net_uid)
    if manager_id is None:
        raise HTTPException(status_code=404, detail="unknown_manager")
    return manager_id


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "healthy" if (db_ok and mongo.ping()) else "degraded",
            "db_connected": db_ok, "mongo_connected": mongo.ping(),
            "version": "0.1.0", "model_version": settings.model_version}


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.get("/tasks/manager/{manager_id}")
def get_inbox(manager_id: int, limit: int = 50) -> dict:
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
def set_status(task_key: str, req: StatusRequest) -> dict:
    outcome = None
    if req.to == TaskStatus.DONE and (req.sold is not None or req.amount is not None):
        outcome = Outcome(sold=bool(req.sold), amount=req.amount)
    try:
        doc = lifecycle.change_status(task_key, req.to, by=req.by, reason=req.reason,
                                      outcome=outcome, snooze_until=req.snooze_until)
        doc["_id"] = str(doc["_id"])
        return doc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/tasks/{task_key}/notes")
def add_note(task_key: str, req: NoteRequest) -> dict:
    try:
        doc = lifecycle.add_note(task_key, req.author_id, req.text)
        doc["_id"] = str(doc["_id"])
        return doc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/generate/manager/{manager_id}")
def generate(manager_id: int, as_of_date: str | None = None) -> dict:
    from app.services import orchestrator
    started = time.time()
    try:
        stats = orchestrator.generate_for_manager(manager_id, as_of_date)
        METRICS.record_request((time.time() - started) * 1000)
        return stats
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("generation_failed", manager_id=manager_id, error=str(exc))
        raise HTTPException(status_code=500, detail="generation_failed") from exc


@app.get("/cockpit/inbox")
def cockpit_inbox(manager_net_uid: str, limit: int = 50, status: str | None = None) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    items = lifecycle.inbox(manager_id, limit=limit, statuses=statuses)
    for it in items:
        it["_id"] = str(it["_id"])
    return {"manager_id": manager_id, "manager_net_uid": manager_net_uid,
            "count": len(items), "tasks": items}


@app.get("/cockpit/count")
def cockpit_count(manager_net_uid: str) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    by_urgency = lifecycle.count_active_by_urgency(manager_id)
    return {"manager_id": manager_id, "active_count": by_urgency["total"],
            "by_urgency": {k: by_urgency[k] for k in ("critical", "high", "normal", "low")}}


@app.post("/cockpit/status")
def cockpit_status(manager_net_uid: str, req: CockpitStatusRequest) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
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
        return doc
    except lifecycle.TransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/cockpit/notes")
def cockpit_notes(manager_net_uid: str, req: CockpitNoteRequest) -> dict:
    manager_id = _resolve_manager(manager_net_uid)
    task = lifecycle.get_task(req.task_key)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["manager_id"] != manager_id:
        raise HTTPException(status_code=403, detail="forbidden")
    doc = lifecycle.add_note(req.task_key, manager_id, req.text)
    doc["_id"] = str(doc["_id"])
    return doc


@app.post("/cockpit/generate")
def cockpit_generate(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    from app.services import orchestrator
    manager_id = _resolve_manager(manager_net_uid)
    return orchestrator.generate_for_manager(manager_id, as_of_date)


@app.get("/cockpit/target")
def cockpit_target(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    """The manager's monthly minimum target + daily pace (shipped & paid) for their dashboard."""
    from app.services import targets
    manager_id = _resolve_manager(manager_net_uid)
    result = targets.compute_target(manager_id, as_of=as_of_date)
    result["manager_name"] = signals_repository.manager_names([manager_id]).get(manager_id)
    return result


@app.get("/cockpit/dashboard")
def cockpit_dashboard(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    """Chart-ready manager dashboard DTO (snake_case), computed from the SAME signals the cockpit
    uses — the MongoDB task store (task_type/urgency/status mix) and the EUR-correct debt
    aggregation (value_at_risk + aging). No scores are recomputed. Internal-key gated."""
    manager_id = _resolve_manager(manager_net_uid)
    as_of = as_of_date or datetime.now(UTC).strftime("%Y-%m-%d")
    counts = lifecycle.dashboard_counts(manager_id)
    debt = signals_repository.debt_dashboard_for_manager(manager_id, as_of)
    return {
        "manager_id": manager_id,
        "as_of": as_of,
        "task_type_mix": counts["task_type_mix"],
        "urgency_mix": counts["urgency_mix"],
        "value_at_risk_eur": debt["value_at_risk_eur"],
        "debt_aging": debt["debt_aging"],
        "completed_vs_open": counts["completed_vs_open"],
    }


@app.get("/cockpit/head/dashboard")
def cockpit_head_dashboard(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    """Chart-ready head/team dashboard DTO (snake_case): per-manager open tasks / critical / debt
    value-at-risk (EUR), plus the escalation count and department value-at-risk. Reuses the same
    head/team role gate and per-manager aggregations. Non-head caller -> benign {is_head: false}
    (200, NOT 403 — the console treats any 403 as a session expiry). Unknown caller -> 404."""
    _resolve_manager(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "as_of": None, "teams": [],
                "escalated_count": 0, "total_value_at_risk_eur": 0.0}
    as_of = as_of_date or datetime.now(UTC).strftime("%Y-%m-%d")
    teams = []
    total_var = 0.0
    for mid in signals_repository.all_managers():
        try:
            debt = signals_repository.debt_dashboard_for_manager(mid, as_of)
        except Exception:  # noqa: BLE001
            continue
        var = debt["value_at_risk_eur"]
        total_var += var
        teams.append({"manager_id": mid,
                      "open_tasks": lifecycle.active_count(mid),
                      "critical": lifecycle.critical_active_count(mid),
                      "value_at_risk_eur": var})
    return {"is_head": True, "as_of": as_of, "teams": teams,
            "escalated_count": lifecycle.escalated_count(),
            "total_value_at_risk_eur": round(total_var, 2)}


@app.get("/targets/overview")
def targets_overview(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    """Head-of-sales view: target + pace for every active manager. HEAD-ONLY (team data)."""
    from app.services import targets
    _resolve_manager(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        raise HTTPException(status_code=403, detail="forbidden")
    rows = []
    for mid in signals_repository.all_managers():
        try:
            rows.append(targets.compute_target(mid, as_of=as_of_date))
        except Exception:  # noqa: BLE001
            continue
    return {"count": len(rows), "managers": rows}


def _summarize_metric(metric: dict) -> dict:
    return {k: metric[k] for k in ("target", "mtd", "attainment_pct", "pace_status")}


@app.get("/head/team")
def head_team(manager_net_uid: str, as_of_date: str | None = None) -> dict:
    """Head-of-sales dashboard: target/attainment/pace + task throughput for every manager.
    gba-nba is the authority on role. Unknown caller -> 404. A non-head caller gets a benign
    {is_head: false} with NO team data (200) — not a 403, because the console treats any 403 as a
    session expiry; the page renders 'лише для керівника' when is_head is false."""
    from app.services import targets
    _resolve_manager(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "as_of": None, "team": [], "totals": {}}

    team = []
    totals = {"shipped_target": 0.0, "shipped_mtd": 0.0, "paid_target": 0.0, "paid_mtd": 0.0,
              "generated_month": 0, "done_month": 0, "sold_month": 0, "dismissed_month": 0,
              "revenue_month": 0.0}
    as_of = None
    mids = signals_repository.all_managers()
    names = signals_repository.manager_names(mids)
    for mid in mids:
        try:
            target = targets.compute_target(mid, as_of=as_of_date)
        except Exception:  # noqa: BLE001
            continue
        as_of = target["as_of"]
        tasks = lifecycle.team_stats(mid)
        team.append({"manager_id": mid, "manager_name": names.get(mid),
                     "target": {"shipped": _summarize_metric(target["shipped"]),
                                "paid": _summarize_metric(target["paid"])},
                     "tasks": tasks})
        totals["shipped_target"] += target["shipped"]["target"]
        totals["shipped_mtd"] += target["shipped"]["mtd"]
        totals["paid_target"] += target["paid"]["target"]
        totals["paid_mtd"] += target["paid"]["mtd"]
        for k in ("generated_month", "done_month", "sold_month", "dismissed_month", "revenue_month"):
            totals[k] += tasks[k]
    for k in ("shipped_target", "shipped_mtd", "paid_target", "paid_mtd", "revenue_month"):
        totals[k] = round(totals[k], 2)
    # department-level KPI (effectiveness), derived from the totals
    totals["close_rate"] = lifecycle.close_rate(totals["done_month"], totals["dismissed_month"])
    totals["conversion_rate"] = lifecycle.conversion_rate(totals["sold_month"], totals["done_month"])
    return {"is_head": True, "as_of": as_of, "team": team, "totals": totals}


@app.get("/head/escalated")
def head_escalated(manager_net_uid: str, limit: int = 100) -> dict:
    """Head-of-sales escalation queue: SLA-breached high/critical tasks escalated by the sweep.
    Same role gate as /head/team — unknown caller -> 404; a non-head caller gets a benign
    {is_head: false} with NO tasks (200, not 403, because the console treats any 403 as a session
    expiry)."""
    _resolve_manager(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {"is_head": False, "count": 0, "tasks": []}
    items = lifecycle.escalated_tasks(limit=limit)
    for it in items:
        it["_id"] = str(it["_id"])
    return {"is_head": True, "count": len(items), "tasks": items}
