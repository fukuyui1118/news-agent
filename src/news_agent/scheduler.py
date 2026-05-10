from __future__ import annotations

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .agent import run_digest_now, run_once
from .config import Config

log = structlog.get_logger()


def run_scheduler(config: Config, *, dry_run: bool = False) -> None:
    sched_cfg = config.scheduler
    scheduler = BlockingScheduler(timezone=sched_cfg.timezone)

    def fetch_and_digest_job() -> None:
        log.info("scheduler.run.start")
        try:
            counts = run_once(dry_run=dry_run)
            log.info("scheduler.fetch_cycle.done", **counts)
        except Exception as e:
            log.error("scheduler.fetch_cycle.failed", error=str(e))

        try:
            counts = run_digest_now(dry_run=dry_run)
            log.info("scheduler.digest.done", **counts)
        except Exception as e:
            log.error("scheduler.digest.failed", error=str(e))

    # Single cron-triggered pipeline: fetch + curate + email, twice daily.
    scheduler.add_job(
        fetch_and_digest_job,
        trigger=CronTrigger(
            hour=sched_cfg.digest_cron_hours,    # e.g. "7,19"
            minute=sched_cfg.digest_cron_minute, # 0
            timezone=sched_cfg.timezone,
        ),
        id="fetch_and_digest",
        max_instances=1,
        coalesce=True,
    )

    log.info(
        "scheduler.starting",
        digest_cron_hours=sched_cfg.digest_cron_hours,
        digest_cron_minute=sched_cfg.digest_cron_minute,
        timezone=sched_cfg.timezone,
        dry_run=dry_run,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.stopped")
