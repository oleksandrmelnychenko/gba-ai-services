"""Naive baselines — the floor that V3.2 must beat to justify itself.

The harness audit demanded this: if V3.2 ≈ a trivial baseline, the algorithm adds little.
Each baseline returns a ranked list of product_ids for a customer at as_of, using the
SAME point-in-time discipline as the real recommender (Created < :asof).

Baselines:
- most_frequent_for_client: the client's own most-frequently-bought products (strong
  repurchase floor — the hardest naive baseline to beat in B2B).
- global_popular: globally most-ordered products (cold-start floor).
"""
from __future__ import annotations

from app.data.db import query


def most_frequent_for_client(customer_id: int, as_of: str, top_n: int) -> list[int]:
    rows = query(
        """
        SELECT TOP (:k) oi.ProductID AS pid, COUNT(*) AS c
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID = :cid AND oi.IsValidForCurrentSale = 1
              AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY oi.ProductID
        ORDER BY c DESC
        """,
        {"cid": customer_id, "asof": as_of, "k": top_n},
    )
    return [int(r["pid"]) for r in rows]


def global_popular(as_of: str, top_n: int, exclude: frozenset[int] | None = None) -> list[int]:
    """Globally most-ordered valid products before `as_of`. `exclude` drops ubiquitous/synthetic
    lines; over-fetch so the post-filter still returns up to `top_n`."""
    excl = exclude or frozenset()
    rows = query(
        """
        SELECT TOP (:k) oi.ProductID AS pid, COUNT(*) AS c
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY oi.ProductID
        ORDER BY c DESC
        """,
        {"asof": as_of, "k": top_n + len(excl)},
    )
    return [int(r["pid"]) for r in rows if int(r["pid"]) not in excl][:top_n]
