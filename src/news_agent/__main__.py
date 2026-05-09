from __future__ import annotations

import argparse
import sys

import structlog

from .agent import print_stats, run_digest_now, run_once, run_p1_batch_now
from .config import load_config
from .logging_setup import setup_logging

log = structlog.get_logger()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news-agent")
    parser.add_argument("--once", action="store_true", help="Run a single fetch cycle and exit.")
    parser.add_argument(
        "--p1-batch-now",
        action="store_true",
        help="Force send the P1 batch (3-hour cadence) now and exit.",
    )
    parser.add_argument(
        "--digest-now",
        action="store_true",
        help="Force send today's digest (P1+P2 from last 24h) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print emails to stdout instead of sending via SMTP.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print feed_stats + DB totals + api_usage summary and exit (no fetch).",
    )
    args = parser.parse_args(argv)

    if args.stats:
        # Stats readout doesn't need full structlog setup — print directly.
        return print_stats()

    config = load_config()
    setup_logging(config.logging.log_path, config.logging.level)

    if args.once:
        counts = run_once(dry_run=args.dry_run)
        log.info("cycle.done", **counts)
        return 0

    if args.p1_batch_now:
        counts = run_p1_batch_now(dry_run=args.dry_run)
        log.info("p1_batch.done", **counts)
        return 0

    if args.digest_now:
        counts = run_digest_now(dry_run=args.dry_run)
        log.info("digest.done", **counts)
        return 0

    # Default: long-running scheduler
    from .scheduler import run_scheduler

    run_scheduler(config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
