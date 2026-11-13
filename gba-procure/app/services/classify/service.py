"""Cached global ABC classification + per-product XYZ helper.

The ABC map is kept for a day but belongs to one source state: once the readiness
snapshot reports a different source fingerprint, the classes are read again so that a
plan never mixes fresh demand with classes ranked on older sales.
"""
from __future__ import annotations

from app.core.history import rolling_coverage
from app.core.logging import get_logger
from app.data import cache
from app.data import supply_repository as repo
from app.services.classify import segmentation

log = get_logger("classify")


def _source_epoch(as_of: str) -> str | None:
    snapshot = cache.get(cache.make_key("readiness", "source", as_of))
    if isinstance(snapshot, dict) and snapshot.get("as_of") == as_of:
        return str(snapshot.get("source_fingerprint") or "") or None
    return None


def get_abc_map(as_of: str, history_days: int) -> dict[int, str]:
    """Global product -> ABC class by trailing revenue for the current source state."""
    coverage = rolling_coverage(as_of, history_days)
    key = cache.make_key(
        "abc",
        f"{history_days}:effective:{coverage.effective_history_days}",
        as_of,
    )
    epoch = _source_epoch(as_of)
    cached = cache.get(key)
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("classes"), dict)
        and cached.get("epoch") == epoch
    ):
        return {int(k): v for k, v in cached["classes"].items()}
    revenue = repo.all_products_revenue_eur(as_of, history_days)
    abc = segmentation.abc_from_revenue(revenue)
    cache.set(key, {"epoch": epoch, "classes": {str(k): v for k, v in abc.items()}}, ttl=86400)
    log.info("abc_classified", products=len(abc), as_of=as_of, source_epoch=epoch)
    return abc
