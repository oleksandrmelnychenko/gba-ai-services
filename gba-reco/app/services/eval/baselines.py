"""Naive baselines — the floor that V3.2 must beat to justify itself.

The harness audit demanded this: if V3.2 ≈ a trivial baseline, the algorithm adds little.
Each baseline returns a ranked list of product_ids for a customer at as_of, using the
SAME point-in-time discipline as the real recommender (source_rules.window).

Baselines:
- most_frequent_for_client: the client's own most-frequently-bought products (strong
  repurchase floor — the hardest naive baseline to beat in B2B).
- global_popular: globally most-ordered products (cold-start floor).
"""
from __future__ import annotations

from app.core.history import require_supported_as_of
from app.data import source_rules as rules
from app.data.db import query


def most_frequent_for_client(customer_id: int, as_of: str, top_n: int) -> list[int]:
    require_supported_as_of(as_of)
    rows = query(
        f"""
        SELECT TOP (:k) oi.ProductID AS pid, COUNT(*) AS c
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid
              AND {rules.PURCHASED}
        GROUP BY oi.ProductID
        ORDER BY c DESC, oi.ProductID ASC
        """,
        {
            "cid": customer_id,
            "k": top_n,
            **rules.window(as_of),
        },
    )
    return [int(r["pid"]) for r in rows]


def global_popular(as_of: str, top_n: int, exclude: frozenset[int] | None = None) -> list[int]:
    """Globally most-ordered valid products before `as_of`. `exclude` drops ubiquitous/synthetic
    lines; over-fetch so the post-filter still returns up to `top_n`."""
    require_supported_as_of(as_of)
    excl = exclude or frozenset()
    rows = query(
        f"""
        SELECT TOP (:k) oi.ProductID AS pid, COUNT(*) AS c
        FROM {rules.SPINE}
        WHERE {rules.PURCHASED}
        GROUP BY oi.ProductID
        ORDER BY c DESC, oi.ProductID ASC
        """,
        {
            "k": top_n + len(excl),
            **rules.window(as_of),
        },
    )
    return [int(r["pid"]) for r in rows if int(r["pid"]) not in excl][:top_n]
