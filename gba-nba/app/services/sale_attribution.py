"""Explicit exclusive sale links; immutable attribution, current documentary evidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo.errors import DuplicateKeyError

from app.core.money import cents
from app.data import mongo
from app.data import sale_attribution_repository as source


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_key: str = Field(min_length=1, max_length=500)
    sale_net_uid: UUID
    contact_event_id: UUID | None = None

    @field_validator("sale_net_uid", "contact_event_id")
    @classmethod
    def nonempty_identity(cls, value):
        if value is not None and value.int == 0:
            raise ValueError("document_and_contact_identity_must_not_be_empty")
        return value


class LinkConflict(ValueError):
    pass


def collection():
    return mongo.get_db()["explicit_sale_attributions"]


def public(row):
    return {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in row.items()
        if key != "_id"
    }


def owned_retry(manager_id, req: LinkRequest):
    """An exact retry reads the original owner's receipt even after a book transfer."""
    row = collection().find_one({"_id": str(req.sale_net_uid), "manager_id": manager_id})
    if row is not None and row["task_key"] == req.task_key and row.get("contact_event_id") == (
        str(req.contact_event_id) if req.contact_event_id else None
    ):
        return public(row)
    return None


def link(manager_id, manager_name, task, req: LinkRequest):
    identity = str(req.sale_net_uid)
    expected = {
        "sale_net_uid": identity,
        "task_key": req.task_key,
        "contact_event_id": str(req.contact_event_id) if req.contact_event_id else None,
    }
    existing = collection().find_one({"_id": identity})

    def replay(row):
        if row["manager_id"] != manager_id or any(row.get(k) != v for k, v in expected.items()):
            raise LinkConflict("sale_already_has_primary_task")
        return public(row)

    if existing is not None:
        return replay(existing)
    fact = source.facts([identity]).get(identity)
    if not fact or fact["client_id"] != task["client_id"]:
        raise ValueError("sale_not_in_task_client")
    if not fact["is_invoice"] or fact["canceled"] or fact["merged"] or fact["retained_lines"] <= 0:
        raise ValueError("sale_not_eligible_for_attribution")
    contact = None
    if req.contact_event_id:
        contacts = list(
            mongo.seller_contact_events()
            .find({"event_id": str(req.contact_event_id), "client_id": task["client_id"]})
            .limit(2)
        )
        if len(contacts) != 1:
            raise ValueError("contact_not_in_task_client_or_ambiguous")
        contact = contacts[0]
    now = datetime.now(UTC)
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)
    previous = task.get("updated_at")
    if previous is not None:
        previous = previous.replace(tzinfo=previous.tzinfo or UTC)
        now = max(now, previous + timedelta(milliseconds=1))
    # Linearize the ownership decision with assignment CAS. If assignment won while SQL
    # was being read, reject. If this wins, a later assignment cannot rewrite this receipt.
    accepted = mongo.tasks().update_one(
        {"task_key": task["task_key"], "manager_id": manager_id,
         "updated_at": task.get("updated_at"), "status": task["status"]},
        {"$set": {"updated_at": now}})
    if accepted.matched_count != 1:
        # A concurrent identical link may already own the sale; retain exact retry semantics.
        existing = collection().find_one({"_id": identity})
        if existing is not None:
            return replay(existing)
        raise LinkConflict("task_changed_or_reassigned")
    row = {
        "_id": identity,
        **expected,
        "client_id": task["client_id"],
        "manager_id": manager_id,
        "manager_name": manager_name,
        "linked_at": now,
        "contact_manager_id": contact["manager_id"] if contact else None,
        "offer_net_uid_at_link": fact["offer_net_uid"],
        "policy": "explicit_primary_task_v1",
        "source_at_link": fact,
    }
    # Native _id uniqueness protects retries/concurrent tasks even if optional indexes failed.
    try:
        collection().insert_one(row)
    except DuplicateKeyError:
        return replay(collection().find_one({"_id": identity}))
    return public(row)


def evidence(fact, client_id):
    if fact is None or fact["client_id"] != client_id:
        return {
            "state": "source_unavailable",
            "document": None,
            "allocated_payment_eur": None,
            "recorded_return_credit_eur": None,
            "return_credit_coverage_complete": False,
        }
    state = (
        "merged_requires_review"
        if fact["merged"]
        else "canceled"
        if fact["canceled"]
        else "draft"
        if not fact["is_invoice"]
        else "confirmed"
    )
    active_returns = [r for r in fact["returns"] if not r["canceled"] and r["credit_processed"]]
    complete = all(r["recorded_return_credit_eur"] is not None for r in active_returns)
    # This is current allocation, not net revenue or a historical period total. Refunds
    # remain separate; ambiguous merged/removed records cannot contribute money.
    paid = sum((Decimal(str(p["amount"])) for p in fact["payments"] if not p["canceled"]), Decimal(0))
    refunded = sum(
        (
            Decimal(str(r["recorded_return_credit_eur"]))
            for r in active_returns
            if r["recorded_return_credit_eur"] is not None
        ),
        Decimal(0),
    )
    return {
        "state": state,
        "document": fact,
        "allocated_payment_eur": cents(paid) if state == "confirmed" else None,
        "recorded_return_credit_eur": cents(refunded) if complete and state == "confirmed" else None,
        "return_credit_coverage_complete": complete and state == "confirmed",
    }


def page(manager_id, task_key, offset, limit, head=False):
    selector = {} if head else {"manager_id": manager_id}
    if task_key:
        selector["task_key"] = task_key
    result = next(
        iter(
            collection().aggregate(
                [
                    {"$match": selector},
                    {
                        "$facet": {
                            "count": [{"$count": "value"}],
                            "rows": [
                                {"$sort": {"linked_at": -1, "_id": 1}},
                                {"$skip": offset},
                                {"$limit": limit},
                            ],
                        }
                    },
                ]
            )
        ),
        {},
    )
    total = result["count"][0]["value"] if result.get("count") else 0
    rows = result.get("rows", [])
    facts = source.facts([row["sale_net_uid"] for row in rows])
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(rows),
        "as_of": datetime.now(UTC).isoformat(),
        "rows": [
            {**public(row), "evidence": evidence(facts.get(row["sale_net_uid"]), row["client_id"])}
            for row in rows
        ],
    }
