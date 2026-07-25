"""MongoDB access — connection, collections, index setup. Graceful, lazy singleton."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("mongo")

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = MongoClient(
            s.mongo_uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=5000,
            socketTimeoutMS=20000, tz_aware=True,
        )
        log.info("mongo_connected", db=s.mongo_db)
    return _client


def get_db() -> Database:
    return get_client()[get_settings().mongo_db]


def tasks() -> Collection:
    return get_db()["tasks"]


def task_events() -> Collection:
    return get_db()["task_events"]


def manager_prefs() -> Collection:
    return get_db()["manager_prefs"]


def generation_runs() -> Collection:
    return get_db()["generation_runs"]


def ensure_indexes() -> None:
    """Idempotent index creation — matches the access patterns in the master plan."""
    t = tasks()
    t.create_index([("task_key", ASCENDING)], unique=True, name="uq_task_key")
    t.create_index([("manager_id", ASCENDING), ("status", ASCENDING), ("priority", DESCENDING)],
                   name="ix_inbox")
    t.create_index([("client_id", ASCENDING), ("task_type", ASCENDING)], name="ix_client_type")
    t.create_index([("status", ASCENDING), ("expires_at", ASCENDING)], name="ix_expiry")
    # team live board (/head/tasks): status $in + optional urgency match, sorted with in_progress
    # work surfaced first. (status, urgency, in_progress_since) serves the team-wide filter+sort.
    t.create_index([("status", ASCENDING), ("urgency", ASCENDING), ("in_progress_since", ASCENDING)],
                   name="ix_team_board")
    t.create_index([("snooze_until", ASCENDING)], name="ix_snooze")
    t.create_index([("status", ASCENDING), ("due_date", ASCENDING)], name="ix_sla")
    t.create_index([("escalated_to", ASCENDING), ("status", ASCENDING)], name="ix_escalated")

    task_events().create_index([("task_key", ASCENDING), ("at", ASCENDING)], name="ix_event_task")
    # task_events is write-only audit (nothing reads it in the serving path) and every daily
    # run appends a refresh-event per active task — expire instead of growing forever.
    task_events().create_index(
        [("at", ASCENDING)], name="ttl_events", expireAfterSeconds=180 * 24 * 3600,
    )
    manager_prefs().create_index([("manager_id", ASCENDING)], unique=True, name="uq_mgr")
    generation_runs().create_index(
        [("completed_at", DESCENDING)], name="ix_generation_completed"
    )
    generation_runs().create_index(
        [("completed_at", ASCENDING)],
        name="ttl_generation_runs",
        expireAfterSeconds=30 * 24 * 3600,
    )
    log.info("mongo_indexes_ensured")


def record_generation_run(stats: dict, *, full_run: bool) -> None:
    """Persist the scheduler proof used by business-aware readiness."""
    managers = int(stats.get("managers") or 0)
    ok = int(stats.get("ok") or 0)
    failed = int(stats.get("failed") or 0)
    generation_runs().insert_one(
        {
            **stats,
            "full_run": full_run,
            "success": full_run and managers > 0 and ok == managers and failed == 0,
            "completed_at": datetime.now(UTC),
        }
    )


def generation_readiness(max_lag_hours: int) -> dict:
    """Read-only proof that a complete task generation finished recently and cleanly."""
    run = generation_runs().find_one({}, sort=[("completed_at", DESCENDING)])
    total_tasks = tasks().count_documents({})
    active_tasks = tasks().count_documents(
        {"status": {"$in": ["generated", "open", "in_progress", "snoozed"]}}
    )
    latest_refresh = tasks().find_one(
        {},
        projection={"updated_at": 1, "_id": 0},
        sort=[("updated_at", DESCENDING)],
    )
    reasons: list[str] = []
    completed_at = run.get("completed_at") if run else None
    if completed_at is not None and completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    recent = isinstance(completed_at, datetime) and completed_at >= datetime.now(UTC) - timedelta(
        hours=max_lag_hours
    )
    if run is None:
        reasons.append("generation_run_missing")
    elif not run.get("full_run"):
        reasons.append("generation_run_partial")
    elif not run.get("success"):
        reasons.append("generation_run_failed")
    elif not recent:
        reasons.append("generation_run_stale")
    if total_tasks <= 0:
        reasons.append("task_store_empty")
    return {
        "generation_ready": not reasons,
        "generation_reasons": reasons,
        "last_generation_at": completed_at.isoformat() if completed_at else None,
        "last_generation_managers": int(run.get("managers") or 0) if run else 0,
        "last_generation_ok": int(run.get("ok") or 0) if run else 0,
        "last_generation_failed": int(run.get("failed") or 0) if run else 0,
        "task_count": total_tasks,
        "active_task_count": active_tasks,
        "latest_task_refresh_at": (
            latest_refresh["updated_at"].isoformat()
            if latest_refresh and latest_refresh.get("updated_at")
            else None
        ),
    }


def ping() -> bool:
    try:
        get_client().admin.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
