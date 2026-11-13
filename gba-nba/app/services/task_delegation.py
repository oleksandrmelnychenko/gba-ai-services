"""Head assignments change task ownership only; client and sale ownership are immutable here."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ReturnDocument

from app.data import mongo, signals_repository

ACTIVE = ["open", "in_progress", "snoozed"]


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_key: str = Field(min_length=1, max_length=500)
    expected_manager_id: int = Field(gt=0)
    expected_updated_at: datetime
    manager_id: int = Field(gt=0)
    clarification: str = Field(min_length=1, max_length=2000)

    @field_validator("expected_updated_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None:
            raise ValueError("timestamp_requires_timezone")
        return value.astimezone(UTC)

    @field_validator("clarification")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("clarification_required")
        return value.strip()


class AssignmentConflict(ValueError):
    pass


def assign(head_id: int, request: AssignmentRequest) -> dict:
    if request.manager_id not in manager_ids():
        raise ValueError("manager_not_active")
    now = datetime.now(UTC)
    now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    now = max(now, request.expected_updated_at + timedelta(milliseconds=1))
    change = {"id": str(uuid4()), "from": request.expected_manager_id, "to": request.manager_id,
              "at": now, "by": head_id, "source": "head_task_only",
              "clarification": request.clarification}
    note = {"author_id": head_id,
            "author_name": signals_repository.manager_names([head_id]).get(head_id, str(head_id)),
            "created_at": now, "text": "Призначення / уточнення: " + request.clarification}
    task = mongo.tasks().find_one_and_update(
        {"task_key": request.task_key, "manager_id": request.expected_manager_id,
         "updated_at": request.expected_updated_at, "status": {"$in": ACTIVE}},
        {"$set": {"manager_id": request.manager_id, "updated_at": now,
                  "assignment_mode": "head_task_only"},
         "$push": {"assignment_history": change, "notes": note}},
        return_document=ReturnDocument.AFTER)
    if task is None:
        raise AssignmentConflict("task_changed_or_closed")
    return task


def manager_ids() -> list[int]:
    active = mongo.tasks().distinct("manager_id", {
        "status": {"$in": ACTIVE}, "manager_id": {"$gt": 0}})
    return signals_repository.task_manager_ids(active)


def delegated_client_ids(manager_id: int) -> list[int]:
    return [int(value) for value in mongo.tasks().distinct("client_id", {
        "manager_id": manager_id, "assignment_mode": "head_task_only",
        "status": {"$in": ACTIVE}, "client_id": {"$gt": 0}})]


def contact_book(manager_id: int, client_id: int | None = None) -> list[dict]:
    owned = signals_repository.contact_book_for_manager(manager_id, client_id)
    if client_id is not None and owned:
        return owned
    delegated = set(delegated_client_ids(manager_id)) - {row["client_id"] for row in owned}
    if client_id is not None:
        delegated &= {client_id}
    if not delegated:
        return owned
    # Identity only, bounded by active assignments; no transfer of the client book.
    rows = signals_repository.contacts_for_clients(sorted(delegated))
    return owned + [{**row, "delegated_task_context": True} for row in rows.values()]
