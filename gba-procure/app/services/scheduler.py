"""Daily cache-warm scheduler — the cart plan (and per-producer plans) are warm before the workday.

Modern cron via APScheduler. Two jobs:
  * producer_warm (05:00 local) — full per-producer pass into procure:v1:producer:{id}:{as_of}
  * cart_warm     (06:00 local) — the cart plan into the EXACT key /plan/cart reads, so the
                                   API serves a cache hit (<1s) instead of recomputing live.
Both are idempotent (key carries as_of=today); safe to run more than once.

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.replenishment import worker

log = get_logger("scheduler")


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


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", producer_hour=s.producer_warm_hour,
             cart_hour=s.cart_warm_hour, tz=s.timezone)

    # catch-up warm on startup so the cart key isn't cold until the first scheduled fire
    _cart_job()

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        _producer_job,
        CronTrigger(hour=s.producer_warm_hour, minute=0, timezone=tz),
        id="producer_warm",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        _cart_job,
        CronTrigger(hour=s.cart_warm_hour, minute=0, timezone=tz),
        id="cart_warm",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")


if __name__ == "__main__":
    main()
