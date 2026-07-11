"""Learned re-ranker over the V3.2 candidate list — numpy logistic regression.

The recommender's headroom is in ORDERING, not candidate generation: V3.2 already
surfaces the right products (hit@10 0.23) but ranks them heuristically. The re-ranker
learns from history which candidate actually gets bought and reorders the over-fetched
top-M accordingly.

Design (leakage-safe, mirrors the eval harness):
- TRAIN cases hold out each client's SECOND-TO-LAST order (rn=2): as_of = that order's
  timestamp, label = candidate is in that order. EVAL cases (the harness) hold out the
  LAST order — strictly later, so training never sees an eval label.
- Candidates = recommender.recommend(top_n=CANDIDATE_M) at the case's as_of.
- Features are point-in-time (all history strictly before as_of).
- Model: standardized logistic regression with L2 and class weighting, pure numpy —
  same zero-dependency philosophy as als.py. Weights persist to a JSON artifact.

Ship rule (same as every recommender change): the re-ranked V3.2 must beat plain V3.2
on the untouched harness (hit@10 / MRR) or it does not ship.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from app.core.logging import get_logger
from app.data.db import in_clause, query
from app.services.recommendations import recommender

log = get_logger("reranker")

CANDIDATE_M = 50
_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "reranker_v1.json"

FEATURES = [
    "v32_score_norm",
    "v32_inv_rank",
    "is_repurchase",
    "log_bought_count",
    "recency_score",
    "pop_pct",
    "price_fit",
]


# ----------------------------------------------------------------- train cases

def build_train_cases(min_orders: int = 3, limit: int | None = None) -> list[dict]:
    """One case per eligible client: hold out the SECOND-TO-LAST valid order."""
    lim = "" if limit is None else f"TOP ({limit})"
    rows = query(
        f"""
        WITH client_orders AS (
            SELECT ca.ClientID AS cid, o.ID AS order_id, o.Created AS dt,
                   ROW_NUMBER() OVER (PARTITION BY ca.ClientID ORDER BY o.Created DESC, o.ID DESC) AS rn
            FROM dbo.[Order] o
            JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
            WHERE EXISTS (
                SELECT 1 FROM dbo.OrderItem oi
                WHERE oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            )
        ),
        counts AS (
            SELECT cid, COUNT(*) AS norders FROM client_orders GROUP BY cid
        )
        SELECT {lim} co.cid AS cid, co.order_id AS order_id, co.dt AS dt
        FROM client_orders co
        JOIN counts c ON c.cid = co.cid
        WHERE co.rn = 2 AND c.norders >= :minord
        ORDER BY co.cid
        """,
        {"minord": min_orders},
    )
    cases = []
    for row in rows:
        truth_rows = query(
            """
            SELECT DISTINCT oi.ProductID AS pid
            FROM dbo.OrderItem oi
            WHERE oi.OrderID = :oid AND oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
            """,
            {"oid": int(row["order_id"])},
        )
        truth = {int(r["pid"]) for r in truth_rows}
        if not truth:
            continue
        dt = row["dt"]
        as_of = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt)
        cases.append({"customer_id": int(row["cid"]), "as_of": as_of, "truth": truth})
    return cases


# ------------------------------------------------------------------- features

def _case_features(customer_id: int, as_of: str, candidates: list) -> np.ndarray:
    """Feature matrix (len(candidates) × len(FEATURES)), all history strictly < as_of."""
    pids = [rec.product_id for rec in candidates]
    ph, pparams = in_clause("p", pids)

    hist = {
        int(r["pid"]): (int(r["cnt"]), r["last_dt"])
        for r in query(
            f"""
            SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS cnt, MAX(o.Created) AS last_dt
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
            WHERE ca.ClientID = :cid AND o.Created < :asof AND oi.ProductID IN {ph}
            GROUP BY oi.ProductID
            """,
            {"cid": customer_id, "asof": as_of, **pparams},
        )
    }

    pop = {
        int(r["pid"]): int(r["cnt"])
        for r in query(
            f"""
            SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS cnt
            FROM dbo.[Order] o
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IN {ph}
                  AND o.Created < :asof AND o.Created >= DATEADD(day, -90, :asof)
            GROUP BY oi.ProductID
            """,
            {"asof": as_of, **pparams},
        )
    }

    price_rows = query(
        f"""
        SELECT oi.ProductID AS pid, AVG(oi.PricePerItem) AS avg_price
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.ProductID IN {ph} AND o.Created < :asof AND oi.PricePerItem > 0
        GROUP BY oi.ProductID
        """,
        {"asof": as_of, **pparams},
    )
    prices = {int(r["pid"]): float(r["avg_price"] or 0) for r in price_rows}

    med_rows = query(
        """
        SELECT AVG(oi.PricePerItem) AS med
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
        WHERE ca.ClientID = :cid AND o.Created < :asof AND oi.PricePerItem > 0
        """,
        {"cid": customer_id, "asof": as_of},
    )
    client_price = float(med_rows[0]["med"] or 0) if med_rows else 0.0

    asof_dt = datetime.fromisoformat(as_of)
    top_score = max((rec.score for rec in candidates), default=1.0) or 1.0
    pop_values = sorted(pop.values()) or [0]

    matrix = np.zeros((len(candidates), len(FEATURES)))
    for i, rec in enumerate(candidates):
        pid = rec.product_id
        cnt, last_dt = hist.get(pid, (0, None))
        days_since = (asof_dt - last_dt).days if last_dt is not None else None
        pop_cnt = pop.get(pid, 0)
        pop_pct = sum(1 for v in pop_values if v <= pop_cnt) / len(pop_values)
        price_p = prices.get(pid, 0.0)
        price_fit = (
            abs(math.log1p(price_p) - math.log1p(client_price))
            if price_p > 0 and client_price > 0 else 2.0
        )
        matrix[i] = [
            rec.score / top_score,
            1.0 / rec.rank,
            1.0 if str(rec.source).endswith("REPURCHASE") or "repurchase" in str(rec.source).lower() else 0.0,
            math.log1p(cnt),
            math.exp(-days_since / 90.0) if days_since is not None else 0.0,
            pop_pct,
            price_fit,
        ]
    return matrix


# ---------------------------------------------------------------------- model

def _fit_logreg(x: np.ndarray, y: np.ndarray, l2: float = 1e-2, iters: int = 800, lr: float = 0.3):
    mean, std = x.mean(axis=0), x.std(axis=0) + 1e-9
    xs = (x - mean) / std
    n, d = xs.shape
    w = np.zeros(d)
    b = 0.0
    pos_weight = (len(y) - y.sum()) / max(y.sum(), 1.0)
    sample_w = np.where(y == 1, pos_weight, 1.0)
    sample_w /= sample_w.mean()
    for _ in range(iters):
        z = xs @ w + b
        p = 1.0 / (1.0 + np.exp(-z))
        grad_z = sample_w * (p - y) / n
        w -= lr * (xs.T @ grad_z + l2 * w)
        b -= lr * grad_z.sum()
    return {"w": w.tolist(), "b": b, "mean": mean.tolist(), "std": std.tolist()}


def _predict(model: dict, x: np.ndarray) -> np.ndarray:
    xs = (x - np.array(model["mean"])) / np.array(model["std"])
    return xs @ np.array(model["w"]) + model["b"]


def load_model() -> dict | None:
    if not _ARTIFACT.exists():
        return None
    return json.loads(_ARTIFACT.read_text())


def rerank(customer_id: int, as_of: str, k: int, model: dict) -> list[int]:
    result = recommender.recommend(customer_id, as_of_date=as_of, top_n=CANDIDATE_M)
    candidates = result.recommendations
    if not candidates:
        return []
    feats = _case_features(customer_id, as_of, candidates)
    scores = _predict(model, feats)
    order = np.argsort(-scores)
    return [candidates[i].product_id for i in order[:k]]


# ------------------------------------------------------------------ train/eval

def train(min_orders: int = 3, limit: int | None = None) -> dict:
    cases = build_train_cases(min_orders=min_orders, limit=limit)
    log.info("reranker_train_cases", n=len(cases))
    xs, ys = [], []
    for idx, case in enumerate(cases):
        result = recommender.recommend(case["customer_id"], as_of_date=case["as_of"], top_n=CANDIDATE_M)
        candidates = result.recommendations
        if not candidates:
            continue
        feats = _case_features(case["customer_id"], case["as_of"], candidates)
        labels = np.array([1.0 if rec.product_id in case["truth"] else 0.0 for rec in candidates])
        xs.append(feats)
        ys.append(labels)
        if (idx + 1) % 25 == 0:
            log.info("reranker_train_progress", done=idx + 1, total=len(cases))
    x = np.vstack(xs)
    y = np.concatenate(ys)
    log.info("reranker_dataset", rows=len(y), positives=int(y.sum()))
    model = _fit_logreg(x, y)
    model["features"] = FEATURES
    model["trained_at"] = datetime.now().isoformat()
    model["rows"] = len(y)
    model["positives"] = int(y.sum())
    _ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    _ARTIFACT.write_text(json.dumps(model, indent=1))
    log.info("reranker_saved", path=str(_ARTIFACT),
             weights={f: round(w, 4) for f, w in zip(FEATURES, model["w"], strict=True)})
    return model


def evaluate(k: int = 10, min_orders: int = 2, limit: int | None = None) -> None:
    from app.services.eval.harness import _score, build_cases

    model = load_model()
    if model is None:
        raise SystemExit("no artifact — run --train first")
    cases = build_cases(min_orders=min_orders, limit=limit)

    def v32_fn(cid, as_of, kk):
        res = recommender.recommend(cid, as_of_date=as_of, top_n=kk)
        return [r.product_id for r in res.recommendations], res.segment

    def rerank_fn(cid, as_of, kk):
        res = recommender.recommend(cid, as_of_date=as_of, top_n=CANDIDATE_M)
        candidates = res.recommendations
        if not candidates:
            return [], res.segment
        feats = _case_features(cid, as_of, candidates)
        scores = _predict(model, feats)
        order = np.argsort(-scores)
        return [candidates[i].product_id for i in order[:kk]], res.segment

    results = {"v3.2": _score(cases, v32_fn, k), "v3.2+reranker": _score(cases, rerank_fn, k)}
    print(f"=== reranker A/B (k={k}, n={results['v3.2'].n}) ===")
    print(f"{'model':16} {'hit_rate':>9} {'recall':>8} {'precision':>10} {'MRR':>7}")
    for name, m in results.items():
        n = max(m.n, 1)
        print(f"{name:16} {m.hits / n:>9.3f} {m.recall_sum / n:>8.3f} "
              f"{m.precision_sum / n:>10.3f} {m.mrr_sum / n:>7.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if args.train:
        train(limit=args.limit)
    if args.eval:
        evaluate(k=args.k, limit=args.limit)


if __name__ == "__main__":
    main()
