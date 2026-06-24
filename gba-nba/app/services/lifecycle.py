"""Task lifecycle — the stateful core.

Enforces the state machine (ALLOWED_TRANSITIONS), idempotent generation (upsert by task_key
without clobbering manager-owned state), status changes with audit, notes, and the inbox query.
Every mutation also appends an immutable record to task_events.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import mongo
from app.domain.models import (
    ACTIVE,
    ALLOWED_TRANSITIONS,
    TERMINAL,
    Outcome,
    Task,
    TaskStatus,
    TaskType,
    Urgency,
)

log = get_logger("lifecycle")

# Inbox ordering policy: triage by urgency band, then by business tier (debt = cash at risk first),
# then by score. Keeps critical items on top while surfacing overdue debt ahead of equal-urgency
# reorder/churn — without letting a low-urgency debt jump above a critical reorder.
_URGENCY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_TYPE_RANK = {"debt_followup": 0, "reorder_due": 1, "churn_winback": 2, "cross_sell": 3,
              "new_client_activation": 4}


def _inbox_sort_key(doc: dict) -> tuple:
    return (_URGENCY_RANK.get(doc.get("urgency"), 9),
            _TYPE_RANK.get(doc.get("task_type"), 9),
            -float(doc.get("priority") or 0))


def _now() -> datetime:
    return datetime.now(UTC)


class TransitionError(Exception):
    pass


def upsert_generated(task: Task) -> str:
    """Idempotent generation. If a task with this task_key already exists AND is still active,
    we refresh its computed fields (priority/payload/reason/contact/etc.) but PRESERVE the
    manager-owned state (status, notes, snooze, outcome). If it's terminal (done/dismissed),
    we do NOT recreate it (respect the manager's decision until the dedup window rolls over —
    the window is part of task_key). Returns the task_key."""
    now = _now()
    ttl_days = get_settings().task_ttl_days
    existing = mongo.tasks().find_one({"task_key": task.task_key})

    if existing and existing.get("status") in {s.value for s in TERMINAL}:
        # manager already resolved this exact task in this window — leave it.
        return task.task_key

    computed = {
        "manager_id": task.manager_id,
        "client_id": task.client_id,
        "client_name": task.client_name,
        "task_type": task.task_type.value,
        "title": task.title,
        "reason": task.reason,
        "priority": task.priority,
        "urgency": task.urgency.value,
        "payload": task.payload,
        "signals": task.signals,
        "explanation": task.explanation.model_dump(),
        "contact": task.contact.model_dump(),
        "due_date": task.due_date,
        "ab_variant": task.ab_variant,
        "model_version": get_settings().model_version,
        "updated_at": now,
        "expires_at": now + timedelta(days=ttl_days),
    }

    if existing:
        # refresh computed fields only; keep status/notes/history/snooze/outcome
        mongo.tasks().update_one({"task_key": task.task_key}, {"$set": computed})
        _event(task.task_key, "refresh", by="system")
    else:
        doc = {
            "task_key": task.task_key,
            "status": TaskStatus.OPEN.value,   # generated tasks go straight to OPEN for the inbox
            "notes": [],
            "status_history": [{"from": TaskStatus.GENERATED.value, "to": TaskStatus.OPEN.value,
                                "at": now, "by": "system"}],
            "snooze_until": None,
            "sla_breached": False,
            "escalated_to": None,
            "outcome": None,
            "generated_at": now,
            **computed,
        }
        mongo.tasks().insert_one(doc)
        _event(task.task_key, "generated", by="system")
    return task.task_key


def change_status(task_key: str, to: TaskStatus, by: int, reason: str | None = None,
                  outcome: Outcome | None = None, snooze_until: datetime | None = None) -> dict:
    doc = mongo.tasks().find_one({"task_key": task_key})
    if not doc:
        raise TransitionError(f"task not found: {task_key}")
    current = TaskStatus(doc["status"])
    if to not in ALLOWED_TRANSITIONS.get(current, set()):
        raise TransitionError(f"illegal transition {current.value} -> {to.value}")

    now = _now()
    change = {"from": current.value, "to": to.value, "at": now, "by": by}
    if reason:
        change["reason"] = reason
    if outcome:
        change["outcome"] = outcome.model_dump()

    update: dict = {
        "$set": {"status": to.value, "updated_at": now},
        "$push": {"status_history": change},
    }
    if to == TaskStatus.SNOOZED:
        update["$set"]["snooze_until"] = snooze_until or (now + timedelta(days=1))
    if outcome:
        update["$set"]["outcome"] = outcome.model_dump()

    updated = mongo.tasks().find_one_and_update(
        {"task_key": task_key}, update, return_document=ReturnDocument.AFTER)
    _event(task_key, f"status:{to.value}", by=by, reason=reason)

    # DISMISS → mute (client, type) so regeneration doesn't re-spam (anti-spam rule)
    if to == TaskStatus.DISMISSED:
        _mute_after_dismiss(doc["manager_id"], doc["client_id"], doc["task_type"])
    return updated


def add_note(task_key: str, author_id: int, text: str) -> dict:
    now = _now()
    note = {"author_id": author_id, "text": text, "created_at": now}
    updated = mongo.tasks().find_one_and_update(
        {"task_key": task_key},
        {"$push": {"notes": note}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER)
    if not updated:
        raise TransitionError(f"task not found: {task_key}")
    _event(task_key, "note", by=author_id, reason=text[:120])
    return updated


def inbox(manager_id: int, limit: int = 50, statuses: list[str] | None = None) -> list[dict]:
    """Manager's active task queue (the cockpit query). Ordered by urgency band, then business
    tier (debt first), then score — see _inbox_sort_key. SNOOZED tasks whose snooze_until has
    passed are surfaced as OPEN-eligible."""
    now = _now()
    statuses = statuses or [TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]
    query = {
        "manager_id": manager_id,
        "$or": [
            {"status": {"$in": statuses}},
            {"status": TaskStatus.SNOOZED.value, "snooze_until": {"$lte": now}},
        ],
    }
    # fetch the top page by score (cap == limit, so this is the full active set), then apply the
    # urgency/tier display ordering in-process.
    docs = list(mongo.tasks().find(query).sort("priority", -1).limit(limit))
    docs.sort(key=_inbox_sort_key)
    return docs


def wake_snoozed() -> int:
    """Move snoozed tasks whose time has come back to OPEN. Run by a sweep."""
    now = _now()
    res = mongo.tasks().update_many(
        {"status": TaskStatus.SNOOZED.value, "snooze_until": {"$lte": now}},
        {"$set": {"status": TaskStatus.OPEN.value, "updated_at": now},
         "$push": {"status_history": {"from": TaskStatus.SNOOZED.value, "to": TaskStatus.OPEN.value,
                                      "at": now, "by": "system"}}})
    return res.modified_count


def sweep_sla(escalate_to: int | None = None) -> dict:
    """Flag overdue active tasks as SLA-breached; when escalate_to (the head's User.ID) is given,
    escalate high/critical breached tasks to them by setting escalated_to (surfaced to the head)."""
    now = _now()
    active_values = list(s.value for s in ACTIVE)
    flagged = mongo.tasks().update_many(
        {"status": {"$in": active_values}, "due_date": {"$lt": now}, "sla_breached": False},
        {"$set": {"sla_breached": True, "updated_at": now}}).modified_count
    escalated = 0
    if escalate_to is not None:
        escalated = mongo.tasks().update_many(
            {"status": {"$in": active_values}, "sla_breached": True,
             "urgency": {"$in": [Urgency.CRITICAL.value, Urgency.HIGH.value]},
             "$or": [{"escalated_to": None}, {"escalated_to": {"$exists": False}}]},
            {"$set": {"escalated_to": escalate_to, "updated_at": now}}).modified_count
    return {"flagged": flagged, "escalated": escalated}


def sweep_expired() -> int:
    """Delete stale never-actioned tasks (ACTIVE past expires_at) so the inbox doesn't accumulate
    cruft. Only active tasks are purged — DONE/DISMISSED are kept for KPI/audit (a blanket Mongo TTL
    index would wrongly drop completed tasks the monthly KPI needs)."""
    res = mongo.tasks().delete_many(
        {"status": {"$in": list(s.value for s in ACTIVE)}, "expires_at": {"$lt": _now()}})
    return res.deleted_count


def feedback_rejections(manager_id: int, window_days: int = 90) -> dict:
    """Recent negative signals per (client_id, task_type): tasks the manager DISMISSED or completed
    without a sale (done-not-sold), over the window. Used to penalise repeatedly-rejected pairs so
    the queue learns from behaviour. Returns {(client_id, task_type): count}."""
    since = _now() - timedelta(days=window_days)
    out: dict = {}
    cursor = mongo.tasks().find(
        {"manager_id": manager_id, "updated_at": {"$gte": since},
         "$or": [{"status": TaskStatus.DISMISSED.value},
                 {"status": TaskStatus.DONE.value, "outcome.sold": False}]},
        {"client_id": 1, "task_type": 1})
    for doc in cursor:
        key = (doc.get("client_id"), doc.get("task_type"))
        out[key] = out.get(key, 0) + 1
    return out


def cross_sell_negatives(window_days: int = 90) -> dict[int, set[int]]:
    """Cross-service negative feedback for reco: per client, the product_ids from cross_sell tasks
    a manager DISMISSED or completed without a sale within the window. Pushed to reco so it stops
    recommending those products to that client. Aggregated across all managers (reco is keyed by
    client, not manager). Returns {client_id: {product_id, ...}}."""
    since = _now() - timedelta(days=window_days)
    out: dict[int, set[int]] = {}
    cursor = mongo.tasks().find(
        {"task_type": TaskType.CROSS_SELL.value, "updated_at": {"$gte": since},
         "$or": [{"status": TaskStatus.DISMISSED.value},
                 {"status": TaskStatus.DONE.value, "outcome.sold": False}]},
        {"client_id": 1, "payload": 1})
    for doc in cursor:
        cid = doc.get("client_id")
        prods = (doc.get("payload") or {}).get("products") or []
        pids = {int(p["product_id"]) for p in prods if p.get("product_id") is not None}
        if cid is not None and pids:
            out.setdefault(cid, set()).update(pids)
    return out


def escalated_tasks(limit: int = 100) -> list[dict]:
    """Active tasks escalated to the head (escalated_to set) — the head's escalation queue.
    Ordered by the same urgency band -> business tier -> score policy as the manager inbox."""
    query = {
        "status": {"$in": list(s.value for s in ACTIVE)},
        "escalated_to": {"$ne": None, "$exists": True},
    }
    docs = list(mongo.tasks().find(query).sort("priority", -1).limit(limit))
    docs.sort(key=_inbox_sort_key)
    return docs


def active_count(manager_id: int) -> int:
    return mongo.tasks().count_documents(
        {"manager_id": manager_id, "status": {"$in": list(s.value for s in ACTIVE)}})


def escalated_count() -> int:
    """Count of active tasks escalated to a head (the head escalation queue size), unbounded —
    the head dashboard reports the true total, not a page cap."""
    return mongo.tasks().count_documents(
        {"status": {"$in": list(s.value for s in ACTIVE)},
         "escalated_to": {"$ne": None, "$exists": True}})


def _active_counts(manager_id: int, field: str) -> dict:
    """Count a manager's ACTIVE tasks grouped by a field — lets generation honour per-client/per-type
    caps against tasks ALREADY in the inbox (so on-demand re-runs can't exceed the daily caps)."""
    out: dict = {}
    for doc in mongo.tasks().find(
            {"manager_id": manager_id, "status": {"$in": list(s.value for s in ACTIVE)}},
            {field: 1}):
        key = doc.get(field)
        if key is not None:
            out[key] = out.get(key, 0) + 1
    return out


def active_counts_by_client(manager_id: int) -> dict:
    return _active_counts(manager_id, "client_id")


def active_counts_by_type(manager_id: int) -> dict:
    return _active_counts(manager_id, "task_type")


def get_task(task_key: str) -> dict | None:
    return mongo.tasks().find_one({"task_key": task_key})


def count_active_by_urgency(manager_id: int) -> dict:
    """Active inbox tasks bucketed by urgency, consistent with inbox() surfacing rules:
    open/in_progress plus snoozed tasks whose snooze_until has passed."""
    counts = {"critical": 0, "high": 0, "normal": 0, "low": 0, "total": 0}
    for doc in mongo.tasks().find(_active_inbox_query(manager_id), {"urgency": 1}):
        bucket = doc.get("urgency")
        if bucket in counts:
            counts[bucket] += 1
        counts["total"] += 1
    return counts


def _active_inbox_query(manager_id: int) -> dict:
    """The inbox surfacing predicate (open/in_progress + woken snoozed) shared by count/dashboard."""
    now = _now()
    return {
        "manager_id": manager_id,
        "$or": [
            {"status": {"$in": [TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]}},
            {"status": TaskStatus.SNOOZED.value, "snooze_until": {"$lte": now}},
        ],
    }


def dashboard_counts(manager_id: int) -> dict:
    """Chart-ready counts for a manager dashboard, computed from the SAME task store the cockpit
    inbox/count use — no separate scoring. Returns:
      task_type_mix: active inbox tasks by task_type (inbox surfacing rule);
      urgency_mix:   active inbox tasks by urgency band (same rule as count_active_by_urgency);
      completed_vs_open: open = active inbox count; done/dismissed = resolved this calendar month
                         (same month window as team_stats), so the manager view matches the head view.
    """
    type_mix: dict[str, int] = {}
    urgency_mix = {"critical": 0, "high": 0, "normal": 0, "low": 0}
    open_count = 0
    for doc in mongo.tasks().find(_active_inbox_query(manager_id), {"task_type": 1, "urgency": 1}):
        open_count += 1
        tt = doc.get("task_type")
        if tt is not None:
            type_mix[tt] = type_mix.get(tt, 0) + 1
        u = doc.get("urgency")
        if u in urgency_mix:
            urgency_mix[u] += 1

    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    done_month = mongo.tasks().count_documents(
        {"manager_id": manager_id, "status": TaskStatus.DONE.value,
         "updated_at": {"$gte": month_start, "$lt": next_month}})
    dismissed_month = mongo.tasks().count_documents(
        {"manager_id": manager_id, "status": TaskStatus.DISMISSED.value,
         "updated_at": {"$gte": month_start, "$lt": next_month}})

    return {
        "task_type_mix": [{"type": tt, "count": n} for tt, n in sorted(type_mix.items())],
        "urgency_mix": [{"urgency": u, "count": urgency_mix[u]}
                        for u in ("critical", "high", "normal", "low")],
        "completed_vs_open": [{"status": "open", "count": open_count},
                              {"status": "done", "count": done_month},
                              {"status": "dismissed", "count": dismissed_month}],
    }


def critical_active_count(manager_id: int) -> int:
    """Active inbox tasks at CRITICAL urgency (inbox surfacing rule) — for the head dashboard."""
    q = dict(_active_inbox_query(manager_id))
    q["urgency"] = Urgency.CRITICAL.value
    return mongo.tasks().count_documents(q)


def team_stats(manager_id: int) -> dict:
    """Per-manager task throughput for the head dashboard. active = ACTIVE-status count;
    done_month/dismissed_month = tasks moved to that terminal status with updated_at in the
    current calendar month; sold_month / revenue_month = done-this-month tasks with a sold outcome."""
    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)

    active = mongo.tasks().count_documents(
        {"manager_id": manager_id, "status": {"$in": list(s.value for s in ACTIVE)}})
    generated_month = mongo.tasks().count_documents(
        {"manager_id": manager_id, "generated_at": {"$gte": month_start, "$lt": next_month}})

    done_month = sold_month = dismissed_month = 0
    revenue_month = 0.0
    closed = mongo.tasks().find(
        {"manager_id": manager_id,
         "status": {"$in": [TaskStatus.DONE.value, TaskStatus.DISMISSED.value]},
         "updated_at": {"$gte": month_start, "$lt": next_month}},
        {"status": 1, "outcome": 1})
    for doc in closed:
        if doc.get("status") == TaskStatus.DISMISSED.value:
            dismissed_month += 1
            continue
        done_month += 1
        outcome = doc.get("outcome") or {}
        if outcome.get("sold"):
            sold_month += 1
            revenue_month += float(outcome.get("amount") or 0.0)
    return {"active": active, "generated_month": generated_month, "done_month": done_month,
            "sold_month": sold_month, "dismissed_month": dismissed_month,
            "revenue_month": round(revenue_month, 2),
            # KPI (effectiveness) — derived, no extra query:
            "close_rate": close_rate(done_month, dismissed_month),       # actioned vs resolved
            "conversion_rate": conversion_rate(sold_month, done_month)}  # sold vs done


def close_rate(done: int, dismissed: int) -> float:
    """Of the tasks a manager RESOLVED this month, the share they actioned (done) vs dropped."""
    resolved = done + dismissed
    return round(done / resolved, 3) if resolved else 0.0


def conversion_rate(sold: int, done: int) -> float:
    """Of the tasks a manager completed (done), the share that resulted in a sale."""
    return round(sold / done, 3) if done else 0.0


def is_muted(manager_id: int, client_id: int, task_type: str) -> bool:
    pref = mongo.manager_prefs().find_one({"manager_id": manager_id})
    if not pref:
        return False
    if task_type in (pref.get("muted_types") or []):
        return True
    now = _now()
    for m in pref.get("muted_pairs") or []:
        if m.get("client_id") == client_id and m.get("task_type") == task_type:
            until = m.get("mute_until")
            if until and _as_aware(until) > now:
                return True
    return False


def _as_aware(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware (some stores drop tzinfo on round-trip)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _mute_after_dismiss(manager_id: int, client_id: int, task_type: str) -> None:
    until = _now() + timedelta(days=get_settings().dismiss_mute_days)
    mongo.manager_prefs().update_one(
        {"manager_id": manager_id},
        {"$pull": {"muted_pairs": {"client_id": client_id, "task_type": task_type}}})
    mongo.manager_prefs().update_one(
        {"manager_id": manager_id},
        {"$push": {"muted_pairs": {"client_id": client_id, "task_type": task_type, "mute_until": until}}},
        upsert=True)


def _event(task_key: str, kind: str, by: int | str, reason: str | None = None) -> None:
    mongo.task_events().insert_one(
        {"task_key": task_key, "kind": kind, "by": by, "reason": reason, "at": _now()})
