"""Real generation run: generate every manager's inbox into Mongo, then inspect the result.

Run: .venv/bin/python -m scripts.realdata_generate [as_of]
"""
from __future__ import annotations

import collections
import sys

from app.data import signals_repository as R
from app.services import lifecycle, orchestrator


def main():
    as_of = sys.argv[1] if len(sys.argv) > 1 else "2026-06-08"
    mgrs = sorted(set(R.all_managers()) | set(R.head_user_ids()))
    names = R.manager_names(mgrs)
    print(f"=== generation run @ {as_of} | {len(mgrs)} managers ===\n")

    for mid in mgrs:
        stats = orchestrator.generate_for_manager(mid, as_of)
        if stats["persisted"] == 0 and stats["candidates"] == 0:
            continue
        nm = names.get(mid, "?")
        inbox = lifecycle.inbox(mid)
        urg = collections.Counter(d.get("urgency") for d in inbox)
        typ = collections.Counter(d.get("task_type") for d in inbox)
        prios = [d.get("priority", 0) for d in inbox]
        print(f"--- mgr {mid} ({nm}) ---")
        print(f"  candidates={stats['candidates']} persisted={stats['persisted']} "
              f"refreshed={stats.get('refreshed',0)} skipped_capped={stats.get('skipped_capped',0)} "
              f"skipped_muted={stats.get('skipped_muted',0)}")
        print(f"  inbox={len(inbox)}  by_type={dict(typ)}")
        print(f"  urgency={dict(urg)}  priority[min={min(prios):.1f} med={sorted(prios)[len(prios)//2]:.1f} max={max(prios):.1f}]" if prios else "  (empty inbox)")
        print("  TOP 8:")
        for d in inbox[:8]:
            print(f"    [{d.get('urgency','?'):8}] p{d.get('priority',0):5.1f} {d.get('task_type','?'):22} "
                  f"{(d.get('client_name') or '?')[:28]:28} | {(d.get('reason') or '')[:60]}")
        print()


if __name__ == "__main__":
    main()
