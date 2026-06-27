"""Lens 3 — substitution / analogues: rank interchangeable, currently-sellable replacements.

Given a target product, `substitutes()` pulls its interchangeable candidates from the catalog
(ProductAnalogue + OE-number fallback, see catalog_repository) and ranks them so a sales manager can
offer the best in-stock replacement first. Ranking favours candidates that are actually sellable now:
on-hand stock first (band != order_to_demand OR qty_on_hand>0), then portfolio health, with curated
ProductAnalogue links preferred over OE-only matches as a tie-break.

PURE w.r.t. the portfolio: the caller passes `health_lookup` (portfolio rows indexed by product_id);
this module never imports portfolio.py. Candidates the lookup does not know about (not in the scored
portfolio universe) still rank — below in-stock ones — using only their catalog flags.
"""
from __future__ import annotations

from typing import Any

from app.data import catalog_repository as cat

_SOURCE_RANK = {"analogue": 1, "oe": 0}


def _in_stock(health_row: dict[str, Any] | None, is_for_sale: bool) -> bool:
    """True when the candidate can be supplied from existing stock right now."""
    if health_row is None:
        return False
    qty = float(health_row.get("qty_on_hand") or 0)
    band = health_row.get("band")
    return qty > 0 or (band is not None and band != "order_to_demand")


def _candidate_view(cand: dict[str, Any], health_lookup: dict[int, dict[str, Any]]) -> dict[str, Any]:
    pid = int(cand["product_id"])
    h = health_lookup.get(pid)
    in_stock = _in_stock(h, bool(cand.get("is_for_sale")))
    return {
        "product_id": pid,
        "name": cand.get("name"),
        "vendor_code": cand.get("vendor_code"),
        "oe_number": cand.get("oe_number"),
        "source": cand.get("source"),
        "is_for_sale": bool(cand.get("is_for_sale")),
        "in_stock": in_stock,
        "qty_on_hand": float(h["qty_on_hand"]) if h and h.get("qty_on_hand") is not None else 0.0,
        "band": h.get("band") if h else None,
        "health": float(h["health"]) if h and h.get("health") is not None else None,
        "in_portfolio": h is not None,
    }


def _sort_key(v: dict[str, Any]) -> tuple:
    return (
        1 if v["in_stock"] else 0,
        1 if v["is_for_sale"] else 0,
        v["health"] if v["health"] is not None else -1.0,
        v["qty_on_hand"],
        _SOURCE_RANK.get(v["source"], 0),
        -v["product_id"],
    )


def substitutes(product_id: int, health_lookup: dict[int, dict[str, Any]],
                limit: int | None = None) -> dict[str, Any]:
    """Ranked replacement candidates for `product_id`.

    `health_lookup`: {pid: portfolio_row} (the lead builds this from portfolio rows; rows carry at
    least band / health / qty_on_hand). Pure — no DB beyond catalog_repository, no portfolio import.

    Returns:
      {product_id, found (target exists & sellable-catalog known), target {...}, count,
       in_stock_count, candidates [ranked {...}]}
    The target itself and deleted products are excluded by the repository. Candidates are ranked
    in-stock-first, then sellable, then health, then on-hand qty, then curated-link, then stable id.
    """
    pid = int(product_id)
    card = cat.product_card(pid)
    if card is None:
        return {"product_id": pid, "found": False, "target": None,
                "count": 0, "in_stock_count": 0, "candidates": []}

    raw = cat.analogues_for(pid)
    views = [_candidate_view(c, health_lookup) for c in raw]
    views.sort(key=_sort_key, reverse=True)
    if limit is not None and limit >= 0:
        views = views[:limit]

    return {
        "product_id": pid,
        "found": True,
        "target": {
            "product_id": pid,
            "name": card.get("name"),
            "vendor_code": card.get("vendor_code"),
            "oe_number": card.get("oe_number"),
            "is_for_sale": bool(card.get("is_for_sale")),
            "has_analogue": bool(card.get("has_analogue")),
        },
        "count": len(views),
        "in_stock_count": sum(1 for v in views if v["in_stock"]),
        "candidates": views,
    }
