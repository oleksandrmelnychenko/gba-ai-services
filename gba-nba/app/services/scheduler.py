"""Daily task-generation scheduler — every manager has a fresh inbox by 09:00 Europe/Kyiv.

Modern cron via APScheduler. Generation is idempotent (task_key carries a monthly window): tasks
not done on an earlier day stay OPEN/SNOOZED and remain in the inbox, while new signals (new debts,
newly-due reorders, fresh cross-sell) are TOPPED UP on top — so "не зробив вчора" simply carries
into today and the day's new work is added. Safe to run more than once (double-run is a no-op).

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import worker

log = get_logger("scheduler")


def _job() -> None:
    try:
        stats = worker.run()
        log.info("daily_generation_done", **stats)
    except Exception as exc:  # noqa: BLE001
        log.error("daily_generation_failed", error=str(exc))


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", hour=s.daily_generate_hour, tz=s.timezone)

    # catch-up run on startup so the inbox isn't empty until the first scheduled fire
    _job()

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        _job,
        CronTrigger(hour=s.daily_generate_hour, minute=0, timezone=tz),
        id="daily_generate",
        replace_existing=True,
        misfire_grace_time=3600,  # if the process was down at 09:00, still run when it comes back
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")


if __name__ == "__main__":
    main()
