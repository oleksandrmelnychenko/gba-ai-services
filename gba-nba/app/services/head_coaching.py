"""Current coaching queues, with full-population counts and no activity quality score."""
from datetime import UTC, date, datetime
from typing import Literal

from app.data import mongo, signals_repository
from app.services import seller_contacts, task_delegation, workday

Bucket = Literal["workload", "overdue_promises", "without_next_contact", "repeat_tasks"]


def queue(manager_id: int | None, bucket: Bucket, status: str,
          date_from: date | None, date_to: date | None, offset: int, limit: int):
    now = datetime.now(UTC)
    day_start, _ = seller_contacts.day_bounds(now)
    managers = task_delegation.manager_ids()
    if manager_id is not None and manager_id not in managers:
        raise ValueError("manager_not_active")
    scope = [manager_id] if manager_id is not None else managers
    names = signals_repository.manager_names(managers)
    ph, params = signals_repository.in_clause("manager", scope) if scope else ("(NULL)", {})
    book = signals_repository.query(f"""
        SELECT c.ID AS client_id, c.MainManagerID AS manager_id,
               COALESCE(NULLIF(c.FullName, ''), c.Name) AS client_name
        FROM dbo.Client c WHERE c.Deleted = 0 AND c.MainManagerID IN {ph}
    """, params)
    clients = {int(row["client_id"]): row for row in book}
    latest = list(mongo.seller_contact_events().aggregate([
        {"$match": {"client_id": {"$in": list(clients)}}},
        {"$sort": {"client_id": 1, "at": -1, "sort_id": -1, "_id": -1}},
        {"$group": {"_id": "$client_id", "event": {"$first": "$$ROOT"}}}]))
    contacts = {row["_id"]: row["event"] for row in latest}
    tasks = list(mongo.tasks().find({"manager_id": {"$in": scope},
                                    "status": {"$in": task_delegation.ACTIVE}},
        {"task_key": 1, "manager_id": 1, "client_id": 1, "client_name": 1, "title": 1,
         "task_type": 1, "status": 1, "due_date": 1, "updated_at": 1,
         "created_at": 1, "assignment_history": 1}))
    groups = {name: [] for name in ("workload", "overdue_promises", "without_next_contact", "repeat_tasks")}

    def aware(value):
        return value.replace(tzinfo=value.tzinfo or UTC) if value else None

    def date_matches(value):
        if not date_from and not date_to:
            return True
        if value is None:
            return False
        local = aware(value).astimezone(seller_contacts.KYIV).date()
        return (not date_from or local >= date_from) and (not date_to or local <= date_to)

    for task in tasks:
        if (status != "all" and task["status"] != status) or not date_matches(task.get("due_date")):
            continue
        row = {"key": task["task_key"], "task_key": task["task_key"],
               "client_id": task.get("client_id"), "client_name": task.get("client_name") or "",
               "manager_id": task["manager_id"], "manager_name": names.get(task["manager_id"], str(task["manager_id"])),
               "title": task.get("title") or task["task_key"], "status": task["status"],
               "task_type": task["task_type"], "due_at": aware(task.get("due_date")),
               "updated_at": aware(task.get("updated_at")), "contact_at": None,
               "assignment_history": task.get("assignment_history", [])}
        groups["workload"].append(row)
        if task["task_type"] == "reorder_due":
            groups["repeat_tasks"].append(row)
    for cid, client in clients.items():
        event = contacts.get(cid, {})
        due = aware(event.get("next_contact_at"))
        # Task status filters do not silently alter the client-contact population.
        if not date_matches(due):
            continue
        row = {"key": f"client:{cid}", "task_key": None, **client,
               "manager_name": names.get(client["manager_id"], str(client["manager_id"])),
               "title": event.get("next_action") or "Наступний контакт не запланований",
               "status": None, "task_type": None, "due_at": due, "updated_at": None,
               "contact_at": aware(event.get("at")), "assignment_history": []}
        if due is None:
            groups["without_next_contact"].append(row)
        elif due < day_start:
            groups["overdue_promises"].append(row)
    rows = sorted(groups[bucket], key=lambda row: (row["due_at"] or datetime.max.replace(tzinfo=UTC), row["key"]))
    return workday._json({"as_of": now, "timezone": "Europe/Kyiv", "requested_manager_id": manager_id,
        "bucket": bucket, "status": status, "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None, "offset": offset, "limit": limit,
        "total": len(rows), "count": len(rows[offset:offset+limit]),
        "counts": {name: len(items) for name, items in groups.items()},
        "rows": rows[offset:offset+limit],
        "managers": [{"id": mid, "name": names.get(mid, str(mid))} for mid in managers],
        "basis": "current_tasks_and_latest_client_contact", "task_status_applies_to": ["workload", "repeat_tasks"]})
