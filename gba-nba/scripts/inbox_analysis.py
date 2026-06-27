"""Post-generation analysis: urgency-band x task_type cross-tab + top-of-inbox mix check."""
from __future__ import annotations

import collections

from app.data import mongo

BANDS = ["critical", "high", "normal", "low"]
TYPES = ["debt_followup", "reorder_due", "churn_winback", "cross_sell", "new_client_activation"]


def main():
    docs = list(mongo.tasks().find(
        {"status": {"$in": ["generated", "open", "in_progress"]}},
        {"task_type": 1, "urgency": 1, "priority": 1, "p_outcome": 1,
         "expected_value": 1, "ev_score": 1, "manager_id": 1}))
    print(f"=== inbox analysis: {len(docs)} active tasks ===\n")

    ct = collections.defaultdict(lambda: collections.Counter())
    for d in docs:
        ct[d.get("task_type")][d.get("urgency")] += 1
    hdr = f"{'task_type':24}" + "".join(f"{b:>10}" for b in BANDS) + f"{'total':>8}"
    print(hdr)
    for t in TYPES:
        row = ct.get(t, collections.Counter())
        line = f"{t:24}" + "".join(f"{row.get(b,0):>10}" for b in BANDS) + f"{sum(row.values()):>8}"
        print(line)
    print()

    by_type_prio = collections.defaultdict(list)
    by_type_ev = collections.defaultdict(list)
    for d in docs:
        by_type_prio[d.get("task_type")].append(d.get("priority", 0))
        by_type_ev[d.get("task_type")].append(d.get("ev_score", d.get("priority", 0)))
    print("P(outcome)% distribution by task type:")
    print(f"{'task_type':24}{'min':>8}{'median':>8}{'max':>8}")
    for t in TYPES:
        ps = sorted(by_type_prio.get(t, []))
        if ps:
            print(f"{t:24}{ps[0]:>8.1f}{ps[len(ps)//2]:>8.1f}{ps[-1]:>8.1f}")
    print()
    print("EV score distribution by task type:")
    print(f"{'task_type':24}{'min':>10}{'median':>10}{'max':>10}")
    for t in TYPES:
        es = sorted(by_type_ev.get(t, []))
        if es:
            print(f"{t:24}{es[0]:>10.1f}{es[len(es)//2]:>10.1f}{es[-1]:>10.1f}")
    print()

    # top-of-inbox mix: of the top-10 tasks per manager (inbox order), how many are each type?
    from app.services import lifecycle
    print("TOP-10 composition per manager (inbox-ordered):")
    for mid in sorted({d["manager_id"] for d in docs}):
        inbox = lifecycle.inbox(mid)[:10]
        comp = collections.Counter(d.get("task_type") for d in inbox)
        crit_first = [d.get("task_type") for d in inbox[:3]]
        print(f"  mgr {mid}: {dict(comp)} | first3={crit_first}")


if __name__ == "__main__":
    main()
