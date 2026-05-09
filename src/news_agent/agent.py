from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from .classifier import classify
from .config import (
    Config,
    Relevance,
    Secrets,
    Watchlists,
    load_buckets,
    load_config,
    load_relevance,
    load_watchlists,
)
from .mailer import Mailer, MailerConfig
from .relevance import is_relevant
from .sources.base import RawItem, Source
from .sources.rss import RSSSource
from .store import Store

log = structlog.get_logger()


def apply_recency_filter(
    items: list[RawItem], *, recency_hours: int = 24
) -> tuple[list[RawItem], int, int]:
    """Filter to items published within the last `recency_hours`.

    Items with `published_at` of None or a naive datetime are skipped with a
    `source.no_pubdate` warning (counted as `no_pubdate`). Items older than the
    threshold are skipped with a `story.too_old` info log (counted as `too_old`).
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
                "source.no_pubdate",
                source=item.source,
                title=item.title,
                url=item.url,
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
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def build_sources(config: Config) -> list[Source]:
    """Combine static (YAML-defined) sources with auto-generated Google News
    queries (every watchlist entity × every keyword bucket).
    """
    import urllib.parse

    from .query_builder import generate_google_news_queries

    sources: list[Source] = []
    secrets: Secrets | None = None

    # Static sources from config.yaml
    for src in config.sources:
        if not src.enabled:
            continue
        if src.type == "rss":
            sources.append(RSSSource(name=src.name, url=src.url, tier=src.tier))
        elif src.type == "google_news_rss":
            # Legacy hand-crafted query rows. Phase 6 prefers auto-generation,
            # but keep this branch so config.yaml can hold one-offs if needed.
            if not src.query:
                log.warning("source.google_news.no_query", name=src.name)
                continue
            encoded = urllib.parse.quote(src.query)
            url = (
                f"https://news.google.com/rss/search?"
                f"q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
            )
            sources.append(RSSSource(name=src.name, url=url, tier=src.tier))
        elif src.type == "browser_use":
            from .sources.nikkei import NikkeiSource

            if secrets is None:
                secrets = Secrets()
            sources.append(
                NikkeiSource(
                    name=src.name,
                    url=src.url,
                    tier=src.tier,
                    nikkei_user=secrets.nikkei_user,
                    nikkei_pass=secrets.nikkei_pass,
                    browser_use_model=secrets.browser_use_model,
                    anthropic_api_key=secrets.anthropic_api_key,
                )
            )
        else:
            log.warning("source.type.unsupported", type=src.type, name=src.name)

    # Auto-generated Google News queries: every watchlist entity × every bucket
    watchlists = load_watchlists(config.watchlists_path)
    buckets = load_buckets(config.query_buckets_path)
    queries = generate_google_news_queries(
        watchlists=watchlists,
        buckets=buckets.buckets,
        recency_hours=config.collection.recency_hours,
    )
    log.info(
        "query_builder.generated",
        count=len(queries),
        entities=len(watchlists.p1_japan) + len(watchlists.p2_global),
        buckets=len(buckets.buckets),
        recency_hours=config.collection.recency_hours,
    )
    for q in queries:
        encoded = urllib.parse.quote(q.query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        sources.append(
            RSSSource(
                name=f"GN: {q.entity_priority} {q.entity_canonical} / {q.bucket_name}",
                url=url,
                tier=2,
            )
        )

    return sources


async def _fetch_one(
    source: Source, sem: asyncio.Semaphore
) -> tuple[Source, list[RawItem], BaseException | None]:
    async with sem:
        try:
            items = await asyncio.to_thread(source.fetch)
            return source, items, None
        except BaseException as e:  # catch broadly; isolated per source
            return source, [], e


async def _fetch_all(
    sources: list[Source], concurrency: int
) -> list[tuple[Source, list[RawItem], BaseException | None]]:
    sem = asyncio.Semaphore(concurrency)
    return await asyncio.gather(*(_fetch_one(s, sem) for s in sources))


def _build_runtime_components(*, dry_run: bool) -> tuple[object | None, Mailer | None]:
    """Returns (summarizer, mailer). Summarizer is created lazily — its import
    pulls in `anthropic`, which we don't want to do for fetch-only cycles.
    """
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


def fetch_cycle(
    config: Config,
    watchlists: Watchlists,
    relevance: Relevance,
    store: Store,
) -> dict[str, int]:
    """Fetch + classify + persist. P1 emails happen in the 3-hour batch job (Phase 4).

    Phase 5: only items published within the last 24 hours are considered for
    classification + persistence. Older items are dropped before dedup so they
    don't pollute the DB.
    """
    counts = {
        "sources": 0,
        "source_errors": 0,
        "fetched": 0,
        "no_pubdate": 0,
        "too_old": 0,
        "new": 0,
        "p1": 0,
        "p2": 0,
        "p3": 0,
        "dropped": 0,
    }
    sources = build_sources(config)
    counts["sources"] = len(sources)
    log.info("fetch_cycle.start", sources=len(sources), concurrency=config.collection.fetch_concurrency)

    results = asyncio.run(_fetch_all(sources, config.collection.fetch_concurrency))

    for source, raw_items, error in results:
        if error is not None:
            counts["source_errors"] += 1
            log.error("source.fetch.failed", source=source.name, error=str(error))
            continue
        items, n_no_pub, n_old = apply_recency_filter(
            raw_items, recency_hours=config.collection.recency_hours
        )
        counts["no_pubdate"] += n_no_pub
        counts["too_old"] += n_old
        if raw_items or n_no_pub or n_old:
            log.info(
                "source.fetched",
                source=source.name,
                total=len(raw_items),
                kept=len(items),
                no_pubdate=n_no_pub,
                too_old=n_old,
            )
        for item in items:
            counts["fetched"] += 1
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
            )
            if not inserted_hash:
                continue

            counts["new"] += 1
            counts[priority.lower()] += 1
            log.info(
                "story.new",
                priority=priority,
                canonical=match.canonical or None,
                matched=match.matched_alias or None,
                dropped_reason=dropped_reason,
                source=item.source,
                tier=item.source_tier,
                title=item.title,
                url=item.url,
            )
    return counts


def run_once(*, dry_run: bool = False) -> dict[str, int]:
    config = load_config()
    watchlists = load_watchlists(config.watchlists_path)
    relevance = load_relevance(config.relevance_path)
    store = Store(config.storage.db_path)
    try:
        return fetch_cycle(config, watchlists, relevance, store)
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
