from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from .mailer import (
    DigestEntry,
    DigestPayload,
    Mailer,
    P1BatchEntry,
    P1BatchPayload,
)
from .similarity import is_duplicate
from .store import Store
from .summarizer import Article, Summarizer

log = structlog.get_logger()


def run_digest(
    *,
    store: Store,
    summarizer: Summarizer,
    mailer: Mailer,
    hours: int = 24,
    limit: int = 100,
    timezone_name: str = "Asia/Tokyo",
) -> dict[str, object]:
    """Daily 07:00 JST digest. Includes P1+P2 from the last `hours` (regardless of email status)."""
    rows = store.digest_eligible_stories(hours=hours, limit=limit)
    if not rows:
        log.info("digest.empty", hours=hours)
        return {"summarized": 0, "failed": 0, "suppressed_dup": 0, "sent": False}

    # Within-digest dedup: skip stories whose title is similar to one already added.
    entries: list[DigestEntry] = []
    seen_titles: list[str] = []
    failed = 0
    suppressed = 0

    for row in rows:
        if is_duplicate(row.title, seen_titles):
            suppressed += 1
            log.debug("digest.suppressed_dup", title=row.title)
            continue

        article = Article(
            title=row.title,
            source=row.source,
            url=row.url,
            raw_text=row.title,  # body not stored; title is acceptable for digest
            published_at=_parse_iso(row.published_at),
            entity=None,
        )
        try:
            summary = summarizer.summarize(article)
        except Exception as e:
            failed += 1
            log.error("digest.summarize.failed", url=row.url, error=str(e))
            continue

        entries.append(
            DigestEntry(
                priority=row.priority,
                headline_ja=summary.headline,
                original_title=row.title,
                source=row.source,
                url=row.url,
                summary_bullets=summary.bullets,
                entity=None,
            )
        )
        seen_titles.append(row.title)

    if not entries:
        return {"summarized": 0, "failed": failed, "suppressed_dup": suppressed, "sent": False}

    date_label = datetime.now(ZoneInfo(timezone_name)).strftime("%m/%d")
    payload = DigestPayload(date_label=date_label, entries=entries)
    mailer.send_digest(payload)
    log.info(
        "digest.sent",
        entries=len(entries),
        failed=failed,
        suppressed_dup=suppressed,
        dry_run=mailer.dry_run,
    )
    return {
        "summarized": len(entries),
        "failed": failed,
        "suppressed_dup": suppressed,
        "sent": True,
    }


def run_p1_batch(
    *,
    store: Store,
    summarizer: Summarizer,
    mailer: Mailer,
    timezone_name: str = "Asia/Tokyo",
    dedup_window_hours: int = 24,
) -> dict[str, object]:
    """3-hour P1 batch. Summarize and email all unemailed P1 stories with similarity dedup."""
    candidates = store.unemailed_stories(priority="P1")
    if not candidates:
        log.info("p1_batch.empty")
        return {"summarized": 0, "failed": 0, "suppressed_dup": 0, "sent": False}

    recent = store.recently_emailed_titles(hours=dedup_window_hours, priority="P1")
    seen_titles: list[str] = list(recent)

    entries: list[P1BatchEntry] = []
    failed = 0
    suppressed = 0

    for row in candidates:
        if is_duplicate(row.title, seen_titles):
            store.mark_suppressed_dup(url_hash=row.url_hash)
            suppressed += 1
            log.info("p1_batch.suppressed_dup", title=row.title)
            continue

        article = Article(
            title=row.title,
            source=row.source,
            url=row.url,
            raw_text=row.title,
            published_at=_parse_iso(row.published_at),
            entity=None,
        )
        try:
            summary = summarizer.summarize(article)
        except Exception as e:
            failed += 1
            log.error("p1_batch.summarize.failed", url=row.url, error=str(e))
            continue

        entries.append(
            P1BatchEntry(
                headline_ja=summary.headline,
                original_title=row.title,
                source=row.source,
                url=row.url,
                summary_bullets=summary.bullets,
                entity=None,
            )
        )
        store.mark_emailed(url_hash=row.url_hash, summary=summary.as_full_text())
        seen_titles.append(row.title)

    if not entries:
        log.info("p1_batch.no_fresh", suppressed_dup=suppressed)
        return {"summarized": 0, "failed": failed, "suppressed_dup": suppressed, "sent": False}

    timestamp_label = datetime.now(ZoneInfo(timezone_name)).strftime("%m/%d %H:00")
    payload = P1BatchPayload(timestamp_label=timestamp_label, entries=entries)
    mailer.send_p1_batch(payload)
    log.info(
        "p1_batch.sent",
        entries=len(entries),
        failed=failed,
        suppressed_dup=suppressed,
        dry_run=mailer.dry_run,
    )
    return {
        "summarized": len(entries),
        "failed": failed,
        "suppressed_dup": suppressed,
        "sent": True,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
