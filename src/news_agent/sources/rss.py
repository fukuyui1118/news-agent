from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from .base import RawItem, Source


class RSSSource(Source):
    def __init__(
        self,
        name: str,
        url: str,
        tier: int = 3,
        trust_freshness: bool = False,
    ) -> None:
        self.name = name
        self.url = url
        self.tier = tier
        self.trust_freshness = trust_freshness

    def fetch(self) -> list[RawItem]:
        parsed = feedparser.parse(self.url)
        items: list[RawItem] = []
        fetch_time = datetime.now(timezone.utc) if self.trust_freshness else None
        for entry in parsed.entries:
            url = getattr(entry, "link", None)
            title = getattr(entry, "title", None)
            if not url or not title:
                continue
            pub = _parse_date(entry)
            if pub is None and self.trust_freshness:
                pub = fetch_time
            items.append(
                RawItem(
                    url=url,
                    title=title.strip(),
                    published_at=pub,
                    source=self.name,
                    raw_text=_extract_text(entry),
                    source_tier=self.tier,
                )
            )
        return items


def _parse_date(entry) -> datetime | None:
    for key in ("published", "updated"):
        value = getattr(entry, key, None)
        if value:
            try:
                return parsedate_to_datetime(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_text(entry) -> str:
    parts: list[str] = []
    for key in ("summary", "description"):
        value = getattr(entry, key, None)
        if value:
            parts.append(value)
    content = getattr(entry, "content", None)
    if content:
        for c in content:
            v = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
            if v:
                parts.append(v)
    return "\n".join(parts)
