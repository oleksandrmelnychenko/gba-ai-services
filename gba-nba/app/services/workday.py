"""Database-paged client work queue. Reads never mutate task rankings or contact history."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from bson import ObjectId

from app.data import mongo
from app.services import lifecycle, seller_contacts

WorkdayBucket = Literal["all", "overdue", "today", "in_progress", "upcoming", "unscheduled"]
TaskScope = Literal["active", "history"]
ACTIVE = ["open", "in_progress", "snoozed"]
HISTORY = ["done", "dismissed", "orphaned", "superseded"]
BUCKETS = ["overdue", "today", "in_progress", "upcoming", "unscheduled"]
NO_DUE = datetime(9999, 1, 1, tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _rank_fields() -> dict:
    return {
        "_manual": {"$cond": [{"$eq": ["$task_type", "manual"]}, 1, 0]},
        "_urgency": {"$switch": {"branches": [
            {"case": {"$eq": ["$urgency", urgency]}, "then": index}
            for index, urgency in enumerate(["critical", "high", "normal", "low"])
        ], "default": 9}},
        # Legacy scores deliberately fall back to persisted priority before page-only hydration.
        "_score": {"$convert": {"input": {"$ifNull": ["$ev_score", "$priority", 0]},
                                 "to": "double", "onError": 0, "onNull": 0}},
        "_priority": {"$convert": {"input": "$priority", "to": "double", "onError": 0, "onNull": 0}},
    }


def _task_sort() -> dict:
    return {"_manual": -1, "_urgency": 1, "_score": -1, "_priority": -1, "task_key": 1}


def _matches_search(fields: list[str], search: str) -> dict:
    if not search:
        return {"$literal": True}
    return {"$or": [{"$regexMatch": {"input": {"$ifNull": [f"${field}", ""]},
                                    "regex": re.escape(search), "options": "i"}} for field in fields]}


def queue(manager_id: int, manager_name: str, book: list[dict], bucket: WorkdayBucket,
          search: str, offset: int, limit: int) -> dict:
    now = _now()
    start, end = seller_contacts.day_bounds(now)
    client_ids = [int(row["client_id"]) for row in book]
    matching_book_ids = [int(row["client_id"]) for row in book if search and any(
        search.casefold() in str(row.get(field) or "").casefold() for field in ("full_name", "name"))]
    positive_client = {"$gt": [{"$ifNull": ["$client_id", 0]}, 0]}
    pipeline = [
        {"$match": {"manager_id": manager_id, "status": {"$in": ACTIVE},
                    "$or": [{"client_id": {"$in": client_ids}}, {"task_type": "manual"}]}},
        {"$set": {"_task": "$$ROOT", **_rank_fields()}},
        {"$project": {
            "_id": 0, "_kind": {"$literal": 0},
            "_manual": 1, "_urgency": 1, "_score": 1, "_priority": 1, "task_key": 1,
            "key": {"$cond": [positive_client, {"$concat": ["client:", {"$toString": "$client_id"}]},
                               {"$concat": ["task:", "$task_key"]}]},
            "client_id": {"$cond": [positive_client, "$client_id", None]},
            "client_name": {"$ifNull": ["$client_name", ""]},
            "task": "$_task", "event": {"$literal": {}}, "task_count": {"$literal": 1},
            "manual_count": "$_manual",
            "in_progress_count": {"$cond": [{"$eq": ["$status", "in_progress"]}, 1, 0]},
            "due": {"$ifNull": [{"$cond": [
                {"$and": [{"$eq": ["$status", "snoozed"]}, {"$gt": ["$snooze_until", now]}]},
                "$snooze_until", "$due_date"]}, NO_DUE]},
            "search_match": _matches_search(["client_name", "title", "reason"], search),
        }},
        {"$unionWith": {"coll": "seller_contact_events", "pipeline": [
            {"$match": {"client_id": {"$in": client_ids}}},
            {"$sort": {"client_id": 1, "at": -1, "sort_id": -1, "_id": -1}},
            {"$group": {"_id": "$client_id", "event": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$event"}},
            {"$project": {"_id": 0, "_kind": {"$literal": 1},
                          "key": {"$concat": ["client:", {"$toString": "$client_id"}]},
                          "client_id": 1, "client_name": 1, "task": {"$literal": None},
                          "event": "$$ROOT", "task_count": {"$literal": 0},
                          "manual_count": {"$literal": 0}, "in_progress_count": {"$literal": 0},
                          "due": {"$ifNull": ["$next_contact_at", NO_DUE]},
                          "search_match": _matches_search(["client_name", "next_action"], search)}},
        ]}},
        # $unionWith does not guarantee output order. Sort the combined stream explicitly:
        # real tasks first, then the complete primary-task ranking with its stable key tie-breaker.
        {"$sort": {"key": 1, "_kind": 1, **_task_sort()}},
        {"$group": {"_id": "$key", "client_id": {"$first": "$client_id"},
                    "client_name": {"$first": "$client_name"}, "primary_task": {"$first": "$task"},
                    "latest_contact": {"$mergeObjects": "$event"}, "due_at": {"$min": "$due"},
                    "task_count": {"$sum": "$task_count"}, "manual_count": {"$sum": "$manual_count"},
                    "in_progress_count": {"$sum": "$in_progress_count"},
                    "search_match": {"$max": "$search_match"}}},
        {"$match": {"$or": [{"search_match": True}, {"client_id": {"$in": matching_book_ids}}]}},
        {"$set": {"bucket": {"$switch": {"branches": [
            {"case": {"$lt": ["$due_at", start]}, "then": "overdue"},
            {"case": {"$lt": ["$due_at", end]}, "then": "today"},
            {"case": {"$gt": ["$in_progress_count", 0]}, "then": "in_progress"},
            {"case": {"$lt": ["$due_at", NO_DUE]}, "then": "upcoming"},
        ], "default": "unscheduled"}}, "manual_rank": {"$cond": [{"$gt": ["$manual_count", 0]}, 0, 1]}}},
        {"$set": {"bucket_rank": {"$indexOfArray": [BUCKETS, "$bucket"]}}},
    ]
    filters = {"all": {}, **{name: {"bucket": name} for name in BUCKETS}}
    facets = {name: [{"$match": query}, {"$count": "value"}] for name, query in filters.items()}
    facets["rows"] = [{"$match": filters[bucket]},
                      {"$sort": {"bucket_rank": 1, "manual_rank": 1, "due_at": 1, "_id": 1}},
                      {"$skip": offset}, {"$limit": limit}]
    pipeline.append({"$facet": facets})
    result = next(iter(mongo.tasks().aggregate(pipeline, allowDiskUse=True)), {})
    counts = {name: result[name][0]["value"] if result.get(name) else 0 for name in filters}
    names = {int(row["client_id"]): seller_contacts._display_name(
        row.get("full_name"), row.get("name"), fallback=int(row["client_id"])) for row in book}
    rows = []
    for document in result.get("rows", []):
        task = document["primary_task"]
        if task is not None:
            task.setdefault("client_id", None)
            lifecycle._hydrate_ranking_fields([task], persist=False)
        contact = document["latest_contact"]
        rows.append({"key": document["_id"], "client_id": document["client_id"],
                     "client_name": names.get(document["client_id"], document.get("client_name") or ""),
                     "can_contact": document["client_id"] in names,
                     "manager_id": manager_id, "manager_name": manager_name,
                     **{field: document[field] for field in
                        ("bucket", "task_count", "manual_count", "in_progress_count")},
                     "due_at": None if document["due_at"] == NO_DUE else document["due_at"],
                     "primary_task": task,
                     "latest_contact": seller_contacts._event(contact) if contact else None})
    return _json({"as_of": now.astimezone(seller_contacts.KYIV).date().isoformat(), "timezone": "Europe/Kyiv",
                  "bucket": bucket, "search": search, "offset": offset, "limit": limit,
                  "count": len(rows), "total": counts[bucket], "counts": counts, "rows": rows})


class WorkdayScopeError(ValueError):
    """The requested group is outside the current client book and own manual assignments."""


def task_page(manager_id: int, client_ids: list[int], key: str, scope: TaskScope,
              offset: int, limit: int) -> dict:
    prefix, identifier = key.split(":", 1)
    query: dict = {"manager_id": manager_id}
    if prefix == "client":
        client_id = int(identifier)
        query["client_id"] = client_id
        if client_id not in client_ids:
            query["task_type"] = "manual"
            if mongo.tasks().find_one(query, {"_id": 1}) is None:
                raise WorkdayScopeError("workday_group_not_owned")
    else:
        query.update({"task_key": identifier, "task_type": "manual",
                      "$or": [{"client_id": {"$lte": 0}}, {"client_id": None}]})
        if mongo.tasks().find_one(query, {"_id": 1}) is None:
            raise WorkdayScopeError("workday_group_not_owned")
    query["status"] = {"$in": ACTIVE if scope == "active" else HISTORY}
    result = next(iter(mongo.tasks().aggregate([
        {"$match": query},
        {"$facet": {"total": [{"$count": "value"}], "tasks": [
            {"$set": _rank_fields()}, {"$sort": _task_sort()},
            {"$skip": offset}, {"$limit": limit},
            {"$unset": ["_manual", "_urgency", "_score", "_priority"]},
        ]}},
    ])), {})
    tasks = result.get("tasks", [])
    for task in tasks:
        task.setdefault("client_id", None)
    lifecycle._hydrate_ranking_fields(tasks, persist=False)
    return _json({"key": key, "scope": scope, "offset": offset, "limit": limit, "count": len(tasks),
                  "total": result["total"][0]["value"] if result.get("total") else 0, "tasks": tasks})
