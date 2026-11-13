from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.sale_attribution import identity
from app.data import signals_repository
from app.services import head_coaching, task_delegation, workday

router = APIRouter()


def head_identity(uid):
    mid, scope = identity(uid)
    if not signals_repository.is_head_of_sales(uid):
        raise HTTPException(403, "head_required")
    return mid, scope


@router.get("/cockpit/head/coaching")
def coaching(manager_net_uid: str, manager_id: Annotated[int | None, Query(gt=0)] = None,
             bucket: head_coaching.Bucket = "workload",
             status: Literal["all", "open", "in_progress", "snoozed"] = "all",
             date_from: date | None = None, date_to: date | None = None,
             offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
             limit: Annotated[int, Query(ge=1, le=100)] = 20):
    _, scope = head_identity(manager_net_uid)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "invalid_date_range")
    try:
        return {**scope, "is_head": True, **head_coaching.queue(manager_id, bucket, status, date_from, date_to, offset, limit)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/cockpit/head/tasks/assign")
def assign(manager_net_uid: str, request: task_delegation.AssignmentRequest):
    mid, scope = head_identity(manager_net_uid)
    try:
        task = task_delegation.assign(mid, request)
        return {**scope, "is_head": True, "task_key": task["task_key"],
                "assigned_manager_id": task["manager_id"],
                "updated_at": workday._json(task["updated_at"]),
                "assignment_history": workday._json(task["assignment_history"])}
    except task_delegation.AssignmentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
