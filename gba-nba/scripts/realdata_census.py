"""Read-only census of NBA signals over real ConcordDb_V5 — ground truth for calibration.

Runs every signal query for every active manager and prints candidate volumes + distributions
BEFORE quota/cap, plus the target run-rate. No Mongo writes. Run: .venv/bin/python -m scripts.realdata_census
"""
from __future__ import annotations

import statistics as st
import sys

from app.data import signals_repository as R
from app.services import targets


def pct(vals, p):
    if not vals:
        return 0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
    return vals[k]


def dist(vals):
    if not vals:
        return "n=0"
    return (f"n={len(vals)} min={min(vals):.0f} p25={pct(vals,25):.0f} med={st.median(vals):.0f} "
            f"p75={pct(vals,75):.0f} p90={pct(vals,90):.0f} max={max(vals):.0f}")


def distf(vals):
    if not vals:
        return "n=0"
    return (f"n={len(vals)} min={min(vals):.2f} p25={pct(vals,25):.2f} med={st.median(vals):.2f} "
            f"p75={pct(vals,75):.2f} p90={pct(vals,90):.2f} max={max(vals):.2f}")


def main():
    as_of = sys.argv[1] if len(sys.argv) > 1 else "2026-06-08"
    print(f"=== NBA real-data census @ {as_of} ===\n")

    excl = R.ubiquitous_product_ids(0.20)
    suffix = "..." if len(excl) > 10 else ""
    print(f"Ubiquity-excluded SKUs (>20% of clients): {len(excl)} -> {sorted(excl)[:10]}{suffix}")
    if excl:
        names = R.product_names(sorted(excl))
        for pid in sorted(excl):
            print(f"    {pid}: {names.get(pid,'?')}")

    heads = R.head_user_ids()
    mgrs = R.all_managers()
    names = R.manager_names(mgrs + heads)
    print(f"\nHeads: {heads}  ({', '.join(names.get(h,'?') for h in heads)})")
    print(f"Managers with clients: {len(mgrs)} -> {sorted(mgrs)}\n")

    for mid in sorted(mgrs):
        nm = names.get(mid, "?")
        clients = R.clients_for_manager(mid)
        debts = R.overdue_debts_for_manager(mid, as_of)
        reorders = R.reorder_candidates_for_manager(mid, as_of)
        churn = R.churn_candidates_for_manager(mid, as_of)
        newc = R.new_clients_for_manager(mid, as_of)

        print(f"--- mgr {mid} ({nm}) | clients={len(clients)} ---")
        debt_amounts = [float(d["overdue_amount"]) for d in debts]
        print(f"  DEBT     clients={len(debts)}  amount€[{dist(debt_amounts)}]")
        print(f"           overdue_days[{dist([int(d['max_overdue_days']) for d in debts])}]")
        print(f"           past_terms[{dist([int(d['max_days_past_terms']) for d in debts])}]")
        rratios = [
            float(r["elapsed_days"]) / float(r["cycle_days"])
            for r in reorders
            if float(r["cycle_days"]) > 0
        ]
        rclients = len({r['client_id'] for r in reorders})
        cycle_days = [float(r["cycle_days"]) for r in reorders]
        print(f"  REORDER  pairs={len(reorders)} clients={rclients}  cycle_d[{dist(cycle_days)}]")
        print(f"           overdue_ratio[{distf(rratios)}]")
        print(f"  CHURN    clients={len(churn)}  silence_d[{dist([int(c['silence_days']) for c in churn])}]")
        drop_ratios = [float(c["recent_orders"]) / max(1, float(c["prior_orders"])) for c in churn]
        print(f"           drop_ratio[{distf(drop_ratios)}]")
        new_days = [int(c["days_since_created"]) for c in newc]
        print(f"  NEW      clients={len(newc)}  days_since[{dist(new_days)}]")

        try:
            tgt = targets.compute_target(mid, as_of)
            sh, pd = tgt.get("shipped", {}), tgt.get("paid", {})
            print(f"  TARGET   shipped: target€{sh.get('target',0):.0f} mtd€{sh.get('mtd',0):.0f} "
                  f"exp€{sh.get('expected_to_date',0):.0f} gap€{sh.get('gap',0):.0f} "
                  f"pace={sh.get('pace_status','?')}")
            print(f"           paid:    target€{pd.get('target',0):.0f} mtd€{pd.get('mtd',0):.0f} "
                  f"exp€{pd.get('expected_to_date',0):.0f} gap€{pd.get('gap',0):.0f} "
                  f"pace={pd.get('pace_status','?')}")
        except Exception as e:
            print(f"  TARGET   ERROR: {str(e)[:120]}")
        print()


if __name__ == "__main__":
    main()
