"""Temporal-holdout evaluation on the snapshot engine.

For each origin date the snapshot is built from confirmed purchases invoiced strictly before it
and every active client's served list is scored against what that client ordered and was
invoiced for in the following window (returns netted; orders already open at the origin are in
nobody's truth, because the service excludes them from the list). Lists and truth are compared
per article, so sibling cards count as one product.
Run:  python -m app.services.eval.holdout --origins 2026-05-13,2026-06-13 --out docs/eval.md
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date

from app.core.config import get_settings
from app.data import cache
from app.data import source_rules as rules
from app.data.db import dispose, query
from app.services.eval import measured
from app.services.recommendations import snapshot, worker

WINDOW_DAYS = 30
SERVED_K = 25
METRICS = (
    "hit@10", "hit@25", "precision@10", "precision@25", "recall@25",
    "new_hit@25", "coverage@25", "revenue_hit@25",
)

VARIANTS: dict[str, dict] = {
    "v40": {},
    "no_jaccard": {"discovery_source": "copurchase"},
    "split_15_10": {"repurchase_count": 15},
    "split_25_0": {"repurchase_count": 25},
    "halflife_14": {"halflife_days": 14},
    "halflife_30": {"halflife_days": 30},
    "halflife_45": {"halflife_days": 45},
    "popular_own_groups": {"popular_scope": "client_groups"},
    "group_cap_5": {"max_per_group": 5},
    "seasonal": {"seasonal_boost": 0.25},
}


def _truth(origin: str) -> dict[int, dict[int, float]]:
    rows = query(
        f"""
        SELECT line.cid AS cid, line.pid AS pid, SUM(line.units * line.price) AS eur
        FROM (
            SELECT ca.ClientID AS cid, oi.ProductID AS pid,
                   {rules.KEPT_UNITS} AS units, oi.PricePerItem AS price
            FROM {rules.SPINE}
            WHERE {rules.PURCHASED} AND {rules.ORDERED_IN_WINDOW}
        ) line
        GROUP BY line.cid, line.pid
        """,
        rules.forward_window(origin, WINDOW_DAYS),
    )
    out: dict[int, dict[int, float]] = defaultdict(dict)
    for row in rows:
        out[int(row["cid"])][int(row["pid"])] = max(float(row["eur"] or 0), 0.0)
    return out


def evaluate_origin(origin: str, variants: dict[str, dict]) -> dict:
    dispose()
    snap = snapshot.load(origin)
    base = snap.params
    raw_truth = _truth(origin)

    def article(pid: int) -> int:
        live = snap.live.get(pid, pid)
        return snap.article.get(live, live)

    active = worker.active_clients(origin, get_settings().active_client_days)
    truth: dict[int, dict[int, float]] = {}
    for cid in active:
        bought: dict[int, float] = defaultdict(float)
        for pid, eur in raw_truth.get(cid, {}).items():
            if pid not in snap.ubiquitous:
                bought[article(pid)] += eur
        if bought:
            truth[cid] = dict(bought)
    contexts = {cid: snap.context(cid, None) for cid in truth}
    new_truth = {
        cid: set(truth[cid]) - {article(pid) for pid in contexts[cid].owned_live} for cid in truth
    }
    new_cases = sum(1 for items in new_truth.values() if items)
    total_eur = sum(sum(items.values()) for items in truth.values())
    result = {"origin": origin, "cases": len(truth), "new_cases": new_cases, "variants": {}}
    for name, overrides in variants.items():
        snap.params = replace(base, **overrides)
        sums: dict[str, float] = defaultdict(float)
        covered: set[int] = set()
        eur_hit = 0.0
        new_hits = 0
        for cid, bought in truth.items():
            recs = [
                article(r.product_id)
                for r in contexts[cid].personal_result(SERVED_K).recommendations
            ]
            covered.update(recs)
            for k in (10, 25):
                hits = len(set(recs[:k]) & bought.keys())
                sums[f"hit@{k}"] += hits > 0
                sums[f"precision@{k}"] += hits / k
            matched = set(recs) & bought.keys()
            sums["recall@25"] += len(matched) / len(bought)
            eur_hit += sum(bought[p] for p in matched)
            new_hits += bool(set(recs) & new_truth[cid])
            cross = [article(pid) for pid, _ in contexts[cid].cross_sell(SERVED_K)]
            for k in (10, 25):
                sums[f"copurchase_precision@{k}"] += len(set(cross[:k]) & bought.keys()) / k
        n = max(len(truth), 1)
        metrics = {key: sums[key] / n for key in ("hit@10", "hit@25", "precision@10",
                                                 "precision@25", "recall@25")}
        for k in (10, 25):
            metrics[f"copurchase_precision@{k}"] = sums[f"copurchase_precision@{k}"] / n
        metrics["new_hit@25"] = new_hits / max(new_cases, 1)
        metrics["coverage@25"] = float(len(covered))
        metrics["revenue_hit@25"] = eur_hit / total_eur if total_eur else 0.0
        result["variants"][name] = metrics
    snap.params = base
    return result


def summarize(results: list[dict], origins: list[str]) -> dict[str, dict[str, tuple[float, float]]]:
    picked = [r for r in results if r["origin"] in origins]
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for name in picked[0]["variants"]:
        out[name] = {}
        for metric in METRICS:
            values = [r["variants"][name][metric] for r in picked]
            out[name][metric] = (
                statistics.mean(values),
                statistics.pstdev(values) if len(values) > 1 else 0.0,
            )
    return out


def _table(summary: dict[str, dict[str, tuple[float, float]]]) -> str:
    lines = ["| variant | " + " | ".join(METRICS) + " |", "|---|" + "---|" * len(METRICS)]
    for name, metrics in summary.items():
        cells = []
        for metric in METRICS:
            mean, spread = metrics[metric]
            cells.append(f"{mean:.0f} ± {spread:.0f}" if metric.startswith("coverage")
                         else f"{mean:.3f} ± {spread:.3f}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def run(origins: list[str], variants: dict[str, dict], processes: int) -> list[dict]:
    with ProcessPoolExecutor(max_workers=processes) as pool:
        return list(pool.map(evaluate_origin, origins, [variants] * len(origins)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origins", required=True)
    parser.add_argument("--confirm", type=int, default=2)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--variants", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument("--write-measured", action="store_true")
    args = parser.parse_args()
    origins = args.origins.split(",")
    variants = VARIANTS if not args.variants else json.loads(args.variants)
    results = run(origins, variants, args.processes)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=1)
    if args.write_measured:
        served = next(iter(variants))

        def mean(metric: str) -> float:
            return round(statistics.mean(r["variants"][served][metric] for r in results), 4)

        measured.write({
            "model_version": cache._MODEL_VERSION,
            "evaluated_on": date.today().isoformat(),
            "protocol": f"temporal holdout, {len(origins)} origins {origins[0]}..{origins[-1]}, "
                        f"{WINDOW_DAYS}-day truth window, served list of {SERVED_K}",
            "cases": sum(r["cases"] for r in results),
            "precision": {
                "personal": {"10": mean("precision@10"), "25": mean("precision@25")},
                "copurchase": {"10": mean("copurchase_precision@10"),
                               "25": mean("copurchase_precision@25")},
            },
            "hit_rate": {"10": mean("hit@10"), "25": mean("hit@25")},
            "revenue_hit": {"25": mean("revenue_hit@25")},
        })
    selection, confirmation = origins[: -args.confirm], origins[-args.confirm:]
    for result in results:
        print(result["origin"], "cases", result["cases"], "new-product cases", result["new_cases"])
    print(f"\nSelection origins {', '.join(selection)}\n")
    print(_table(summarize(results, selection)))
    print(f"\nConfirmation origins {', '.join(confirmation)}\n")
    print(_table(summarize(results, confirmation)))


if __name__ == "__main__":
    main()
