"""Daily cache-warming scheduler — every active client has a warm reco:* entry by morning.

Modern cron via APScheduler. The worker warms the exact key scheme the API/service reads on a
default /recommend, so a warmed client is a guaranteed cache hit (no live compute on the request
path). Warming is idempotent — entries are simply overwritten with the day's fresh as_of, and the
warmed TTL outlives the interval — so a double-run is harmless.

A failed warm (catch-up or 03:00 cron — e.g. a DB blip) schedules a one-shot retry with
exponential backoff (warm_retry_base_minutes doubling up to warm_retry_max_minutes) until a run
succeeds, instead of silently leaving the cache cold until tomorrow's cron.

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.recommendations import worker

log = get_logger("scheduler")

_RETRY_JOB_ID = "daily_warm_retry"


def _job(scheduler: BaseScheduler, attempt: int = 0) -> None:
    s = get_settings()
    try:
        stats = worker.run()
        if stats["clients"] and not stats["ok"]:
            raise RuntimeError(f"warm produced 0 ok of {stats['clients']} clients")
    except Exception as exc:  # noqa: BLE001
        delay_min = min(s.warm_retry_base_minutes * (2 ** attempt), s.warm_retry_max_minutes)
        log.error("daily_warm_failed", error=str(exc), attempt=attempt + 1,
                  retry_in_min=delay_min)
        scheduler.add_job(
            _job,
            DateTrigger(run_date=datetime.now(scheduler.timezone) + timedelta(minutes=delay_min)),
            args=[scheduler, attempt + 1],
            id=_RETRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=None,  # a late retry must still run, never be discarded
        )
        return
    with suppress(JobLookupError):
        scheduler.remove_job(_RETRY_JOB_ID)
    log.info("daily_warm_done", **stats)


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", hour=s.daily_warm_hour, tz=s.timezone)

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        _job,
        CronTrigger(hour=s.daily_warm_hour, minute=0, timezone=tz),
        args=[scheduler],
        id="daily_warm",
        replace_existing=True,
        misfire_grace_time=3600,  # if the process was down at the fire time, still run on recovery
        coalesce=True,
    )
    # catch-up run on startup so the cache isn't cold until the first scheduled fire; routed
    # through the scheduler so a failure enters the same one-shot retry path as the cron run
    scheduler.add_job(
        _job,
        DateTrigger(run_date=datetime.now(tz)),
        args=[scheduler],
        id="daily_warm_catchup",
        misfire_grace_time=3600,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")


if __name__ == "__main__":
    main()
