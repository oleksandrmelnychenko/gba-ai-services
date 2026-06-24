"""Redis cache — ONE documented key scheme (fixes the prototype's 3-scheme mess).

Key scheme (single source of truth):
    reco:v1:{customer_id}:{as_of}:{top_n}:{discovery}
The model version is embedded so a model bump auto-invalidates old entries.

Graceful degradation: if Redis is down, every call is a no-op miss — the service
still works (just uncached). Never let cache failure break recommendations.
"""
from __future__ import annotations

import json
from typing import Any

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS

log = get_logger("cache")

_MODEL_VERSION = "v33-realdata-202606"
_client: redis.Redis | None = None
_unavailable = False


def _get_client() -> redis.Redis | None:
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is None:
        s = get_settings()
        try:
            _client = redis.Redis(
                host=s.redis_host, port=s.redis_port, db=s.redis_db,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _client.ping()
            log.info("redis_connected", host=s.redis_host, port=s.redis_port)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_unavailable", error=str(exc))
            _client = None
            _unavailable = True
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
    pattern = f"reco:{_MODEL_VERSION}:{customer_id}:*"
    keys = list(client.scan_iter(match=pattern, count=200))
    return client.delete(*keys) if keys else 0


def invalidate_copurchase(customer_id: int) -> int:
    client = _get_client()
    if client is None:
        return 0
    pattern = f"copurchase:{_MODEL_VERSION}:{customer_id}:*"
    keys = list(client.scan_iter(match=pattern, count=200))
    return client.delete(*keys) if keys else 0


def _neg_key(customer_id: int) -> str:
    return f"reco:neg:{customer_id}"


def add_negatives(customer_id: int, product_ids: list[int], ttl: int) -> int:
    """Record products a downstream consumer (e.g. NBA: manager dismissed / sold=False) judged a
    bad recommendation for this customer. Stored as a TTL'd set; the recommender excludes them."""
    client = _get_client()
    if client is None or not product_ids:
        return 0
    key = _neg_key(customer_id)
    try:
        n = client.sadd(key, *[int(p) for p in product_ids])
        client.expire(key, ttl)
        return int(n)
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_add_failed", customer_id=customer_id, error=str(exc))
        return 0


def get_negatives(customer_id: int) -> frozenset[int]:
    client = _get_client()
    if client is None:
        return frozenset()
    try:
        return frozenset(int(x) for x in client.smembers(_neg_key(customer_id)))
    except Exception as exc:  # noqa: BLE001
        log.warning("neg_get_failed", customer_id=customer_id, error=str(exc))
        return frozenset()


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False
