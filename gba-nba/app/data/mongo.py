"""MongoDB access — connection, collections, index setup. Graceful, lazy singleton."""
from __future__ import annotations

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
        _client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
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


def ensure_indexes() -> None:
    """Idempotent index creation — matches the access patterns in the master plan."""
    t = tasks()
    t.create_index([("task_key", ASCENDING)], unique=True, name="uq_task_key")
    t.create_index([("manager_id", ASCENDING), ("status", ASCENDING), ("priority", DESCENDING)],
                   name="ix_inbox")
    t.create_index([("client_id", ASCENDING), ("task_type", ASCENDING)], name="ix_client_type")
    t.create_index([("status", ASCENDING), ("expires_at", ASCENDING)], name="ix_expiry")
    t.create_index([("snooze_until", ASCENDING)], name="ix_snooze")
    t.create_index([("status", ASCENDING), ("due_date", ASCENDING)], name="ix_sla")
    t.create_index([("escalated_to", ASCENDING), ("status", ASCENDING)], name="ix_escalated")

    task_events().create_index([("task_key", ASCENDING), ("at", ASCENDING)], name="ix_event_task")
    manager_prefs().create_index([("manager_id", ASCENDING)], unique=True, name="uq_mgr")
    log.info("mongo_indexes_ensured")


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
