from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .classifier import classify
from .config import (
    Config,
    Feeds,
    Relevance,
    Secrets,
    Watchlists,
    load_concept_uris,
    load_config,
    load_feeds,
    load_relevance,
    load_watchlists,
)
from .mailer import Mailer, MailerConfig
from .relevance import is_relevant
from .sources.base import RawItem, Source
from .sources.claude_research import ClaudeResearchSource
from .sources.newsapi import NewsApiSource  # retained but no longer instantiated
from .sources.rss import RSSSource
from .store import Store

log = structlog.get_logger()


def apply_recency_filter(
    items: list[RawItem], *, recency_hours: int = 24
) -> tuple[list[RawItem], int, int]:
    """Filter to items published within the last `recency_hours`.

    Items with no/naive `published_at` → `source.no_pubdate` warning, counted
    as `no_pubdate`. Items older than the threshold → `story.too_old` info,
    counted as `too_old`.
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=recency_hours)
    kept: list[RawItem] = []
    no_pubdate = 0
    too_old = 0
    for item in items:
        pub = item.published_at
        if pub is None or pub.tzinfo is None:
            no_pubdate += 1
            log.warning(
                "source.no_pubdate", source=item.source, title=item.title, url=item.url
            )
            continue
        if pub < threshold:
            too_old += 1
            log.info(
                "story.too_old",
                source=item.source,
                title=item.title,
                age_hours=round((now - pub).total_seconds() / 3600, 1),
            )
            continue
        kept.append(item)
    return kept, no_pubdate, too_old


# Backwards-compat constant for existing tests / external callers.
RECENCY_HOURS = 24


def _normalize_text(text: str) -> str:
    return (
        (text or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def _compute_date_window(first_run: bool) -> tuple[str, str]:
    """Return (dateStart, dateEnd) as YYYY-MM-DD for NewsAPI.ai queries."""
    end = datetime.now(timezone.utc)
    if first_run:
        start = end - timedelta(hours=24)
    else:
        start = end - timedelta(hours=2)  # small buffer for indexing lag
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def build_sources(
    config: Config,
    feeds: Feeds,
    secrets: Secrets,
    *,
    store: Store,
    watchlists: Watchlists | None = None,
    first_run: bool = False,
) -> list[Source]:
    """Build the per-cycle source list from feeds.yaml.

    Two layers:
      - native_rss        — every cycle, free
      - claude_research   — Claude Opus 4.7 + web_search; cadence-gated
                            (typically 12h) so only fires ~twice per day.
    """
    sources: list[Source] = []

    # Layer 1: native RSS
    for nf in feeds.native_rss:
        sources.append(
            RSSSource(
                name=nf.name,
                url=nf.url,
                tier=nf.tier,
                trust_freshness=nf.trust_freshness,
            )
        )

    # Layer 2: Claude Opus 4.7 + web_search (curated research). Phase 8.
    # Replaces the prior NewsAPI.ai layer entirely. Cadence-gated per query
    # (typically 12h) so the hourly fetch_cycle invokes it but it only
    # actually fires twice per day.
    if not secrets.anthropic_api_key:
        log.warning(
            "claude_research.disabled",
            reason="ANTHROPIC_API_KEY not set; running native_rss only",
        )
        return sources

    for q in feeds.claude_research.queries:
        sources.append(
            ClaudeResearchSource(
                name=f"Claude Research: {q.name}",
                api_key=secrets.anthropic_api_key,
                watchlists=watchlists,
                model=q.model,
                cadence_hours=q.cadence_hours,
                tier=q.tier,
                max_headlines=q.max_headlines,
                max_search_uses=q.max_search_uses,
                store=store,
            )
        )
    log.info(
        "claude_research.queries.built",
        count=len(feeds.claude_research.queries),
    )
    return sources


def _build_runtime_components(*, dry_run: bool) -> tuple[object | None, Mailer | None]:
    secrets = Secrets()
    summarizer = None
    if secrets.anthropic_api_key:
        from .summarizer import Summarizer

        summarizer = Summarizer(api_key=secrets.anthropic_api_key)
    else:
        log.warning(
            "summarizer.disabled",
            reason="ANTHROPIC_API_KEY not set; P1/digest stories will be persisted but not summarized/emailed",
        )

    mailer: Mailer | None = None
    if dry_run or secrets.smtp_password:
        mailer = Mailer(
            MailerConfig(
                smtp_host=secrets.smtp_host,
                smtp_port=secrets.smtp_port,
                smtp_user=secrets.smtp_user,
                smtp_password=secrets.smtp_password,
                email_from=secrets.email_from,
                email_to=secrets.email_to,
            ),
            dry_run=dry_run,
        )
    else:
        log.warning(
            "mailer.disabled",
            reason="SMTP_PASSWORD not set and --dry-run not active; emails will not be sent",
        )

    return summarizer, mailer


async def _fetch_one(
    source: Source, sem: asyncio.Semaphore
) -> tuple[Source, list[RawItem], BaseException | None]:
    async with sem:
        try:
            items = await asyncio.to_thread(source.fetch)
            return source, items, None
        except BaseException as e:
            return source, [], e


async def _fetch_all(
    sources: list[Source], concurrency: int
) -> list[tuple[Source, list[RawItem], BaseException | None]]:
    sem = asyncio.Semaphore(concurrency)
    return await asyncio.gather(*(_fetch_one(s, sem) for s in sources))


def _format_stats_block(
    *,
    cycle_seconds: float,
    counts: dict,
    failed_feeds: list[tuple[str, str]],
    sources_total: int,
    store: Store | None = None,
    timezone_name: str = "Asia/Tokyo",
) -> str:
    now_local = datetime.now(ZoneInfo(timezone_name))
    feeds_ok = sources_total - len(failed_feeds)
    failure_summary = ""
    if failed_feeds:
        first = failed_feeds[0]
        failure_summary = f" ({len(failed_feeds)} failed: {first[0]} — {first[1][:60]})"
    next_run = (now_local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    lines = [
        f"[{now_local:%Y-%m-%d %H:%M:%S} {now_local.tzname()}] Cycle complete in {cycle_seconds:.1f}s",
        f"  Feeds fetched: {feeds_ok}/{sources_total}{failure_summary}",
        f"  Items seen (raw): {counts.get('raw', 0):,}",
        f"  Items new (not in DB): {counts.get('new', 0):,}",
        f"  After 24h published_at filter: {counts.get('after_recency', 0):,}",
        f"  Classified: P1={counts.get('p1', 0)}, "
        f"P2={counts.get('p2', 0)}, "
        f"P3={counts.get('p3', 0)}, "
        f"discarded={counts.get('dropped', 0)}",
        f"  P1 alerts queued: {counts.get('p1', 0)}",
        f"  Next run: {next_run:%H:%M %Z}",
    ]
    if store is not None:
        anth_30d = store.api_call_count(provider="anthropic", hours=24 * 30)
        anth_today = store.api_call_count_today(provider="anthropic", timezone_name=timezone_name)
        if anth_30d > 0:
            lines.append("")
            lines.append(
                f"  Anthropic calls (rolling 30d): {anth_30d} | today: {anth_today}"
            )
    return "\n".join(lines)


def _append_to_stats_log(block: str, log_path: Path) -> None:
    stats_path = log_path.parent / "stats.log"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stats_path, "a", encoding="utf-8") as f:
        f.write(block + "\n\n")


def fetch_cycle(
    config: Config,
    watchlists: Watchlists,
    relevance: Relevance,
    feeds: Feeds,
    secrets: Secrets,
    store: Store,
) -> dict:
    """Phase 8 fetch_cycle. Reads feeds.yaml; native_rss + claude_research
    layers; per-feed stats + cycle-end summary block.
    """
    cycle_started = datetime.now(timezone.utc)
    first_run = store.is_first_run()

    sources = build_sources(
        config, feeds, secrets,
        store=store, watchlists=watchlists, first_run=first_run,
    )

    counts = {
        "sources": len(sources),
        "raw": 0,             # total items returned across all feeds before any filter
        "no_pubdate": 0,
        "too_old": 0,
        "after_recency": 0,
        "new": 0,             # items inserted (passed url+content dedup)
        "p1": 0, "p2": 0, "p3": 0, "dropped": 0,
        "first_run": first_run,
    }
    failed_feeds: list[tuple[str, str]] = []

    log.info(
        "fetch_cycle.start",
        sources=len(sources),
        concurrency=config.collection.fetch_concurrency,
        first_run=first_run,
    )

    results = asyncio.run(_fetch_all(sources, config.collection.fetch_concurrency))

    for source, raw_items, error in results:
        feed_name = source.name
        if error is not None:
            failed_feeds.append((feed_name, str(error)))
            log.error("source.fetch.failed", source=feed_name, error=str(error))
            store.update_feed_stats(feed_name=feed_name, success=False, error=str(error))
            continue

        counts["raw"] += len(raw_items)
        # claude_research opt-in fallback: when the in-window yield is thin,
        # the Stage-1 prompt is allowed to surface items up to 72h old. Widen
        # the recency gate only for that source so RSS still respects the
        # Phase-5 24h rule.
        recency_hours = (
            72 if isinstance(source, ClaudeResearchSource)
            else config.collection.recency_hours
        )
        items, n_no_pub, n_old = apply_recency_filter(
            raw_items, recency_hours=recency_hours
        )
        counts["no_pubdate"] += n_no_pub
        counts["too_old"] += n_old
        counts["after_recency"] += len(items)

        classified_count = 0
        for item in items:
            text = _normalize_text(item.title + "\n" + item.raw_text)
            match = classify(text, watchlists)
            priority = match.priority
            dropped_reason: str | None = None
            if priority == "P3":
                gate = is_relevant(text, item.source_tier, relevance)
                if not gate.relevant:
                    priority = "DROPPED"
                    dropped_reason = gate.reason

            inserted_hash = store.insert_if_new(
                url=item.url,
                title=item.title,
                source=item.source,
                published_at=item.published_at,
                priority=priority,
                dropped_reason=dropped_reason,
                body=item.raw_text,
            )
            if not inserted_hash:
                continue

            counts["new"] += 1
            counts[priority.lower()] += 1
            if priority != "DROPPED":
                classified_count += 1
            log.info(
                "story.new",
                priority=priority,
                canonical=match.canonical or None,
                matched=match.matched_alias or None,
                dropped_reason=dropped_reason,
                source=item.source,
                title=item.title,
                url=item.url,
            )

        store.update_feed_stats(
            feed_name=feed_name,
            success=True,
            items_returned=len(raw_items),
            items_classified=classified_count,
        )

    cycle_seconds = (datetime.now(timezone.utc) - cycle_started).total_seconds()
    counts["cycle_seconds"] = round(cycle_seconds, 1)

    block = _format_stats_block(
        cycle_seconds=cycle_seconds,
        counts=counts,
        failed_feeds=failed_feeds,
        sources_total=len(sources),
        store=store,
        timezone_name=config.scheduler.timezone,
    )
    print(block)
    _append_to_stats_log(block, config.logging.log_path)
    log.info("cycle.done", **{k: v for k, v in counts.items() if not isinstance(v, dict)})
    return counts


def run_once(*, dry_run: bool = False) -> dict:
    config = load_config()
    watchlists = load_watchlists(config.watchlists_path)
    relevance = load_relevance(config.relevance_path)
    feeds = load_feeds(config.feeds_path)
    secrets = Secrets()
    store = Store(config.storage.db_path)

    try:
        return fetch_cycle(config, watchlists, relevance, feeds, secrets, store)
    finally:
        store.close()


def run_digest_now(*, dry_run: bool = False) -> dict[str, object]:
    from .digest import run_digest

    config = load_config()
    summarizer, mailer = _build_runtime_components(dry_run=dry_run)
    if summarizer is None or mailer is None:
        log.error(
            "digest.skipped",
            reason="missing summarizer or mailer (set ANTHROPIC_API_KEY and SMTP creds, or use --dry-run)",
        )
        return {"summarized": 0, "failed": 0, "suppressed_dup": 0, "sent": False}
    store = Store(config.storage.db_path)
    try:
        return run_digest(
            store=store,
            summarizer=summarizer,
            mailer=mailer,
            timezone_name=config.scheduler.timezone,
        )
    finally:
        store.close()


def run_fetch_and_digest_now(*, dry_run: bool = False) -> dict[str, object]:
    """Manual full-pipeline trigger: one fetch cycle followed by one digest.

    Mirrors what the scheduler does at each 07:00 / 19:00 JST tick.
    """
    fetch_counts = run_once(dry_run=dry_run)
    digest_counts = run_digest_now(dry_run=dry_run)
    return {
        "fetch": fetch_counts,
        "digest": digest_counts,
    }


def print_stats() -> int:
    """`--stats` (no fetch) — feed_stats + DB totals + api_usage summary."""
    config = load_config()
    store = Store(config.storage.db_path)
    try:
        feed_rows = store.all_feed_stats()
        totals = store.db_totals()

        print("=== feed_stats ===")
        if not feed_rows:
            print("  (no feeds tracked yet — run --once at least once)")
        else:
            print(f"  {'feed_name':40} {'last_success':20} {'last_failure':20} items_last  consec_fail")
            for row in feed_rows:
                print(
                    f"  {row['feed_name'][:40]:40} "
                    f"{(row['last_success_at'] or '—')[:19]:20} "
                    f"{(row['last_failure_at'] or '—')[:19]:20} "
                    f"{row['items_returned_last_run']:>10}  "
                    f"{row['consecutive_failures']:>11}"
                )

        print()
        print("=== DB totals ===")
        print(f"  {'priority':10} {'24h':>8} {'7d':>8} {'all':>8}")
        for p in ("p1", "p2", "p3", "dropped"):
            print(
                f"  {p.upper():10} "
                f"{totals.get(f'{p}_24h', 0):>8} "
                f"{totals.get(f'{p}_7d', 0):>8} "
                f"{totals.get(f'{p}_all', 0):>8}"
            )

        print()
        print("=== api_usage (rolling 30d) ===")
        for provider in ("anthropic", "newsapi.ai"):
            n_30d = store.api_call_count(provider=provider, hours=24 * 30)
            n_today = store.api_call_count_today(
                provider=provider, timezone_name=config.scheduler.timezone
            )
            if n_30d > 0 or provider == "anthropic":
                print(f"  {provider:15} 30d={n_30d:>4}  today={n_today:>3}")
        return 0
    finally:
        store.close()
