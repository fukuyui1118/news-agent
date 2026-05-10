"""Inoreader API source.

Replaces the public-RSS export with the authenticated REST API for the same
tag streams. The API exposes the article's true publish time (`published`
in unix seconds) and the canonical publisher URL (`canonical[0].href`),
both of which the public RSS hides.

Used for the 14 user-curated keyword tags listed in `feeds.yaml` under
`native_rss`. The build_sources logic in `agent.py` detects URLs of the
form `https://www.inoreader.com/stream/user/<userid>/tag/<tag>` and
instantiates `InoreaderSource` instead of `RSSSource`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import unquote

import structlog

from ..inoreader_oauth import InoreaderAuthError, InoreaderClient
from .base import RawItem, Source

log = structlog.get_logger()

# Match https://www.inoreader.com/stream/user/<userid>/tag/<tag>
_TAG_URL_RE = re.compile(
    r"https?://www\.inoreader\.com/stream/user/(?P<user>\d+)/tag/(?P<tag>.+?)/?$"
)


def parse_tag_url(url: str) -> tuple[str, str] | None:
    """Extract (user_id, tag) from an Inoreader public-share URL.

    The tag in the URL is %-encoded (spaces are %20, etc.). We URL-decode
    here so the API call passes the literal tag string.
    """
    m = _TAG_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group("user"), unquote(m.group("tag"))


class InoreaderSource(Source):
    def __init__(
        self,
        *,
        name: str,
        tag_url: str,
        client: InoreaderClient,
        tier: int = 2,
        max_items: int = 50,
    ) -> None:
        self.name = name
        self.tag_url = tag_url
        self.client = client
        self.tier = tier
        self.max_items = max_items
        parsed = parse_tag_url(tag_url)
        if parsed is None:
            raise ValueError(f"invalid Inoreader tag URL: {tag_url!r}")
        self.user_id, self.tag = parsed

    def fetch(self) -> list[RawItem]:
        try:
            api_items = self.client.fetch_tag(
                self.user_id, self.tag, n=self.max_items
            )
        except InoreaderAuthError as e:
            log.error("inoreader.auth_error", source=self.name, error=str(e))
            return []

        items: list[RawItem] = []
        for ai in api_items:
            url = _pick_canonical_url(ai)
            title = (ai.get("title") or "").strip()
            if not url or not title:
                continue

            published_at = _published_to_datetime(ai.get("published"))
            summary = ((ai.get("summary") or {}).get("content") or "").strip()
            origin_title = ((ai.get("origin") or {}).get("title") or "").strip()
            # Use the publisher's name as the per-item source field; falls back
            # to the feed name. Lets the dashboard show "朝日新聞" instead of
            # "Inoreader: 朝日生命".
            source = origin_title or self.name

            items.append(
                RawItem(
                    url=url,
                    title=title,
                    published_at=published_at,
                    source=source,
                    raw_text=summary,
                    source_tier=self.tier,
                )
            )

        log.info(
            "inoreader.fetch.success",
            source=self.name,
            tag=self.tag,
            api_items=len(api_items),
            kept=len(items),
        )
        return items


def _pick_canonical_url(api_item: dict) -> str:
    """Prefer `canonical[0].href` (publisher URL) over `alternate[0].href`
    (Google News wrapper)."""
    for key in ("canonical", "alternate"):
        arr = api_item.get(key)
        if isinstance(arr, list) and arr:
            href = (arr[0] or {}).get("href")
            if href:
                return href.strip()
    return ""


def _published_to_datetime(value) -> datetime | None:
    """Convert Inoreader's `published` (unix seconds, int or numeric str)
    to a UTC `datetime`. Returns None if missing / unparseable."""
    if value is None or value == "" or value == 0:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
