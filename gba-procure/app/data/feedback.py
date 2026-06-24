"""Buyer feedback capture + learned per-(producer,ABC) order-quantity bias.

Append-only feedback (accept / edit / dismiss with suggested vs final qty) in MongoDB;
a learned override_factor = median(final/suggested) per (producer, abc) nudges future
suggestions toward how the buyer actually orders. Graceful: no Mongo -> no learning.
"""
from __future__ import annotations

from statistics import median

from pymongo import ASCENDING
from pymongo.collection import Collection

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import masters

log = get_logger("feedback")

_VALID_ACTIONS = {"accept", "edit", "dismiss"}


def _feedback() -> Collection | None:
    client = masters._get_client()
    if client is None:
        return None
    return client[get_settings().mongo_db]["procure_feedback"]


def ensure_indexes() -> None:
    coll = _feedback()
    if coll is not None:
        coll.create_index([("producer_id", ASCENDING), ("at", ASCENDING)], name="ix_producer_at")


def record(producer_id: int, product_id: int, suggested_qty: float, final_qty: float,
           action: str, abc: str | None, at: str) -> dict:
    coll = _feedback()
    if coll is None:
        raise RuntimeError("feedback_store_unavailable")
    if action not in _VALID_ACTIONS:
        raise ValueError("invalid_action")
    doc = {
        "producer_id": int(producer_id),
        "product_id": int(product_id),
        "suggested_qty": float(suggested_qty),
        "final_qty": float(final_qty),
        "action": action,
        "abc": abc,
        "at": at,
    }
    coll.insert_one(dict(doc))
    return doc


def learned_factors(producer_id: int, min_samples: int, lo: float, hi: float) -> dict[str, float]:
    """Per-ABC override factor = clamp(median(final/suggested)) over recent accept/edit/dismiss."""
    coll = _feedback()
    if coll is None:
        return {}
    try:
        ratios: dict[str, list[float]] = {}
        for d in coll.find({"producer_id": int(producer_id)}, {"_id": 0}):
            sq = float(d.get("suggested_qty") or 0)
            if sq <= 0:
                continue
            abc = d.get("abc") or "C"
            ratios.setdefault(abc, []).append(max(0.0, float(d.get("final_qty") or 0)) / sq)
        out: dict[str, float] = {}
        for abc, vals in ratios.items():
            if len(vals) >= min_samples:
                out[abc] = min(max(median(vals), lo), hi)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("learned_factors_failed", producer_id=producer_id, error=str(exc))
        return {}
