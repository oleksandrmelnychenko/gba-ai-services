"""Redis cache — ONE documented key scheme (fixes the prototype's 3-scheme mess).

Key scheme (single source of truth):
    reco:{model_version}:{customer_id}:{as_of}:{top_n}:{discovery}
The model version embeds the source-history floor, so either model or boundary changes
auto-invalidate old entries.

Graceful degradation: if Redis is down, every call is a no-op miss — the service
still works (just uncached). Never let cache failure break recommendations.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS

log = get_logger("cache")

# v38: v37 source truth plus a hard 2025-01-01 transactional-history floor. Embedding the
# boundary prevents pre-floor cached results from surviving the contract change.
_MODEL_VERSION = "v38-history-floor-20250101-source-detail-202607"
_RETRY_COOLDOWN_S = 30.0
_client: redis.Redis | None = None
_unavailable_until = 0.0


def _get_client() -> redis.Redis | None:
    global _client, _unavailable_until
    if _unavailable_until and time.monotonic() < _unavailable_until:
        return None
    if _client is None:
        s = get_settings()
        try:
            _client = redis.Redis(
                host=s.redis_host, port=s.redis_port, db=s.redis_db,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _client.ping()
            _unavailable_until = 0.0
            log.info("redis_connected", host=s.redis_host, port=s.redis_port)
        except Exception as exc:  # noqa: BLE001
            # Cool-down, not a permanent flag: a Redis blip at startup must not
            # disable caching (and the nightly warm) for the process lifetime.
            log.warning("redis_unavailable", error=str(exc), retry_in_s=_RETRY_COOLDOWN_S)
            _client = None
            _unavailable_until = time.monotonic() + _RETRY_COOLDOWN_S
    return _client


def make_key(customer_id: int, as_of: str, top_n: int, discovery: bool,
             region_scope: bool = False) -> str:
    base = f"reco:{_MODEL_VERSION}:{customer_id}:{as_of}:{top_n}:{int(discovery)}"
    return f"{base}:r" if region_scope else base


def make_copurchase_key(customer_id: int, as_of: str, top_n: int) -> str:
    return f"copurchase:{_MODEL_VERSION}:{customer_id}:{as_of}:{top_n}"


def get(key: str) -> dict[str, Any] | None:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_get_failed", error=str(exc))
        return None
    if raw is None:
        METRICS.record_cache(hit=False)
        return None
    METRICS.record_cache(hit=True)
    return json.loads(raw)


def set(key: str, value: dict[str, Any], ttl: int | None = None) -> None:
    client = _get_client()
    if client is None:
        return
    ttl = ttl or get_settings().cache_ttl
    try:
        client.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_set_failed", error=str(exc))


def invalidate_customer(customer_id: int) -> int:
    client = _get_client()
    if client is None:
        return 0
    patterns = (
        f"reco:{_MODEL_VERSION}:{customer_id}:*",
        f"copurchase:{_MODEL_VERSION}:{customer_id}:*",
    )
    try:
        keys: list[str] = []
        for pattern in patterns:
            keys.extend(client.scan_iter(match=pattern, count=200))
        return client.delete(*keys) if keys else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_invalidate_failed", customer_id=customer_id, error=str(exc))
        return 0


def invalidate_copurchase(customer_id: int) -> int:
    client = _get_client()
    if client is None:
        return 0
    pattern = f"copurchase:{_MODEL_VERSION}:{customer_id}:*"
    try:
        keys = list(client.scan_iter(match=pattern, count=200))
        return client.delete(*keys) if keys else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_invalidate_failed", customer_id=customer_id, error=str(exc))
        return 0


_ACTIVE_CLIENTS_KEY = "reco:worker:active_clients"


def get_active_client_snapshot() -> frozenset[int]:
    """Active-client id set persisted by the last warm run — churn detection baseline."""
    client = _get_client()
    if client is None:
        return frozenset()
    try:
        raw = client.get(_ACTIVE_CLIENTS_KEY)
        return frozenset(int(x) for x in json.loads(raw)) if raw else frozenset()
    except Exception as exc:  # noqa: BLE001
        log.warning("active_snapshot_get_failed", error=str(exc))
        return frozenset()


def set_active_client_snapshot(client_ids: frozenset[int] | set[int]) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.set(_ACTIVE_CLIENTS_KEY, json.dumps(sorted(int(c) for c in client_ids)))
    except Exception as exc:  # noqa: BLE001
        log.warning("active_snapshot_set_failed", error=str(exc))


def _neg_key(net_uid: str) -> str:
    return f"reco:neg:{net_uid.lower()}"


def add_negatives(customer_id: int, product_ids: list[int], ttl: int) -> int:
    """Record products a downstream consumer (e.g. NBA: manager dismissed / sold=False) judged a
    bad recommendation for this customer. Stored as a TTL'd set; the recommender excludes them.

    Keyed by Client.NetUID with Product.VendorCode members — the natural keys survive the
    catalog/client re-syncs that mint new integer ids (a re-mint used to orphan
    reco:neg:{client_id} sets full of dead product ids). The caller still passes live ids;
    translation happens here."""
    from app.data import sales_repository as repo

    client = _get_client()
    if client is None or not product_ids:
        return 0
    try:
        net_uid = repo.client_net_uid(customer_id)
        codes = repo.product_vendor_codes(product_ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_translate_failed", customer_id=customer_id, error=str(exc))
        return 0
    if net_uid is None or not codes:
        log.warning("neg_add_unresolved", customer_id=customer_id,
                    client_found=net_uid is not None, vendor_codes=len(codes))
        return 0
    key = _neg_key(net_uid)
    try:
        n = client.sadd(key, *codes)
        client.expire(key, ttl)
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_add_failed", customer_id=customer_id, error=str(exc))
        return 0

    # journal AFTER the cache write succeeds — the durable copy self-heals Redis on startup
    from app.data import feedback_store
    feedback_store.append(net_uid, list(codes))
    return int(n)


def get_negative_vendor_codes(customer_id: int) -> frozenset[str]:
    """The stored negative set as VendorCodes — one entry per distinct product natural key."""
    from app.data import sales_repository as repo

    client = _get_client()
    if client is None:
        return frozenset()
    try:
        net_uid = repo.client_net_uid(customer_id)
        if net_uid is None:
            return frozenset()
        return frozenset(str(x) for x in client.smembers(_neg_key(net_uid)))
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_get_failed", customer_id=customer_id, error=str(exc))
        return frozenset()


def get_negatives(customer_id: int) -> frozenset[int]:
    """Negative product ids for exclusion — the stored VendorCodes expanded to EVERY catalog
    generation (live and soft-deleted), because candidate pools carry historical ids that only
    collapse onto live rows in live_remap at the end of the pipeline."""
    from app.data import sales_repository as repo

    codes = get_negative_vendor_codes(customer_id)
    if not codes:
        return frozenset()
    try:
        return frozenset(repo.product_ids_for_vendor_codes(sorted(codes)))
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_expand_failed", customer_id=customer_id, error=str(exc))
        return frozenset()


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False
