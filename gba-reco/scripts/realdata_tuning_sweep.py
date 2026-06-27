"""Real-data tuning sweep over the leave-last-basket harness.

Builds eval cases ONCE, then re-scores the SAME cases under different in-process knob
overrides (segment freq/recency weights, recency half-life, group-diversity cap). Pure
measurement — mutates only in-process module globals / the cached Settings singleton, no
.env, no restart. Mirrors scripts/repurchase_quota_sweep.py.

NOTE: the recency-scale correctness fix (#1) is already applied in recommender.py, so every
arm here is measured on top of it. The PRE-fix baseline is the committed honest baseline run
separately via `python -m app.services.eval.harness --k 10`.
"""
from __future__ import annotations

import argparse
import copy

from app.core.config import get_settings
from app.domain.models import Segment
from app.services.eval import harness
from app.services.recommendations import recommender

rec_mod = recommender


def _metrics_dict(m: harness.Metrics, k: int) -> dict:
    n = max(m.n, 1)
    seg = {seg: (h / tot if tot else 0.0, tot) for seg, (h, tot) in m.by_segment.items()}
    return {
        "n": m.n,
        "hit_rate": m.hits / n,
        "precision": m.precision_sum / n,
        "recall": m.recall_sum / n,
        "mrr": m.mrr_sum / n,
        "by_segment": seg,
    }


def _print_row(label: str, d: dict, k: int) -> None:
    print(f"\n### {label}")
    print(f"  n={d['n']} hit@{k}={d['hit_rate']:.4f} prec={d['precision']:.4f} "
          f"recall={d['recall']:.4f} MRR={d['mrr']:.4f}")
    for seg in ("HEAVY", "LIGHT", "REGULAR_CONSISTENT", "REGULAR_EXPLORATORY"):
        if seg in d["by_segment"]:
            hr, tot = d["by_segment"][seg]
            print(f"    {seg:22} n={tot:4} hit_rate={hr:.4f}")


