from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Source(BaseModel):
    name: str
    type: Literal["rss", "html", "browser_use", "google_news_rss"]
    url: str = ""
    query: str = ""  # for google_news_rss
    tier: int = 3
    enabled: bool = True


class Storage(BaseModel):
    db_path: Path = Path("seen.db")


class Logging(BaseModel):
    log_path: Path = Path("logs/agent.log")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class WatchlistEntry(BaseModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class Watchlists(BaseModel):
    p1_japan: list[WatchlistEntry]
    p2_global: list[WatchlistEntry]


class Relevance(BaseModel):
    business_keywords: list[str]


class Bucket(BaseModel):
    name: str
    keywords: list[str]


class Buckets(BaseModel):
    buckets: list[Bucket]


class TopicQuery(BaseModel):
    name: str
    query: str
    tier: int = 3


class TopicQueries(BaseModel):
    queries: list[TopicQuery]


# ----- Phase 7: feeds.yaml + concept_uris.yaml -----------------------------


class NativeRSSFeed(BaseModel):
    name: str
    url: str
    tier: int = 2
    # Some feeds (e.g. Nikkei Asia RDF/RSS 1.0) ship no per-item pubdate.
    # When trust_freshness is true, items without a parseable pubdate are
    # tagged as "fresh at fetch time" so they survive the 24h filter.
    # Safe because URL and content-hash dedup catch re-publications.
    trust_freshness: bool = False


class NewsApiQuery(BaseModel):
    name: str
    lang: Literal["eng", "jpn"]
    sort_by: Literal["date", "rel", "sourceImportance"] = "date"
    articles_count: int = 100
    concept_uri_keys: list[str] = Field(default_factory=list)
    keyword_fallback: list[str] = Field(default_factory=list)
    keyword_oper: Literal["or", "and"] = "or"
    tier: int = 2


class NewsApiConfig(BaseModel):
    endpoint: str = "https://eventregistry.org/api/v1/article/getArticles"
    monthly_cap: int = 4800
    per_cycle_hard_cap: int = 8
    daily_soft_warning: int = 200
    timezone: str = "Asia/Tokyo"
    queries: list[NewsApiQuery] = Field(default_factory=list)


class ClaudeResearchQuery(BaseModel):
    name: str
    model: str = "claude-opus-4-7"
    cadence_hours: int = 12
    tier: int = 1
    max_headlines: int = 30
    max_search_uses: int = 12


class ClaudeResearchConfig(BaseModel):
    monthly_token_cap: int = 2_000_000   # rough $60 ceiling at Opus rates
    queries: list[ClaudeResearchQuery] = Field(default_factory=list)


class Feeds(BaseModel):
    native_rss: list[NativeRSSFeed] = Field(default_factory=list)
    newsapi: NewsApiConfig = Field(default_factory=NewsApiConfig)
    claude_research: ClaudeResearchConfig = Field(default_factory=ClaudeResearchConfig)


class ConceptUris(BaseModel):
    resolved: dict[str, str] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class Collection(BaseModel):
    recency_hours: int = 24
    fetch_concurrency: int = 10


class Scheduler(BaseModel):
    digest_cron_hours: str = "7,19"   # comma-separated hour list for the cron trigger
    digest_cron_minute: int = 0
    timezone: str = "Asia/Tokyo"


class Config(BaseModel):
    # Phase 7: sources moved to feeds.yaml. Kept here for backwards compat
    # with older config.yaml files; defaults to empty list.
    sources: list[Source] = Field(default_factory=list)
    storage: Storage = Field(default_factory=Storage)
    logging: Logging = Field(default_factory=Logging)
    watchlists_path: Path = Path("config/watchlists.yaml")
    relevance_path: Path = Path("config/relevance.yaml")
    feeds_path: Path = Path("config/feeds.yaml")
    concept_uris_path: Path = Path("config/concept_uris.yaml")
    scheduler: Scheduler = Field(default_factory=Scheduler)
    collection: Collection = Field(default_factory=Collection)


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    anthropic_api_key: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""
    nikkei_user: str = ""
    nikkei_pass: str = ""
    browser_use_model: str = "claude-sonnet-4-5"
    newsapi_ai_key: str = ""


def load_config(path: Path = Path("config/config.yaml")) -> Config:
    with open(path) as f:
        return Config.model_validate(yaml.safe_load(f))


def load_watchlists(path: Path) -> Watchlists:
    with open(path) as f:
        return Watchlists.model_validate(yaml.safe_load(f))


def load_relevance(path: Path) -> Relevance:
    with open(path) as f:
        return Relevance.model_validate(yaml.safe_load(f))


def load_buckets(path: Path) -> Buckets:
    with open(path) as f:
        return Buckets.model_validate(yaml.safe_load(f))


def load_topic_queries(path: Path) -> TopicQueries:
    with open(path) as f:
        return TopicQueries.model_validate(yaml.safe_load(f))


def load_feeds(path: Path) -> Feeds:
    with open(path) as f:
        return Feeds.model_validate(yaml.safe_load(f))


def load_concept_uris(path: Path) -> ConceptUris:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
        return ConceptUris.model_validate(data)
