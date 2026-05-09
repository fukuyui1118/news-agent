from __future__ import annotations

from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .agent import run_digest_now, run_once, run_p1_batch_now
from .config import Config

log = structlog.get_logger()


def run_scheduler(config: Config, *, dry_run: bool = False) -> None:
    sched_cfg = config.scheduler
    scheduler = BlockingScheduler(timezone=sched_cfg.timezone)

    def fetch_job() -> None:
        log.info("scheduler.fetch_cycle.start")
        try:
            counts = run_once(dry_run=dry_run)
            log.info("scheduler.fetch_cycle.done", **counts)
        except Exception as e:
            log.error("scheduler.fetch_cycle.failed", error=str(e))

    def p1_batch_job() -> None:
        log.info("scheduler.p1_batch.start")
        try:
            counts = run_p1_batch_now(dry_run=dry_run)
            log.info("scheduler.p1_batch.done", **counts)
        except Exception as e:
            log.error("scheduler.p1_batch.failed", error=str(e))

    def digest_job() -> None:
        log.info("scheduler.digest.start")
        try:
            counts = run_digest_now(dry_run=dry_run)
            log.info("scheduler.digest.done", **counts)
        except Exception as e:
            log.error("scheduler.digest.failed", error=str(e))

    # Fetch every N minutes (default 30)
    scheduler.add_job(
        fetch_job,
        trigger=IntervalTrigger(minutes=sched_cfg.fetch_interval_minutes),
        id="fetch_cycle",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    # P1 batch every N hours (default 3)
    scheduler.add_job(
        p1_batch_job,
        trigger=IntervalTrigger(hours=sched_cfg.p1_batch_interval_hours),
        id="p1_batch",
        max_instances=1,
        coalesce=True,
    )
    # Daily digest at HH:MM in configured timezone
    scheduler.add_job(
        digest_job,
        trigger=CronTrigger(
            hour=sched_cfg.digest_cron_hour,
            minute=sched_cfg.digest_cron_minute,
            timezone=sched_cfg.timezone,
        ),
        id="daily_digest",
        max_instances=1,
        coalesce=True,
    )

    log.info(
        "scheduler.starting",
        fetch_interval_minutes=sched_cfg.fetch_interval_minutes,
        p1_batch_interval_hours=sched_cfg.p1_batch_interval_hours,
        digest_cron=f"{sched_cfg.digest_cron_hour:02d}:{sched_cfg.digest_cron_minute:02d}",
        timezone=sched_cfg.timezone,
        dry_run=dry_run,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.stopped")
