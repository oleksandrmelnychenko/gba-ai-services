"""Cached global ABC classification (daily) + per-product XYZ helper."""
from __future__ import annotations

from app.core.logging import get_logger
from app.data import cache
from app.data import supply_repository as repo
from app.services.classify import segmentation

log = get_logger("classify")


def get_abc_map(as_of: str, history_days: int) -> dict[int, str]:
    """Global product -> ABC class by trailing revenue, cached daily (Redis)."""
    key = cache.make_key("abc", history_days, as_of)
    cached = cache.get(key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    revenue = repo.all_products_revenue_eur(as_of, history_days)
    abc = segmentation.abc_from_revenue(revenue)
    cache.set(key, {str(k): v for k, v in abc.items()}, ttl=86400)
    log.info("abc_classified", products=len(abc), as_of=as_of)
    return abc
