from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from .budget import BudgetConfig, BudgetGuard
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
from .sources.newsapi import NewsApiSource
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
    budget: BudgetGuard | None,
    first_run: bool,
) -> list[Source]:
    """Build the per-cycle source list from feeds.yaml + concept_uris.yaml.

    Two layers:
      - native_rss     — every cycle, free
      - newsapi        — every cycle if API key + budget present
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

    # Layer 2: NewsAPI.ai (skip if no key — graceful degradation)
    if not secrets.newsapi_ai_key:
        log.warning(
            "newsapi.disabled",
            reason="NEWSAPI_AI_KEY not set; running native_rss only",
        )
        return sources

    if budget is None:
        log.warning("newsapi.disabled", reason="no budget guard supplied")
        return sources

    # Resolve concept URIs from cache
    try:
        concept_uris = load_concept_uris(config.concept_uris_path)
    except FileNotFoundError:
        log.warning(
            "concept_uris.missing",
            path=str(config.concept_uris_path),
            hint="run scripts/resolve_concept_uris.py to populate",
        )
        from .config import ConceptUris

        concept_uris = ConceptUris(resolved={}, unresolved=[])

    # Build an alias index from watchlists.yaml so unresolved entities fall
    # back to disambiguating keywords (e.g. "Aviva plc" rather than just
    # "Aviva", which collides with rugby coverage).
    watchlists = load_watchlists(config.watchlists_path)
    alias_index: dict[str, list[str]] = {}
    for entry in watchlists.p1_japan + watchlists.p2_global:
        alias_index[entry.canonical] = [entry.canonical, *entry.aliases]

    date_start, date_end = _compute_date_window(first_run)
    for q in feeds.newsapi.queries:
        uris = [
            concept_uris.resolved[k]
            for k in q.concept_uri_keys
            if k in concept_uris.resolved
        ]
        # Entities that didn't resolve fall back to keyword matching using
        # canonical + all aliases so ambiguous names get disambiguated.
        unresolved_keywords: list[str] = []
        for k in q.concept_uri_keys:
            if k in concept_uris.resolved:
                continue
            unresolved_keywords.extend(alias_index.get(k, [k]))
        keywords = list(q.keyword_fallback) + unresolved_keywords

        sources.append(
            NewsApiSource(
                name=f"NewsAPI: {q.name}",
                api_key=secrets.newsapi_ai_key,
                lang=q.lang,
                concept_uris=uris,
                keywords=keywords,
                keyword_oper=q.keyword_oper,
                articles_count=q.articles_count,
                articles_sort_by=q.sort_by,
                date_start=date_start,
                date_end=date_end,
                tier=q.tier,
                budget=budget,
            )
        )
    log.info(
        "newsapi.queries.built",
        count=len([s for s in sources if isinstance(s, NewsApiSource)]),
        first_run=first_run,
        date_start=date_start,
        date_end=date_end,
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
    budget: BudgetGuard | None,
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
    if budget is not None:
        usage = budget.usage_summary()
        lines.append("")
        lines.append(
            f"  API budget (rolling 30d): {usage['used_30d']:,} / {usage['monthly_cap']:,} "
            f"({100 * usage['used_30d'] / max(usage['monthly_cap'], 1):.1f}%)"
        )
        lines.append(
            f"  Today so far: {usage['today']} / {usage['daily_soft_warning']} soft"
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
    *,
    budget: BudgetGuard | None,
) -> dict:
    """Phase 7 fetch_cycle. Reads feeds.yaml; native + NewsAPI layers; per-feed
    stats + cycle-end summary block.
    """
    cycle_started = datetime.now(timezone.utc)
    first_run = store.is_first_run()

    if budget is not None:
        budget.reset_cycle()

    sources = build_sources(
        config, feeds, secrets, budget=budget, first_run=first_run
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
        items, n_no_pub, n_old = apply_recency_filter(
            raw_items, recency_hours=config.collection.recency_hours
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
        budget=budget,
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

    budget = None
    if secrets.newsapi_ai_key:
        bcfg = BudgetConfig(
            provider="newsapi.ai",
            monthly_cap=feeds.newsapi.monthly_cap,
            per_cycle_hard_cap=feeds.newsapi.per_cycle_hard_cap,
            daily_soft_warning=feeds.newsapi.daily_soft_warning,
            timezone_name=feeds.newsapi.timezone,
        )
        budget = BudgetGuard(config=bcfg, store=store)

    try:
        return fetch_cycle(
            config, watchlists, relevance, feeds, secrets, store, budget=budget
        )
    finally:
        store.close()


def run_p1_batch_now(*, dry_run: bool = False) -> dict[str, object]:
    from .digest import run_p1_batch

    config = load_config()
    summarizer, mailer = _build_runtime_components(dry_run=dry_run)
    if summarizer is None or mailer is None:
        log.error(
            "p1_batch.skipped",
            reason="missing summarizer or mailer (set ANTHROPIC_API_KEY and SMTP creds, or use --dry-run)",
        )
        return {"summarized": 0, "failed": 0, "suppressed_dup": 0, "sent": False}
    store = Store(config.storage.db_path)
    try:
        return run_p1_batch(
            store=store,
            summarizer=summarizer,
            mailer=mailer,
            timezone_name=config.scheduler.timezone,
        )
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


def print_stats() -> int:
    """`--stats` (no fetch) — feed_stats + DB totals + api_usage summary."""
    config = load_config()
    store = Store(config.storage.db_path)
    try:
        feed_rows = store.all_feed_stats()
        totals = store.db_totals()
        secrets = Secrets()

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
        if secrets.newsapi_ai_key:
            bcfg = BudgetConfig(timezone_name=config.scheduler.timezone)
            budget = BudgetGuard(config=bcfg, store=store)
            usage = budget.usage_summary()
            print(
                f"  newsapi.ai           "
                f"{usage['used_30d']:>5} / {usage['monthly_cap']} "
                f"({100 * usage['used_30d'] / max(usage['monthly_cap'], 1):.1f}%)"
            )
            print(f"  today (JST):         {usage['today']} / {usage['daily_soft_warning']} soft")
        else:
            print("  (NEWSAPI_AI_KEY not set; NewsAPI.ai not in use)")
        return 0
    finally:
        store.close()
