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


class Collection(BaseModel):
    recency_hours: int = 24
    fetch_concurrency: int = 10


class Scheduler(BaseModel):
    fetch_interval_minutes: int = 60
    p1_batch_interval_hours: int = 3
    digest_cron_hour: int = 7
    digest_cron_minute: int = 0
    timezone: str = "Asia/Tokyo"


class Config(BaseModel):
    sources: list[Source]
    storage: Storage = Field(default_factory=Storage)
    logging: Logging = Field(default_factory=Logging)
    watchlists_path: Path = Path("config/watchlists.yaml")
    relevance_path: Path = Path("config/relevance.yaml")
    query_buckets_path: Path = Path("config/query_buckets.yaml")
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
