"""Buyer-maintained masters in MongoDB: per-producer profile + per (producer,product) terms.

Graceful: a missing/unreachable Mongo yields empty masters (no rounding, no override) so the
plan still builds. Collections live in the shared gba-nba Mongo instance, procure_ prefixed.
"""
from __future__ import annotations

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("masters")

_client: MongoClient | None = None


def _get_client() -> MongoClient | None:
    global _client
    s = get_settings()
    if not s.mongo_uri:
        return None
    if _client is None:
        _client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=2000, tz_aware=True)
        log.info("masters_mongo_connected", db=s.mongo_db)
    return _client


def _coll(name: str) -> Collection | None:
    client = _get_client()
    if client is None:
        return None
    return client[get_settings().mongo_db][name]


def _producer_profiles() -> Collection | None:
    return _coll("procure_producer_profile")


def _product_terms() -> Collection | None:
    return _coll("procure_product_terms")


def ensure_indexes() -> None:
    pp = _producer_profiles()
    pt = _product_terms()
    if pp is not None:
        pp.create_index([("producer_id", ASCENDING)], unique=True, name="uq_producer")
    if pt is not None:
        pt.create_index([("producer_id", ASCENDING), ("product_id", ASCENDING)],
                        unique=True, name="uq_producer_product")


def producer_profile(producer_id: int) -> dict | None:
    coll = _producer_profiles()
    if coll is None:
        return None
    try:
        doc = coll.find_one({"producer_id": int(producer_id)}, {"_id": 0})
        return doc
    except Exception as exc:  # noqa: BLE001
        log.warning("producer_profile_read_failed", producer_id=producer_id, error=str(exc))
        return None


def product_terms_for(producer_id: int, product_ids: list[int]) -> dict[int, dict]:
    coll = _product_terms()
    if coll is None or not product_ids:
        return {}
    try:
        cur = coll.find(
            {"producer_id": int(producer_id), "product_id": {"$in": [int(p) for p in product_ids]}},
            {"_id": 0},
        )
        return {int(d["product_id"]): d for d in cur}
    except Exception as exc:  # noqa: BLE001
        log.warning("product_terms_read_failed", producer_id=producer_id, error=str(exc))
        return {}


def list_product_terms(producer_id: int) -> list[dict]:
    coll = _product_terms()
    if coll is None:
        return []
    try:
        return list(coll.find({"producer_id": int(producer_id)}, {"_id": 0}))
    except Exception as exc:  # noqa: BLE001
        log.warning("product_terms_list_failed", producer_id=producer_id, error=str(exc))
        return []


def upsert_producer_profile(producer_id: int, profile: dict) -> dict:
    coll = _producer_profiles()
    if coll is None:
        raise RuntimeError("masters_store_unavailable")
    doc = {"producer_id": int(producer_id), **{k: v for k, v in profile.items() if k != "producer_id"}}
    coll.update_one({"producer_id": int(producer_id)}, {"$set": doc}, upsert=True)
    return doc


def upsert_product_terms(producer_id: int, product_id: int, terms: dict) -> dict:
    coll = _product_terms()
    if coll is None:
        raise RuntimeError("masters_store_unavailable")
    doc = {
        "producer_id": int(producer_id),
        "product_id": int(product_id),
        **{k: v for k, v in terms.items() if k not in ("producer_id", "product_id")},
    }
    coll.update_one(
        {"producer_id": int(producer_id), "product_id": int(product_id)},
        {"$set": doc}, upsert=True,
    )
    return doc


def seed_derived_terms(min_orders: int = 3, overwrite: bool = False) -> dict:
    """Seed product_terms from real supply history (MOQ=min observed qty, multiple=PackingStandard).
    Skips buyer-curated rows (source != 'derived') unless overwrite."""
    from app.data import supply_repository as repo

    coll = _product_terms()
    if coll is None:
        raise RuntimeError("masters_store_unavailable")
    terms = repo.derive_moq_terms(min_orders)
    seeded = skipped = 0
    for t in terms:
        existing = coll.find_one(
            {"producer_id": t["producer_id"], "product_id": t["product_id"]}, {"_id": 0, "source": 1}
        )
        if existing and not overwrite and existing.get("source") != "derived":
            skipped += 1
            continue
        doc = {"moq": t["moq"], "source": "derived", "samples": t["orders"]}
        if t["pack"] and t["pack"] > 1:
            doc["order_multiple"] = t["pack"]
        upsert_product_terms(t["producer_id"], t["product_id"], doc)
        seeded += 1
    log.info("terms_seeded", seeded=seeded, skipped=skipped, candidates=len(terms))
    return {"seeded": seeded, "skipped": skipped, "candidates": len(terms)}


def ping() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.admin.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False
