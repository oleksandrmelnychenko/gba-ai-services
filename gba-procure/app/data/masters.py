"""Buyer-maintained masters in MongoDB: per-producer profile + per (producer,product) terms.

Optional reads may return empty masters. Plan and reconciliation readers require successful
reads so a store failure cannot remove buyer overrides or procurement constraints.
Collections live in the shared gba-nba Mongo instance, procure_ prefixed.
"""

from __future__ import annotations

from datetime import date

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.readiness import ProcurementBusinessReadinessError

log = get_logger("masters")

_client: MongoClient | None = None


class MastersReadError(ProcurementBusinessReadinessError):
    """Required buyer-maintained inputs could not be read; contains only a safe reason."""


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
        pt.create_index(
            [("producer_id", ASCENDING), ("product_id", ASCENDING)], unique=True, name="uq_producer_product"
        )


def producer_profile(producer_id: int, *, required: bool = False) -> dict | None:
    try:
        coll = _producer_profiles()
        if coll is None:
            if required:
                raise MastersReadError("masters_store_unavailable")
            return None
        doc = coll.find_one({"producer_id": int(producer_id)}, {"_id": 0}, max_time_ms=2000)
        return doc
    except MastersReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        if required:
            raise MastersReadError("masters_profile_read_failed") from exc
        log.warning("producer_profile_read_failed", producer_id=producer_id, error_type=type(exc).__name__)
        return None


def product_terms_for(producer_id: int, product_ids: list[int], *, required: bool = False) -> dict[int, dict]:
    if not product_ids:
        return {}
    try:
        coll = _product_terms()
        if coll is None:
            if required:
                raise MastersReadError("masters_store_unavailable")
            return {}
        cur = coll.find(
            {"producer_id": int(producer_id), "product_id": {"$in": [int(p) for p in product_ids]}},
            {"_id": 0},
            max_time_ms=2000,
        )
        return {int(d["product_id"]): d for d in cur}
    except MastersReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        if required:
            raise MastersReadError("masters_terms_read_failed") from exc
        log.warning("product_terms_read_failed", producer_id=producer_id, error_type=type(exc).__name__)
        return {}


def require_read_access() -> None:
    """Bounded collection permission checks, including valid empty collections.

    This is an early readiness check. Actual plan reads remain required independently;
    an earlier successful probe never authorizes a later failed read.
    """
    for get_collection in (_producer_profiles, _product_terms):
        try:
            coll = get_collection()
            if coll is None:
                raise MastersReadError("masters_store_unavailable")
            coll.find_one({}, {"_id": 1}, max_time_ms=2000)
        except MastersReadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MastersReadError("masters_collections_unavailable") from exc


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
        {"$set": doc},
        upsert=True,
    )
    return doc


def seed_derived_terms(
    min_orders: int = 3,
    overwrite: bool = False,
    as_of: str | None = None,
) -> dict:
    """Seed product_terms from real supply history (MOQ=min observed qty, multiple=PackingStandard).
    Skips buyer-curated rows (source != 'derived') unless overwrite."""
    from app.data import supply_repository as repo

    coll = _product_terms()
    if coll is None:
        raise RuntimeError("masters_store_unavailable")
    terms = repo.derive_moq_terms(as_of or date.today().isoformat(), min_orders)
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


def orphan_report() -> dict[str, dict] | None:
    """Per-collection count of docs keyed on producer/product IDs absent from the source DB.

    The DB periodically re-mints reference rows under new IDs (pre-wipe generations die);
    procure docs carry no natural keys (names/vendor codes), so dead-ID docs cannot be
    remapped to the re-minted rows — they are retained and surfaced via a startup warning.
    """
    from app.data.db import in_clause, query

    client = _get_client()
    if client is None:
        return None
    db = client[get_settings().mongo_db]

    def _alive(table: str, ids: list[int]) -> set[int]:
        alive: set[int] = set()
        for i in range(0, len(ids), 1000):
            chunk = ids[i : i + 1000]
            if not chunk:
                continue
            ph, params = in_clause("p", chunk)
            rows = query(f"SELECT ID FROM dbo.{table} WHERE ID IN {ph}", params)
            alive.update(int(r["ID"]) for r in rows)
        return alive

    out: dict[str, dict] = {}
    for name in ("procure_product_terms", "procure_feedback"):
        coll = db[name]
        producers = [int(p) for p in coll.distinct("producer_id")]
        products = [int(p) for p in coll.distinct("product_id")]
        dead_producers = sorted(set(producers) - _alive("Client", producers))
        dead_products = sorted(set(products) - _alive("Product", products))
        orphaned = 0
        if dead_producers or dead_products:
            orphaned = coll.count_documents(
                {
                    "$or": [
                        {"producer_id": {"$in": dead_producers}},
                        {"product_id": {"$in": dead_products}},
                    ]
                }
            )
        out[name] = {
            "total": coll.count_documents({}),
            "orphaned": orphaned,
            "dead_producer_ids": len(dead_producers),
            "dead_product_ids": len(dead_products),
        }
    return out


def ping() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.admin.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False
