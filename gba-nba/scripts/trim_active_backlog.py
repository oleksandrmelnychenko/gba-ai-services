"""Trim over-cap active NBA task backlog.

Dry-run by default:
    .venv/bin/python -m scripts.trim_active_backlog

Apply:
    .venv/bin/python -m scripts.trim_active_backlog --apply
"""
from __future__ import annotations

import argparse
import collections
import json
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.data import mongo
from app.domain.models import ACTIVE, TaskStatus
from app.services import lifecycle

ACTIVE_STATUSES = [s.value for s in ACTIVE]
DEFAULT_TRIMMABLE_STATUSES = {TaskStatus.OPEN.value, TaskStatus.GENERATED.value}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _task_summary(doc: dict) -> dict:
    return {
        "task_key": doc.get("task_key"),
        "status": doc.get("status"),
        "task_type": doc.get("task_type"),
        "urgency": doc.get("urgency"),
        "priority": doc.get("priority"),
        "ev_score": doc.get("ev_score"),
        "client_name": doc.get("client_name"),
    }


def _all_manager_ids() -> list[int]:
    return sorted(
        mid for mid in mongo.tasks().distinct("manager_id", {"status": {"$in": ACTIVE_STATUSES}})
        if mid is not None
    )


def plan_manager_trim(
    manager_id: int,
    *,
    target_active: int,
    trimmable_statuses: set[str] | None = None,
    sample_size: int = 3,
) -> dict:
    trimmable_statuses = trimmable_statuses or DEFAULT_TRIMMABLE_STATUSES
    lifecycle.backfill_active_ranking_fields(manager_id=manager_id, limit=10000)
    docs = list(mongo.tasks().find(
        {"manager_id": manager_id, "status": {"$in": ACTIVE_STATUSES}},
        {
            "task_key": 1,
            "manager_id": 1,
            "client_name": 1,
            "task_type": 1,
            "status": 1,
            "urgency": 1,
            "priority": 1,
            "p_outcome": 1,
            "expected_value": 1,
            "ev_score": 1,
            "signals": 1,
            "payload": 1,
        },
    ))
    lifecycle._hydrate_ranking_fields(docs, persist=False)
    docs.sort(key=lifecycle._inbox_sort_key)

    trim: list[dict] = []
    protected_over_target: list[dict] = []
    for idx, doc in enumerate(docs):
        if idx < target_active:
            continue
        if doc.get("status") in trimmable_statuses:
            trim.append(doc)
        else:
            protected_over_target.append(doc)

    keep_count = len(docs) - len(trim)
    return {
        "manager_id": manager_id,
        "target_active": target_active,
        "active_before": len(docs),
        "active_after": keep_count,
        "trim_count": len(trim),
        "protected_over_target": len(protected_over_target),
        "trim_by_type": dict(collections.Counter(d.get("task_type") for d in trim)),
        "trim_by_status": dict(collections.Counter(d.get("status") for d in trim)),
        "trim_ids": [d["_id"] for d in trim],
        "trim_boundary": [_task_summary(d) for d in trim[:sample_size]],
        "trim_lowest": [_task_summary(d) for d in trim[-sample_size:]],
    }


def apply_plan(plan: dict) -> int:
    ids = plan.get("trim_ids") or []
    if not ids:
        return 0
    reason = f"release active backlog trim over target {plan['target_active']}"
    for doc in mongo.tasks().find({"_id": {"$in": ids}}, {"task_key": 1}):
        lifecycle._event(doc["task_key"], "trim_backlog", by="system", reason=reason)
    return mongo.tasks().delete_many({"_id": {"$in": ids}}).deleted_count


def _public_plan(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k != "trim_ids"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trim over-cap active NBA task backlog.")
    parser.add_argument("--manager-id", type=int, action="append",
                        help="Manager ID to trim. Repeatable. Defaults to every manager with active tasks.")
    parser.add_argument("--target-active", type=int,
                        help="Target active tasks per manager. Defaults to cap + critical debt reserve.")
    parser.add_argument("--without-reserve", action="store_true",
                        help="Default target is max_active_tasks_per_manager only, excluding reserve.")
    parser.add_argument("--trim-status", action="append",
                        choices=[TaskStatus.OPEN.value, TaskStatus.GENERATED.value],
                        help="Status eligible for deletion. Repeatable. Defaults to open and generated.")
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--apply", action="store_true", help="Delete planned tail tasks.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    settings = get_settings()
    target_active = args.target_active
    if target_active is None:
        target_active = settings.max_active_tasks_per_manager
        if not args.without_reserve:
            target_active += settings.crit_debt_reserve
    if target_active < 0:
        raise SystemExit("--target-active must be >= 0")

    manager_ids = args.manager_id or _all_manager_ids()
    trimmable = set(args.trim_status) if args.trim_status else DEFAULT_TRIMMABLE_STATUSES
    plans = [
        plan_manager_trim(mid, target_active=target_active, trimmable_statuses=trimmable,
                          sample_size=max(0, args.sample_size))
        for mid in manager_ids
    ]

    deleted = 0
    if args.apply:
        for plan in plans:
            deleted += apply_plan(plan)
        # Re-plan after deletion so active_after reflects the actual persisted state.
        plans = [
            plan_manager_trim(mid, target_active=target_active, trimmable_statuses=trimmable,
                              sample_size=max(0, args.sample_size))
            for mid in manager_ids
        ]

    payload = {
        "mode": "apply" if args.apply else "dry_run",
        "target_active": target_active,
        "trimmable_statuses": sorted(trimmable),
        "deleted": deleted,
        "managers": [_public_plan(p) for p in plans],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=_json_default, indent=2))
    else:
        print(f"mode={payload['mode']} target_active={target_active} deleted={deleted}")
        for p in payload["managers"]:
            if p["active_before"] <= target_active and p["trim_count"] == 0:
                continue
            print(
                f"manager {p['manager_id']}: active_before={p['active_before']} "
                f"trim={p['trim_count']} active_after={p['active_after']} "
                f"protected_over_target={p['protected_over_target']} "
                f"by_type={p['trim_by_type']}"
            )
            for item in p["trim_boundary"]:
                print(
                    f"  trim boundary: {item['task_type']} {item['urgency']} "
                    f"ev={item['ev_score']} p={item['priority']} {item['task_key']}"
                )
            for item in p["trim_lowest"]:
                print(
                    f"  trim lowest:   {item['task_type']} {item['urgency']} "
                    f"ev={item['ev_score']} p={item['priority']} {item['task_key']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
