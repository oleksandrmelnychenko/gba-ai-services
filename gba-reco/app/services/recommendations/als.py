"""Implicit-feedback ALS (Hu-Koren-Volinsky 2008) — matrix factorization recommender.

The strongest classical candidate: learns latent client/product factors from the
implicit client×product purchase matrix. Confidence weighting c = 1 + alpha·count means
more-purchased pairs pull harder; unobserved pairs are weak-negative (preference 0).

Pure numpy (matrix is ~604×6166, 15.7k nnz, 0.4% dense — trivial to factor). No scipy/
implicit dependency, keeping the stack clean. Trained point-in-time (Created < as_of) so
it's eval-safe through the same harness. Model is cached per as_of (training is the cost).

Scoring: score(u, i) = x_u · y_i. Recommend top-N unseen-or-seen by score.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from app.data.db import query
from app.domain.models import ProductRec, RecommendationResult, RecSource

# small, fast defaults; tune on real data later
_FACTORS = 32
_ITERATIONS = 12
_ALPHA = 40.0
_REG = 0.1

# cache trained models per as_of (avoid retraining for every eval case)
_MODEL_CACHE: dict[str, "ALSModel"] = {}


class ALSModel:
    def __init__(self, user_factors, item_factors, user_index, item_index, item_ids, owned):
        self.user_factors = user_factors          # (n_users, f)
        self.item_factors = item_factors          # (n_items, f)
        self.user_index = user_index              # client_id -> row
        self.item_index = item_index              # product_id -> col
        self.item_ids = item_ids                  # col -> product_id
        self.owned = owned                        # client_id -> set(product_id)

    def recommend(self, customer_id: int, top_n: int) -> list[tuple[int, float]]:
        u = self.user_index.get(customer_id)
        if u is None:
            return []
        scores = self.item_factors @ self.user_factors[u]
        # top-N by score (include owned — B2B repurchase is valid; harness measures it)
        top_idx = np.argpartition(-scores, min(top_n, len(scores) - 1))[:top_n]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return [(int(self.item_ids[i]), float(scores[i])) for i in top_idx]


def _load_interactions(as_of: str):
    rows = query(
        """
        SELECT ca.ClientID AS cid, oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS cnt
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY ca.ClientID, oi.ProductID
        """,
        {"asof": as_of},
    )
    return rows


def _train(as_of: str) -> ALSModel:
    rows = _load_interactions(as_of)
    users = sorted({int(r["cid"]) for r in rows})
    items = sorted({int(r["pid"]) for r in rows})
    uidx = {u: i for i, u in enumerate(users)}
    iidx = {p: j for j, p in enumerate(items)}
    n_u, n_i = len(users), len(items)

    # preference P (1 if observed) and confidence C = 1 + alpha*count, as dense (small matrix)
    counts = np.zeros((n_u, n_i), dtype=np.float64)
    owned: dict[int, set[int]] = {}
    for r in rows:
        u, p, c = int(r["cid"]), int(r["pid"]), float(r["cnt"])
        counts[uidx[u], iidx[p]] = c
        owned.setdefault(u, set()).add(p)

    P = (counts > 0).astype(np.float64)
    C = 1.0 + _ALPHA * counts

    rng = np.random.default_rng(42)
    f = _FACTORS
    X = rng.normal(0, 0.01, (n_u, f))   # user factors
    Y = rng.normal(0, 0.01, (n_i, f))   # item factors
    reg = _REG * np.eye(f)

    for _ in range(_ITERATIONS):
        # fix Y, solve X
        YtY = Y.T @ Y
        for u in range(n_u):
            Cu = C[u]                       # (n_i,)
            Cu_minus = Cu - 1.0
            A = YtY + (Y.T * Cu_minus) @ Y + reg
            b = (Y.T * Cu) @ P[u]
            X[u] = np.linalg.solve(A, b)
        # fix X, solve Y
        XtX = X.T @ X
        for i in range(n_i):
            Ci = C[:, i]
            Ci_minus = Ci - 1.0
            A = XtX + (X.T * Ci_minus) @ X + reg
            b = (X.T * Ci) @ P[:, i]
            Y[i] = np.linalg.solve(A, b)

    return ALSModel(X, Y, uidx, iidx, np.array(items), owned)


def get_model(as_of: str) -> ALSModel:
    if as_of not in _MODEL_CACHE:
        _MODEL_CACHE[as_of] = _train(as_of)
    return _MODEL_CACHE[as_of]


def recommend(customer_id: int, as_of_date: str, top_n: int = 25) -> RecommendationResult:
    started = datetime.now()
    model = get_model(as_of_date)
    scored = model.recommend(customer_id, top_n)
    owned = model.owned.get(customer_id, set())
    recs = [
        ProductRec(
            product_id=pid, score=round(score, 4), rank=i + 1, segment="ALS",
            source=RecSource.REPURCHASE if pid in owned else RecSource.DISCOVERY,
        )
        for i, (pid, score) in enumerate(scored)
    ]
    discovery = sum(1 for r in recs if r.source == RecSource.DISCOVERY)
    latency = (datetime.now() - started).total_seconds() * 1000
    return RecommendationResult(
        customer_id=customer_id, recommendations=recs, count=len(recs),
        discovery_count=discovery, segment="ALS",
        latency_ms=round(latency, 2), as_of_date=as_of_date, model_version="als-v1",
    )
