from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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
    dropped_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_priority_emailed
    ON seen(priority, emailed_at);
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
        self.conn = sqlite3.connect(db_path)
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

    def insert_if_new(
        self,
        *,
        url: str,
        title: str,
        source: str,
        published_at: datetime | None,
        priority: str,
        dropped_reason: str | None = None,
    ) -> str | None:
        """Insert a row. Returns the url_hash on insert, or None if duplicate."""
        canonical = canonicalize_url(url)
        h = hash_item(canonical, title)
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO seen
                (url_hash, url, title, source, collected_at, published_at, priority, dropped_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

    def close(self) -> None:
        self.conn.close()
