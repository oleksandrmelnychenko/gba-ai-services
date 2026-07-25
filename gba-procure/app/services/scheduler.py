"""Daily cache-warm scheduler — the cart plan (and per-producer plans) are warm before the workday.

Modern cron via APScheduler. Jobs:
  * producer_warm (05:00 local) — full per-producer pass into procure:v1:producer:{id}:{as_of}
  * cart_warm     (06:00 local) — cart plan + cart-wide charts into the EXACT keys
                                   /plan/cart and /plan/charts read, so the API serves a
                                   cache hit (<1s) instead of recomputing live.
  * startup_catchup — retries every 10 min until today's cart + charts + per-producer keys
                      are warm, then removes itself; the producer pass skips already-warm
                      keys so a partial pass resumes instead of restarting; a boot-time
                      DB/Redis outage (or a missed producer cron) self-heals instead of
                      leaving the cache cold until the next cron fire.
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


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", producer_hour=s.producer_warm_hour,
             cart_hour=s.cart_warm_hour, tz=s.timezone)

    scheduler = BlockingScheduler(timezone=tz)

    def _startup_catchup() -> None:
        as_of = datetime.now().strftime("%Y-%m-%d")
        try:
            if not cache.health():
                raise RuntimeError("redis_unavailable")
            if cache.get(worker.cart_cache_key(as_of)) is None:
                worker.warm_cart(as_of=as_of)
            if cache.get(cache.make_key("charts", f"all:{worker.CHARTS_TOP_N}", as_of)) is None:
                worker.warm_charts(as_of=as_of)
            stats = worker.run(as_of=as_of, warm_cart_key=False, skip_existing=True)
            if stats["failed"]:
                raise RuntimeError(f"producers_failed:{stats['failed']}")
        except Exception as exc:  # noqa: BLE001
            log.error("startup_catchup_failed", error=str(exc),
                      retry_in_min=_CATCHUP_RETRY_MIN)
            return
        scheduler.remove_job("startup_catchup")
        log.info("startup_catchup_done", as_of=as_of, producers_warmed=stats["ok"],
                 producers_skipped=stats["skipped"])

    scheduler.add_job(
        _startup_catchup,
        IntervalTrigger(minutes=_CATCHUP_RETRY_MIN, timezone=tz),
        id="startup_catchup",
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
