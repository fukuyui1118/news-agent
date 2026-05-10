from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = (
    "utm_",
    "ref_",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def hash_item(canonical_url: str, title: str) -> str:
    key = canonical_url + "\x00" + title.strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def content_hash(title: str, body: str) -> str:
    """SHA256 of (normalized title + first 200 chars of normalized body).

    Catches the same article published under different URLs by NewsAPI vs a
    direct RSS feed. Robust to whitespace and case but sensitive to wording —
    won't false-merge two unrelated stories.
    """
    norm_title = " ".join((title or "").lower().split())
    norm_body = " ".join((body or "").lower().split())[:200]
    key = norm_title + "\x01" + norm_body
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    published_at TEXT,
    priority TEXT NOT NULL,
    summary TEXT,
    emailed_at TEXT,
    dropped_reason TEXT,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_priority_emailed
    ON seen(priority, emailed_at);
CREATE INDEX IF NOT EXISTS idx_seen_content_hash
    ON seen(content_hash);

CREATE TABLE IF NOT EXISTS api_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at   TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    endpoint    TEXT    NOT NULL,
    query_name  TEXT,
    article_count INTEGER,
    elapsed_ms  INTEGER,
    http_status INTEGER,
    error       TEXT,
    searches_run          INTEGER,
    tier1_aggregators_hit INTEGER,
    fallback_used         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_usage_called_at ON api_usage(called_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider_endpoint
    ON api_usage(provider, endpoint);

CREATE TABLE IF NOT EXISTS feed_stats (
    feed_name                  TEXT PRIMARY KEY,
    last_success_at            TEXT,
    last_failure_at            TEXT,
    last_error                 TEXT,
    items_returned_last_run    INTEGER DEFAULT 0,
    items_classified_last_run  INTEGER DEFAULT 0,
    consecutive_failures       INTEGER DEFAULT 0
);
"""


@dataclass
class StoryRow:
    url_hash: str
    url: str
    title: str
    source: str
    published_at: str | None
    priority: str


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        if db_path.parent and str(db_path.parent) not in ("", "."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so async sources running in worker threads
        # (via asyncio.to_thread) can write api_usage rows. SQLite serializes
        # writes via its own locking; isolation_level remains default (auto).
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL mode gives concurrent readers + a single writer without conflict;
        # safer for the multi-threaded fetch + dashboard read pattern.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        # Index on collected_at is created after migration to avoid referencing
        # the column on legacy DBs that still have `fetched_at` at SCHEMA-apply time.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_collected_at ON seen(collected_at)"
        )
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(seen)").fetchall()}
        if "dropped_reason" not in cols:
            self.conn.execute("ALTER TABLE seen ADD COLUMN dropped_reason TEXT")
            cols.add("dropped_reason")
        # Phase 5: rename fetched_at -> collected_at if needed (SQLite 3.25+).
        if "fetched_at" in cols and "collected_at" not in cols:
            self.conn.execute("ALTER TABLE seen RENAME COLUMN fetched_at TO collected_at")
            self.conn.execute("DROP INDEX IF EXISTS idx_seen_fetched_at")
        # Phase 7: cross-source dedup via content_hash.
        if "content_hash" not in cols:
            self.conn.execute("ALTER TABLE seen ADD COLUMN content_hash TEXT")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_content_hash ON seen(content_hash)"
            )
        # Phase 8.3: claude_research COVERAGE_NOTES telemetry.
        api_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(api_usage)").fetchall()}
        if "searches_run" not in api_cols:
            self.conn.execute("ALTER TABLE api_usage ADD COLUMN searches_run INTEGER")
        if "tier1_aggregators_hit" not in api_cols:
            self.conn.execute("ALTER TABLE api_usage ADD COLUMN tier1_aggregators_hit INTEGER")
        if "fallback_used" not in api_cols:
            self.conn.execute("ALTER TABLE api_usage ADD COLUMN fallback_used INTEGER")

    def insert_if_new(
        self,
        *,
        url: str,
        title: str,
        source: str,
        published_at: datetime | None,
        priority: str,
        dropped_reason: str | None = None,
        body: str = "",
    ) -> str | None:
        """Insert a row. Returns url_hash on insert, or None if duplicate.

        Two-layer dedup: by url_hash (primary key) and by content_hash (covers
        the same article appearing from NewsAPI.ai AND a native RSS feed under
        different URLs).
        """
        canonical = canonicalize_url(url)
        h = hash_item(canonical, title)
        c_hash = content_hash(title, body)

        # Cross-source dedup: bail if content_hash already present.
        existing = self.conn.execute(
            "SELECT 1 FROM seen WHERE content_hash = ? LIMIT 1", (c_hash,)
        ).fetchone()
        if existing:
            return None

        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO seen
                (url_hash, url, title, source, collected_at, published_at,
                 priority, dropped_reason, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                h,
                canonical,
                title,
                source,
                datetime.now(timezone.utc).isoformat(),
                published_at.isoformat() if published_at else None,
                priority,
                dropped_reason,
                c_hash,
            ),
        )
        self.conn.commit()
        return h if cur.rowcount == 1 else None

    def mark_emailed(self, *, url_hash: str, summary: str) -> None:
        self.conn.execute(
            "UPDATE seen SET summary = ?, emailed_at = ? WHERE url_hash = ?",
            (summary, datetime.now(timezone.utc).isoformat(), url_hash),
        )
        self.conn.commit()

    def digest_eligible_stories(self, *, hours: int = 24, limit: int = 100) -> list[StoryRow]:
        """P1+P2 stories collected within the last `hours`, regardless of email status.

        The daily digest re-summarizes the day's important news, so it deliberately
        includes stories already emailed in 3-hour P1 batches.
        """
        cur = self.conn.execute(
            f"""
            SELECT url_hash, url, title, source, published_at, priority
            FROM seen
            WHERE priority IN ('P1', 'P2')
              AND collected_at > datetime('now', '-{int(hours)} hours')
            ORDER BY priority ASC, collected_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            StoryRow(
                url_hash=row[0],
                url=row[1],
                title=row[2],
                source=row[3],
                published_at=row[4],
                priority=row[5],
            )
            for row in cur.fetchall()
        ]

    def unemailed_stories(self, *, priority: str = "P1", limit: int = 100) -> list[StoryRow]:
        """Stories of `priority` with emailed_at IS NULL, ordered by collected_at desc."""
        cur = self.conn.execute(
            """
            SELECT url_hash, url, title, source, published_at, priority
            FROM seen
            WHERE priority = ?
              AND emailed_at IS NULL
            ORDER BY collected_at DESC
            LIMIT ?
            """,
            (priority, limit),
        )
        return [
            StoryRow(
                url_hash=row[0],
                url=row[1],
                title=row[2],
                source=row[3],
                published_at=row[4],
                priority=row[5],
            )
            for row in cur.fetchall()
        ]

    def recently_emailed_titles(self, *, hours: int = 24, priority: str = "P1") -> list[str]:
        cur = self.conn.execute(
            f"""
            SELECT title
            FROM seen
            WHERE priority = ?
              AND emailed_at IS NOT NULL
              AND emailed_at > datetime('now', '-{int(hours)} hours')
            """,
            (priority,),
        )
        return [row[0] for row in cur.fetchall()]

    def mark_suppressed_dup(self, *, url_hash: str) -> None:
        """Mark as 'emailed' so it's not picked up again, with a sentinel summary."""
        self.conn.execute(
            "UPDATE seen SET summary = ?, emailed_at = ? WHERE url_hash = ?",
            (
                "[suppressed: duplicate of recently-emailed P1]",
                datetime.now(timezone.utc).isoformat(),
                url_hash,
            ),
        )
        self.conn.commit()

    # ----- api_usage --------------------------------------------------------

    def record_api_call(
        self,
        *,
        provider: str,
        endpoint: str,
        query_name: str | None = None,
        article_count: int | None = None,
        elapsed_ms: int | None = None,
        http_status: int | None = None,
        error: str | None = None,
        searches_run: int | None = None,
        tier1_aggregators_hit: int | None = None,
        fallback_used: bool | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO api_usage
                (called_at, provider, endpoint, query_name, article_count,
                 elapsed_ms, http_status, error,
                 searches_run, tier1_aggregators_hit, fallback_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                provider,
                endpoint,
                query_name,
                article_count,
                elapsed_ms,
                http_status,
                error,
                searches_run,
                tier1_aggregators_hit,
                int(fallback_used) if fallback_used is not None else None,
            ),
        )
        self.conn.commit()

    def api_call_count(self, *, provider: str, hours: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND called_at > ?",
            (provider, cutoff.isoformat()),
        )
        return cur.fetchone()[0]

    def last_successful_call_at(
        self, *, provider: str, query_name: str | None = None
    ) -> datetime | None:
        """Return the UTC timestamp of the most recent successful (error IS NULL)
        call for `provider` (and optionally `query_name`), or None if none."""
        if query_name is None:
            cur = self.conn.execute(
                """
                SELECT called_at FROM api_usage
                WHERE provider = ? AND error IS NULL
                ORDER BY called_at DESC LIMIT 1
                """,
                (provider,),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT called_at FROM api_usage
                WHERE provider = ? AND query_name = ? AND error IS NULL
                ORDER BY called_at DESC LIMIT 1
                """,
                (provider, query_name),
            )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            dt = datetime.fromisoformat(row[0])
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def api_call_count_today(self, *, provider: str, timezone_name: str = "Asia/Tokyo") -> int:
        from zoneinfo import ZoneInfo

        local = datetime.now(ZoneInfo(timezone_name))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_utc = midnight.astimezone(timezone.utc)
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND called_at >= ?",
            (provider, cutoff_utc.isoformat()),
        )
        return cur.fetchone()[0]

    # ----- feed_stats -------------------------------------------------------

    def update_feed_stats(
        self,
        *,
        feed_name: str,
        success: bool,
        items_returned: int = 0,
        items_classified: int = 0,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # UPSERT: insert if missing, update if present.
        if success:
            self.conn.execute(
                """
                INSERT INTO feed_stats (
                    feed_name, last_success_at, items_returned_last_run,
                    items_classified_last_run, consecutive_failures,
                    last_failure_at, last_error
                )
                VALUES (?, ?, ?, ?, 0,
                        (SELECT last_failure_at FROM feed_stats WHERE feed_name = ?),
                        (SELECT last_error      FROM feed_stats WHERE feed_name = ?))
                ON CONFLICT(feed_name) DO UPDATE SET
                    last_success_at           = excluded.last_success_at,
                    items_returned_last_run   = excluded.items_returned_last_run,
                    items_classified_last_run = excluded.items_classified_last_run,
                    consecutive_failures      = 0
                """,
                (feed_name, now, items_returned, items_classified, feed_name, feed_name),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO feed_stats (
                    feed_name, last_failure_at, last_error,
                    items_returned_last_run, items_classified_last_run,
                    consecutive_failures, last_success_at
                )
                VALUES (?, ?, ?, 0, 0, 1, NULL)
                ON CONFLICT(feed_name) DO UPDATE SET
                    last_failure_at           = excluded.last_failure_at,
                    last_error                = excluded.last_error,
                    items_returned_last_run   = 0,
                    items_classified_last_run = 0,
                    consecutive_failures      = feed_stats.consecutive_failures + 1
                """,
                (feed_name, now, error or ""),
            )
        self.conn.commit()

    def all_feed_stats(self) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT feed_name, last_success_at, last_failure_at, last_error,
                   items_returned_last_run, items_classified_last_run,
                   consecutive_failures
            FROM feed_stats
            ORDER BY feed_name
            """
        )
        cols = [
            "feed_name", "last_success_at", "last_failure_at", "last_error",
            "items_returned_last_run", "items_classified_last_run",
            "consecutive_failures",
        ]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def is_first_run(self, *, threshold_hours: int = 6) -> bool:
        """True if `seen` table is empty or no rows collected in last `threshold_hours`."""
        cur = self.conn.execute("SELECT MAX(collected_at) FROM seen")
        row = cur.fetchone()
        last = row[0] if row else None
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt) > timedelta(hours=threshold_hours)

    def db_totals(self) -> dict:
        out = {}
        for window_label, hours in [("24h", 24), ("7d", 24 * 7), ("all", None)]:
            for priority in ("P1", "P2", "P3", "DROPPED"):
                if hours is None:
                    cur = self.conn.execute(
                        "SELECT COUNT(*) FROM seen WHERE priority = ?", (priority,)
                    )
                else:
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                    cur = self.conn.execute(
                        "SELECT COUNT(*) FROM seen WHERE priority = ? AND collected_at > ?",
                        (priority, cutoff.isoformat()),
                    )
                out[f"{priority.lower()}_{window_label}"] = cur.fetchone()[0]
        return out

    def close(self) -> None:
        self.conn.close()
