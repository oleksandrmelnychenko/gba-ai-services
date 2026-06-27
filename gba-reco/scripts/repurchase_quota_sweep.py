"""Repurchase-quota sweep over the leave-last-basket harness.

Sweeps `repurchase_count` over a grid and re-scores the SAME eval cases (built once)
that the committed v3.2 baseline used (synthetic/ubiquitous excluded, k=10). For each
setting reports hit_rate@k, MRR@k, recall@k, precision@k and discovery-share (mean
fraction of the returned top-k that came from the discovery engine).

Pure measurement: mutates only the in-process cached Settings singleton's
`repurchase_count`/`discovery_count` (no .env, no service restart). The recommender reads
`get_settings()` on every call so the override is picked up immediately.
"""
from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.domain.models import RecSource
from app.services.eval import harness


def _score_with_discovery_share(cases, k):
    excl = harness._excluded()
    m = harness.Metrics()
    disc_share_sum = 0.0
    for case in cases:
        result = harness.recommender.recommend(case.customer_id, as_of_date=case.as_of, top_n=k)
        recs_full = result.recommendations[:k]
        recs = [r.product_id for r in recs_full if r.product_id not in excl][:k]
        kept_objs = [r for r in recs_full if r.product_id not in excl][:k]
        n_disc = sum(1 for r in kept_objs if r.source == RecSource.DISCOVERY)
        disc_share_sum += (n_disc / k) if k else 0.0
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
        m.add(result.segment, hit=bool(hit_set))
    return m, (disc_share_sum / m.n if m.n else 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--min-orders", type=int, default=2)
    ap.add_argument("--grid", type=int, nargs="+", default=[20, 16, 12, 10, 8, 6])
    args = ap.parse_args()

    s = get_settings()
    orig_rep, orig_disc = s.repurchase_count, s.discovery_count

    cases = harness.build_cases(min_orders=args.min_orders)
    print(f"built {len(cases)} eval cases (k={args.k}, min_orders={args.min_orders})")
    print(f"{'rep_cnt':>7} {'disc_cnt':>8} {'n':>4} {'hit@k':>7} {'MRR':>7} "
          f"{'recall':>7} {'prec':>7} {'disc_share':>10}")

    rows = []
    try:
        for rep in args.grid:
            s.repurchase_count = rep
            s.discovery_count = max(args.k - min(rep, args.k), 0)
            m, disc_share = _score_with_discovery_share(cases, args.k)
            n = max(m.n, 1)
            row = {
                "repurchase_count": rep,
                "discovery_count": s.discovery_count,
                "n": m.n,
                "hit_rate": m.hits / n,
                "mrr": m.mrr_sum / n,
                "recall": m.recall_sum / n,
                "precision": m.precision_sum / n,
                "discovery_share": disc_share,
            }
            rows.append(row)
            print(f"{rep:>7} {s.discovery_count:>8} {m.n:>4} {row['hit_rate']:>7.3f} "
                  f"{row['mrr']:>7.3f} {row['recall']:>7.3f} {row['precision']:>7.3f} "
                  f"{disc_share:>10.3f}")
    finally:
        s.repurchase_count, s.discovery_count = orig_rep, orig_disc

    baseline_hit = 0.242
    eligible = [r for r in rows if r["hit_rate"] >= baseline_hit]
    winner = max(eligible, key=lambda r: (r["hit_rate"], r["mrr"])) if eligible else None
    print("\n--- verdict ---")
    print(f"baseline hit@{args.k} floor = {baseline_hit}")
    if winner and winner["repurchase_count"] != orig_rep:
        print(f"WINNER: repurchase_count={winner['repurchase_count']} "
              f"hit={winner['hit_rate']:.3f} mrr={winner['mrr']:.3f} (beats/ties baseline)")
    else:
        print(f"HOLD at {orig_rep}: no setting strictly beats baseline hit@{args.k}={baseline_hit}")


if __name__ == "__main__":
    main()