def score(cases, k: int, label: str = "") -> harness.Metrics:
    """Re-implements harness._score with a streaming heartbeat so a long silent pass
    (~2.5 min over ~500 cases) never trips a no-output watchdog."""
    import sys

    m = harness.Metrics()
    excl = harness._excluded()
    total = len(cases)
    for idx, case in enumerate(cases, 1):
        recs, segment = harness._v32_recs(case.customer_id, case.as_of, k)
        recs = [r for r in recs if r not in excl][:k]
        m.n += 1
        hit_set = set(recs) & case.truth
        if hit_set:
            m.hits += 1
        m.precision_sum += len(hit_set) / k
        m.recall_sum += len(hit_set) / len(case.truth)
        rr = 0.0
        for i, pid in enumerate(recs):
            if pid in case.truth:
                rr = 1.0 / (i + 1)
                break
        m.mrr_sum += rr
        m.add(segment, hit=bool(hit_set))
        if idx % 50 == 0 or idx == total:
            print(f"    .. {label} {idx}/{total}", flush=True)
            sys.stdout.flush()
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--min-orders", type=int, default=2)
    ap.add_argument("--phase", default="all",
                    help="weights | halflife | groupcap | all")
    args = ap.parse_args()
    k = args.k

    s = get_settings()
    orig_weights = copy.deepcopy(rec_mod._WEIGHTS)
    orig_halflife = rec_mod._RECENCY_HALFLIFE_DAYS
    orig_max_group = s.max_per_group

    cases = harness.build_cases(min_orders=args.min_orders)
    print(f"built {len(cases)} eval cases (k={k}, min_orders={args.min_orders})")

    # Hold the group cap at its committed default (3) for the weights & half-life phases so
    # those knobs are measured independently of the group-cap sweep (its own phase below).
    s.max_per_group = 3

    # ---- baseline (recency-norm fix ON, committed weights/halflife, caps 3/3) ----
    base = _metrics_dict(score(cases, k, "baseline"), k)
    _print_row("BASELINE (post recency-fix, committed weights/halflife, caps 3/3)", base, k)

    try:
        if args.phase in ("weights", "all"):
            print("\n========== PHASE: per-segment freq/recency weights ==========")
            # Candidate arms per segment. Each entry: list of (w_freq, w_rec) to try.
            seg_grid = {
                Segment.LIGHT: [(0.70, 0.30), (0.50, 0.50), (0.30, 0.70), (0.25, 0.75)],
                Segment.REGULAR_CONSISTENT: [(0.50, 0.35), (0.40, 0.60), (0.30, 0.70), (0.25, 0.75)],
                Segment.REGULAR_EXPLORATORY: [(0.25, 0.50), (0.30, 0.70)],
                Segment.HEAVY: [(0.60, 0.25), (0.40, 0.60)],
            }
            for seg, arms in seg_grid.items():
                for arm in arms:
                    rec_mod._WEIGHTS = copy.deepcopy(orig_weights)
                    rec_mod._WEIGHTS[seg] = arm
                    d = _metrics_dict(score(cases, k, f"{seg.value}{arm}"), k)
                    hr, tot = d["by_segment"].get(seg.value, (0.0, 0))
                    print(f"  {seg.value:22} {str(arm):14} -> seg_hit={hr:.4f} (n={tot}) "
                          f"| overall hit={d['hit_rate']:.4f} recall={d['recall']:.4f} MRR={d['mrr']:.4f}")
            rec_mod._WEIGHTS = copy.deepcopy(orig_weights)

        # Empirically-chosen per-segment winners from the weights phase. Applied for the
        # combined check and as the base config for half-life / group-cap so those knobs are
        # measured against the real shipping weights, not the old committed ones.
        chosen = {
            Segment.LIGHT: (0.30, 0.70),
            Segment.REGULAR_CONSISTENT: (0.40, 0.60),
            Segment.REGULAR_EXPLORATORY: (0.30, 0.70),
            Segment.HEAVY: (0.40, 0.60),
        }

        if args.phase in ("combined", "halflife", "groupcap", "all", "tune"):
            rec_mod._WEIGHTS = copy.deepcopy(orig_weights)
            rec_mod._WEIGHTS.update(chosen)
            d = _metrics_dict(score(cases, k, "combined"), k)
            _print_row("COMBINED chosen per-segment weights (caps 3/3, halflife 90)", d, k)

        if args.phase in ("halflife", "all", "tune"):
            print("\n========== PHASE: recency half-life (on combined weights) ==========")
            for hl in (21, 30, 45, 60, 90):
                rec_mod._RECENCY_HALFLIFE_DAYS = hl
                d = _metrics_dict(score(cases, k, f"hl{hl}"), k)
                print(f"  halflife={hl:3} -> hit@{k}={d['hit_rate']:.4f} recall={d['recall']:.4f} "
                      f"MRR={d['mrr']:.4f} prec={d['precision']:.4f}")
            rec_mod._RECENCY_HALFLIFE_DAYS = orig_halflife

        if args.phase in ("groupcap", "all", "tune"):
            print("\n========== PHASE: group-diversity cap (shared max_per_group) ==========")
            # The committed model has a single shared max_per_group (decoupling was measured
            # but NOT shipped: cap=5 helps recall@20 yet regresses recall@10). Sweep the real
            # knob at k and at k=20 (recall@20 guard) to keep the not-shipped call reproducible.
            for kk in (k, 20):
                for cap in (3, 5):
                    s.max_per_group = cap
                    d = _metrics_dict(score(cases, kk, f"cap{cap}k{kk}"), kk)
                    print(f"  k={kk:2} max_per_group={cap} -> hit@{kk}={d['hit_rate']:.4f} "
                          f"recall@{kk}={d['recall']:.4f} MRR={d['mrr']:.4f} prec={d['precision']:.4f}")
            s.max_per_group = orig_max_group
    finally:
        rec_mod._WEIGHTS = orig_weights
        rec_mod._RECENCY_HALFLIFE_DAYS = orig_halflife
        s.max_per_group = orig_max_group


if __name__ == "__main__":
    main()
