"""Daily task-generation scheduler — every manager has a fresh inbox by 09:00 Europe/Kyiv.

Modern cron via APScheduler. Generation is idempotent (task_key carries a monthly window): tasks
not done on an earlier day stay OPEN/SNOOZED and remain in the inbox, while new signals (new debts,
newly-due reorders, fresh cross-sell) are TOPPED UP on top — so "не зробив вчора" simply carries
into today and the day's new work is added. Safe to run more than once (double-run is a no-op).

Run as a dedicated process:  python -m app.services.scheduler
"""
from __future__ import annotations

import time
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


_CATCHUP_MAX_BACKOFF_S = 600.0


def _startup_catchup() -> None:
    """Catch-up run on startup so the inbox isn't empty until the first scheduled fire.
    Unlike the scheduled _job, a failure here (e.g. MSSQL still coming up after a host reboot)
    is RETRIED with exponential backoff capped at 10 min, until a run succeeds — otherwise a
    boot-order race would silently leave every inbox empty until 09:00."""
    delay = 60.0
    while True:
        try:
            stats = worker.run()
        except Exception as exc:  # noqa: BLE001
            log.error("startup_catchup_failed", error=str(exc), retry_in_s=int(delay))
        else:
            if stats["managers"] == 0 or stats["ok"] > 0:
                log.info("startup_catchup_done", **stats)
                return
            log.error("startup_catchup_all_managers_failed", retry_in_s=int(delay), **stats)
        time.sleep(delay)
        delay = min(delay * 2, _CATCHUP_MAX_BACKOFF_S)


def main() -> None:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    log.info("scheduler_starting", hour=s.daily_generate_hour, tz=s.timezone)

    _startup_catchup()

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
