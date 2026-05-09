"""NewsAPI.ai (Event Registry) source adapter.

One source = one query (e.g. "P1 Japan EN"). Each cycle, the agent constructs
the dateStart/dateEnd window (1h normal, 24h on first run) and calls
getArticles. Results map to RawItem.

Concept URIs (e.g. http://en.wikipedia.org/wiki/Tokio_Marine_Holdings) match
specific entities far more accurately than keywords. The query mixes
conceptUris (for resolved entities) with keyword fallback (for entities that
didn't resolve) — both fields are sent and the API combines them.

Budget enforcement happens via BudgetGuard at the agent level — this source
just makes the request and reports article_count.
"""
from __future__ import annotations

import json
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx
import structlog

from .base import RawItem, Source

log = structlog.get_logger()

ENDPOINT = "https://eventregistry.org/api/v1/article/getArticles"
DEFAULT_TIMEOUT = 30.0


class NewsApiSource(Source):
    """One configured NewsAPI.ai query."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        lang: str,                       # "eng" or "jpn"
        concept_uris: list[str] | None = None,
        keywords: list[str] | None = None,
        keyword_oper: str = "or",        # currently unused; OR is implicit
        articles_count: int = 100,
        articles_sort_by: str = "date",  # "date" | "rel" | "sourceImportance"
        date_start: str | None = None,   # "YYYY-MM-DD"; set per-cycle by agent
        date_end: str | None = None,
        category_uri: str | None = "dmoz/Business/Financial_Services/Insurance",
        tier: int = 2,
        budget=None,                     # injected by agent
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.tier = tier
        self.api_key = api_key
        self.lang = lang
        self.concept_uris = list(concept_uris or [])
        self.keywords = list(keywords or [])
        self.keyword_oper = keyword_oper
        self.articles_count = articles_count
        self.articles_sort_by = articles_sort_by
        self.date_start = date_start
        self.date_end = date_end
        self.category_uri = category_uri
        self.budget = budget
        self.timeout = timeout

    def _build_query_body(self) -> dict:
        """Build Event Registry's nested query.

        Shape:
          $query:
            $and:
              - $or: [{conceptUri: A}, {conceptUri: B}, {keyword: K}, ...]
              - categoryUri: dmoz/.../Insurance     # only if category_uri set
            lang: eng | jpn
            dateStart: YYYY-MM-DD
            dateEnd:   YYYY-MM-DD

        OR-join of multi-value fields is via {$or: [{field: v1}, ...]}.
        Category filter narrows aggressively (e.g. drops Bayern Munich /
        Allianz Arena coverage from Allianz queries).
        """
        or_clauses: list[dict] = []
        for uri in self.concept_uris:
            or_clauses.append({"conceptUri": uri})
        for kw in self.keywords:
            or_clauses.append({"keyword": kw})

        and_clauses: list[dict] = []
        if or_clauses:
            and_clauses.append({"$or": or_clauses})
        if self.category_uri:
            and_clauses.append({"categoryUri": self.category_uri})

        query_filter: dict = {}
        if len(and_clauses) > 1:
            query_filter["$and"] = and_clauses
        elif and_clauses:
            query_filter.update(and_clauses[0])

        query_filter["lang"] = self.lang
        if self.date_start:
            query_filter["dateStart"] = self.date_start
        if self.date_end:
            query_filter["dateEnd"] = self.date_end

        return {
            "action": "getArticles",
            "query": {"$query": query_filter},
            "resultType": "articles",
            "articlesPage": 1,
            "articlesCount": self.articles_count,
            "articlesSortBy": self.articles_sort_by,
            "includeArticleEventUri": False,
            "includeArticleConcepts": False,
            "includeArticleCategories": False,
            "includeArticleSocialScore": False,
            "apiKey": self.api_key,
        }

    def fetch(self) -> list[RawItem]:
        if not self.api_key:
            log.warning("newsapi.skipped", name=self.name, reason="no_api_key")
            return []
        if not self.concept_uris and not self.keywords:
            log.warning("newsapi.skipped", name=self.name, reason="empty_query")
            return []

        body = self._build_query_body()

        if self.budget is not None:
            from ..budget import BudgetExceeded

            try:
                with self.budget.guard(endpoint="getArticles", query_name=self.name) as record:
                    items = self._call_and_parse(body, record)
            except BudgetExceeded as e:
                log.warning("newsapi.budget_skip", name=self.name, reason=str(e))
                return []
        else:
            items = self._call_and_parse(body, lambda **kw: None)
        return items

    def _call_and_parse(self, body: dict, record) -> list[RawItem]:
        try:
            resp = httpx.post(ENDPOINT, json=body, timeout=self.timeout)
            status = resp.status_code
            if status != 200:
                record(http_status=status, error=f"http_{status}")
                log.error(
                    "newsapi.http_error",
                    name=self.name,
                    status=status,
                    body_preview=resp.text[:200],
                )
                return []
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            record(error=str(e))
            log.error("newsapi.request_failed", name=self.name, error=str(e))
            return []

        articles = (data.get("articles") or {}).get("results") or []
        record(article_count=len(articles), http_status=200)

        items: list[RawItem] = []
        for art in articles:
            url = art.get("url") or ""
            title = art.get("title") or ""
            if not url or not title:
                continue
            items.append(
                RawItem(
                    url=url,
                    title=title.strip(),
                    published_at=_parse_pubdate(art),
                    source=self.name,
                    raw_text=(art.get("body") or "")[:2000],
                    source_tier=self.tier,
                )
            )
        return items


def _parse_pubdate(article: dict) -> datetime | None:
    """NewsAPI.ai returns date and time fields. Combine into ISO8601 UTC.

    Common shape: {"date": "2026-05-10", "time": "12:34:56", "dateTime": "2026-05-10T12:34:56Z"}
    """
    dt_str = article.get("dateTime") or article.get("dateTimePub")
    if dt_str:
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    # Fallback: combine date + time.
    date = article.get("date")
    time = article.get("time")
    if date:
        candidate = f"{date}T{time or '00:00:00'}+00:00"
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None
