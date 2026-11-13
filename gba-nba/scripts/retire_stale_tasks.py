"""Stale-queue retirement report for the daily NBA run.

Dry-run by default: the task store is copied into an in-memory Mongo and the real daily worker
(stale retirement, generation, sweeps) runs against the copy, so nothing is written:
    .venv/bin/python -m scripts.retire_stale_tasks

Apply (the same daily worker against the real store):
    .venv/bin/python -m scripts.retire_stale_tasks --apply
"""
from __future__ import annotations

import argparse
import collections
import json

from app.data import mongo
from app.domain.models import ACTIVE
from app.services import orchestrator, worker

_COPIED = ("tasks", "manager_prefs", "seller_contact_events", "explicit_sale_attributions")
_ACTIVE = [status.value for status in ACTIVE]


def composition() -> dict:
    out: dict = collections.defaultdict(lambda: {"active": 0, "sla_breached": 0, "by_type": {}})
    for doc in mongo.tasks().find({"status": {"$in": _ACTIVE}},
                                  {"manager_id": 1, "task_type": 1, "sla_breached": 1}):
        row = out[doc["manager_id"]]
        row["active"] += 1
        row["sla_breached"] += bool(doc.get("sla_breached"))
        row["by_type"][doc["task_type"]] = row["by_type"].get(doc["task_type"], 0) + 1
    return {manager_id: out[manager_id] for manager_id in sorted(out)}


def _use_in_memory_copy() -> None:
    import mongomock

    source = mongo.get_db()
    copy = mongomock.MongoClient(tz_aware=True)["dry_run"]
    for name in _COPIED:
        documents = list(source[name].find({}))
        if documents:
            copy[name].insert_many(documents)
    mongo.get_db = lambda: copy


def run(apply: bool, as_of: str | None = None) -> dict:
    if not apply:
        _use_in_memory_copy()
    before = composition()
    per_manager: dict = {}
    generate = orchestrator.generate_for_manager

    def recording(manager_id, day=None):
        stats = generate(manager_id, day)
        per_manager[manager_id] = stats
        return stats

    orchestrator.generate_for_manager = recording
    try:
        totals = worker.run(as_of=as_of, push_feedback=False)
    finally:
        orchestrator.generate_for_manager = generate
    after = composition()
    managers = {}
    for manager_id in sorted(set(before) | set(after) | set(per_manager)):
        stats = per_manager.get(manager_id, {})
        managers[manager_id] = {
            "active_before": before.get(manager_id, {}).get("active", 0),
            "breached_before": before.get(manager_id, {}).get("sla_breached", 0),
            "retired": stats.get("stale_retired", 0),
            "retired_by_type": stats.get("stale_retired_by_type", {}),
            "retired_by_rule": stats.get("stale_retired_by_rule", {}),
            "candidates": stats.get("candidates", 0),
            "newly_admitted": stats.get("persisted", 0),
            "refreshed": stats.get("refreshed", 0),
            "skipped_capped": stats.get("skipped_capped", 0),
            "active_after": after.get(manager_id, {}).get("active", 0),
            "breached_after": after.get(manager_id, {}).get("sla_breached", 0),
            "by_type_before": before.get(manager_id, {}).get("by_type", {}),
            "by_type_after": after.get(manager_id, {}).get("by_type", {}),
        }
    return {"mode": "apply" if apply else "dry_run", "managers": managers, "totals": totals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.apply, args.as_of), indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
