from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from .ai_email import compose_email
from .mailer import DigestPayload, Mailer
from .store import Store
from .summarizer import Summarizer

log = structlog.get_logger()


def run_digest(
    *,
    store: Store,
    summarizer: Summarizer,
    mailer: Mailer,
    hours: int = 12,
    limit: int = 100,
    timezone_name: str = "Asia/Tokyo",
) -> dict[str, object]:
    """Twice-daily digest at 07:00 / 19:00 JST. Includes P1+P2 from the
    last `hours` (default 12 — matches the cron interval, no overlap).

    The email-composer step (one Claude Opus call via `ai_email.compose_email`)
    aggregates same-event clusters, ranks P1 first, and emits a curated
    DigestEntry list capped at 15 entries.
    """
    rows = store.digest_eligible_stories(hours=hours, limit=limit)
    if not rows:
        log.info("digest.empty", hours=hours)
        return {"summarized": 0, "failed": 0, "suppressed_dup": 0, "sent": False}

    entries = compose_email(rows, summarizer)
    if not entries:
        log.info("digest.composer_empty", input_rows=len(rows))
        return {
            "summarized": 0,
            "failed": 0,
            "suppressed_dup": 0,
            "sent": False,
        }

    date_label = datetime.now(ZoneInfo(timezone_name)).strftime("%m/%d %H:00")
    payload = DigestPayload(date_label=date_label, entries=entries)
    mailer.send_digest(payload)
    log.info(
        "digest.sent",
        entries=len(entries),
        input_rows=len(rows),
        dry_run=mailer.dry_run,
    )
    return {
        "summarized": len(entries),
        "failed": 0,
        "suppressed_dup": max(0, len(rows) - len(entries)),
        "sent": True,
    }
