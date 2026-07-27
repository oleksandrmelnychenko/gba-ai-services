"""Daily cache-warm scheduler — the cart plan (and per-producer plans) are warm before the workday.

Modern cron via APScheduler. Jobs:
  * producer_warm (05:00 local) — full per-producer pass into procure:v1:producer:{id}:{as_of}
  * cart_warm     (06:00 local) — cart plan + cart-wide charts into the EXACT keys
                                   /plan/cart and /plan/charts read, so the API serves a
                                   cache hit (<1s) instead of recomputing live.
  * cache_watchdog  — every 10 min verifies today's cart + charts against the current
                      source fingerprint and resumes missing per-producer keys. It stays
                      active after success because cache keys roll over at midnight and
                      source data changes during the day.
All are idempotent (key carries as_of=today); safe to run more than once. Cron jobs carry a
24h misfire grace so multi-hour scheduler stalls still fire the missed daily pass once.

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import cache
from app.services.replenishment import worker

log = get_logger("scheduler")

_MISFIRE_GRACE_S = 86400
_CATCHUP_RETRY_MIN = 10


def _today() -> str:
    tz = ZoneInfo(get_settings().timezone)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _producer_job() -> None:
    try:
        stats = worker.run(warm_cart_key=False)
        log.info("producer_warm_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.error("producer_warm_failed", error=str(exc))


def _cart_job() -> None:
    try:
        stats = worker.warm_cart()
        log.info("cart_warm_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.error("cart_warm_failed", error=str(exc))
    try:
        stats = worker.warm_charts()
        log.info("charts_warm_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.error("charts_warm_failed", error=str(exc))


def _cache_watchdog_job(as_of: str | None = None) -> dict:
    """Continuously prove and repair the current canonical cache generation."""
    as_of = as_of or _today()
    if not cache.health():
        raise RuntimeError("redis_unavailable")

    source = worker.require_source_readiness(as_of, force=True)
    source_fingerprint = source.get("source_fingerprint")
    cart_key = worker.cart_cache_key(as_of)
    cached_cart = cache.get(cart_key)
    if not worker.canonical_cart_payload_is_ready(
        cached_cart,
        source_fingerprint=source_fingerprint,
    ):
        if cached_cart is not None:
            cache.delete(cart_key)
        worker.warm_cart(as_of=as_of, cart_limit=worker.CART_LIMIT)

    charts_key = cache.make_key("charts", f"all:{worker.CHARTS_TOP_N}", as_of)
    cached_charts = cache.get(charts_key)
    if not worker.canonical_charts_payload_is_ready(
        cached_charts,
        source_fingerprint=source_fingerprint,
    ):
        if cached_charts is not None:
            cache.delete(charts_key)
        worker.warm_charts(as_of=as_of, top_n=worker.CHARTS_TOP_N)

    stats = worker.run(as_of=as_of, warm_cart_key=False, skip_existing=True)
    if stats["failed"]:
        raise RuntimeError(f"producers_failed:{stats['failed']}")
    result = {
        "as_of": as_of,
        "cart_key": cart_key,
        "charts_key": charts_key,
        "producers_warmed": stats["ok"],
        "producers_skipped": stats["skipped"],
    }
    log.info("cache_watchdog_done", **result)
    return result


def _safe_cache_watchdog_job() -> None:
    try:
        _cache_watchdog_job()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "cache_watchdog_failed",
            error=str(exc),
            retry_in_min=_CATCHUP_RETRY_MIN,
        )


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", producer_hour=s.producer_warm_hour,
             cart_hour=s.cart_warm_hour, tz=s.timezone)

    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(
        _safe_cache_watchdog_job,
        IntervalTrigger(minutes=_CATCHUP_RETRY_MIN, timezone=tz),
        id="cache_watchdog",
        next_run_time=datetime.now(tz),
        misfire_grace_time=None,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _producer_job,
        CronTrigger(hour=s.producer_warm_hour, minute=0, timezone=tz),
        id="producer_warm",
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_S,
        coalesce=True,
    )
    scheduler.add_job(
        _cart_job,
        CronTrigger(hour=s.cart_warm_hour, minute=0, timezone=tz),
        id="cart_warm",
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_S,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")


if __name__ == "__main__":
    main()
