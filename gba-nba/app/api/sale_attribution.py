"""Explicit documentary attribution routes under the existing environment/auth fence."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.data import sale_attribution_repository as source
from app.data import signals_repository
from app.services import lifecycle, task_delegation
from app.services import sale_attribution as service

router = APIRouter()


def identity(uid):
    # Lazy import avoids coupling the standalone router to application construction.
    from app.api.main import _canonical_net_uid, _resolve_manager

    mid = _resolve_manager(uid)
    return mid, {"manager_id": mid, "manager_net_uid": _canonical_net_uid(uid)}


def owned_task(mid, key):
    task = lifecycle.get_task(key)
    if not task or task.get("manager_id") != mid or not task.get("client_id"):
        raise HTTPException(404, "task_not_owned")
    if not task_delegation.contact_book(mid, task["client_id"]):
        raise HTTPException(404, "client_not_in_manager_book")
    return task


@router.get("/cockpit/attribution/candidates")
def candidates(
    manager_net_uid: str,
    task_key: Annotated[str, Query(min_length=1, max_length=500)],
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    mid, scope = identity(manager_net_uid)
    task = owned_task(mid, task_key)
    rows, total = source.candidates(task["client_id"], offset, limit)
    return {
        **scope,
        "task_key": task_key,
        "client_id": task["client_id"],
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(rows),
        "rows": rows,
    }


@router.post("/cockpit/attribution/links")
def link(manager_net_uid: str, req: service.LinkRequest):
    mid, scope = identity(manager_net_uid)
    replay = service.owned_retry(mid, req)
    if replay is not None:
        return {**scope, "link": replay}
    task = owned_task(mid, req.task_key)
    name = signals_repository.manager_names([mid]).get(mid, str(mid))
    try:
        row = service.link(mid, name, task, req)
    except service.LinkConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**scope, "link": row}


@router.get("/cockpit/attribution/links")
def links(
    manager_net_uid: str,
    task_key: Annotated[str, Query(max_length=500)] = "",
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    mid, scope = identity(manager_net_uid)
    return {**scope, **service.page(mid, task_key, offset, limit)}


@router.get("/cockpit/head/attribution/links")
def head_links(
    manager_net_uid: str,
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    mid, scope = identity(manager_net_uid)
    if not signals_repository.is_head_of_sales(manager_net_uid):
        return {
            **scope,
            "is_head": False,
            "offset": offset,
            "limit": limit,
            "total": 0,
            "count": 0,
            "rows": [],
        }
    return {**scope, "is_head": True, **service.page(mid, "", offset, limit, head=True)}
