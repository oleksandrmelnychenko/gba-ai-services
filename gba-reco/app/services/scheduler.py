"""Daily cache-warming scheduler — every active client has a warm reco:* entry by morning.

Modern cron via APScheduler. The worker warms the exact key scheme the API/service reads on a
default /recommend, so a warmed client is a guaranteed cache hit (no live compute on the request
path). Warming is idempotent — entries are simply overwritten with the day's fresh as_of, and the
warmed TTL outlives the interval — so a double-run is harmless.

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.recommendations import worker

log = get_logger("scheduler")


def _job() -> None:
    try:
        stats = worker.run()
        log.info("daily_warm_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.error("daily_warm_failed", error=str(exc))


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", hour=s.daily_warm_hour, tz=s.timezone)

    # catch-up run on startup so the cache isn't cold until the first scheduled fire
    _job()

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        _job,
        CronTrigger(hour=s.daily_warm_hour, minute=0, timezone=tz),
        id="daily_warm",
        replace_existing=True,
        misfire_grace_time=3600,  # if the process was down at the fire time, still run on recovery
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")


if __name__ == "__main__":
    main()
