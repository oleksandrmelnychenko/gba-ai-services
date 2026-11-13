"""Append-only contacts and server-side commitment queue; never changes AI tasks."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.data import mongo
from app.domain.seller_contacts import ContactBucket, ContactCreate, ContactEvent

KYIV = ZoneInfo("Europe/Kyiv")


class ContactConflictError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _event(document: dict) -> dict:
    # tz_aware=True is configured on the production MongoClient. Also tolerate old/fixture dates.
    row = dict(document)
    for field in ("at", "next_contact_at"):
        if row.get(field) is not None and row[field].tzinfo is None:
            row[field] = row[field].replace(tzinfo=UTC)
    return ContactEvent.model_validate(row).model_dump(mode="json")


def _display_name(*values: str | None, fallback: int) -> str:
    return next((value.strip() for value in values if value and value.strip()), str(fallback))


def create(manager_id: int, manager_name: str, client: dict, request: ContactCreate) -> dict:
    collection = mongo.seller_contact_events()
    event_id = str(request.event_id)
    identity = f"{manager_id}:{event_id}"
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()

    def replay(document: dict) -> dict:
        if document["request_hash"] != fingerprint:
            raise ContactConflictError("contact_event_id_conflict")
        return _event(document)

    existing = collection.find_one({"_id": identity})
    if existing is not None:
        return replay(existing)
    now = _now()
    if request.next_contact_at is not None and request.next_contact_at <= now:
        raise ValueError("next_contact_at_must_be_future")
    document = {
        **request.model_dump(mode="python"),
        "event_id": event_id,
        "_id": identity,
        "request_hash": fingerprint,
        "sort_id": ObjectId(),
        "manager_id": manager_id,
        "manager_name": _display_name(manager_name, fallback=manager_id),
        "client_name": _display_name(client.get("full_name"), client.get("name"), fallback=request.client_id),
        "client_net_uid": client.get("client_net_uid"),
        # Mongo stores millisecond precision; use the same value for fresh/replayed responses.
        "at": now.replace(microsecond=(now.microsecond // 1000) * 1000),
    }
    if document["next_contact_at"] is not None:
        value = document["next_contact_at"]
        document["next_contact_at"] = value.replace(microsecond=(value.microsecond // 1000) * 1000)
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        # _id uniqueness exists even if startup index creation was unavailable.
        return replay(collection.find_one({"_id": identity}))
    return _event(document)


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    today = now.astimezone(KYIV).date()
    return (datetime.combine(today, time.min, KYIV).astimezone(UTC),
            datetime.combine(today + timedelta(days=1), time.min, KYIV).astimezone(UTC))


def queue(manager_id: int, client_ids: list[int], bucket: ContactBucket,
          search: str, offset: int, limit: int) -> dict:
    now = _now()
    start, end = day_bounds(now)
    filters = {
        "all": {},
        "overdue": {"next_contact_at": {"$ne": None, "$lt": start}},
        "today": {"next_contact_at": {"$gte": start, "$lt": end}},
        "upcoming": {"next_contact_at": {"$gte": end}},
        "unscheduled": {"next_contact_at": None},
    }
    pipeline = [
        {"$match": {"client_id": {"$in": client_ids}}},
        {"$sort": {"at": -1, "sort_id": -1}},
        {"$group": {"_id": "$client_id", "event": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$event"}},
    ]
    if search:
        pattern = {"$regex": re.escape(search), "$options": "i"}
        pipeline.append({"$match": {"$or": [{"client_name": pattern}, {"next_action": pattern}]}})
    facets = {key: [{"$match": value}, {"$count": "value"}] for key, value in filters.items()}
    facets["contacts"] = [
        {"$match": filters[bucket]},
        {"$addFields": {"unscheduled_rank": {"$cond": [{"$eq": ["$next_contact_at", None]}, 1, 0]}}},
        {"$sort": {"unscheduled_rank": 1, "next_contact_at": 1, "at": -1, "sort_id": -1}},
        {"$skip": offset}, {"$limit": limit},
    ]
    pipeline.append({"$facet": facets})
    result = next(iter(mongo.seller_contact_events().aggregate(pipeline, allowDiskUse=True)), {})
    counts = {key: (result[key][0]["value"] if result.get(key) else 0) for key in filters}
    contacts = [_event(row) for row in result.get("contacts", [])]
    return {"as_of": now.astimezone(KYIV).date().isoformat(), "timezone": "Europe/Kyiv",
            "bucket": bucket, "search": search, "offset": offset, "limit": limit,
            "count": len(contacts), "total": counts[bucket], "counts": counts, "contacts": contacts}


def history(manager_id: int, client_id: int, offset: int, limit: int) -> dict:
    result = next(iter(mongo.seller_contact_events().aggregate([
        {"$match": {"client_id": client_id}},
        {"$facet": {
            "total": [{"$count": "value"}],
            "events": [{"$sort": {"at": -1, "sort_id": -1}}, {"$skip": offset}, {"$limit": limit}],
        }},
    ])), {})
    events = [_event(row) for row in result.get("events", [])]
    return {"client_id": client_id, "offset": offset, "limit": limit, "count": len(events),
            "total": result["total"][0]["value"] if result.get("total") else 0, "events": events}
