from datetime import datetime
from pathlib import Path

from news_agent.sources.nikkei import (
    NikkeiArticle,
    NikkeiResult,
    NikkeiSource,
    _parse_iso,
)


def test_skips_when_anthropic_key_missing(caplog):
    src = NikkeiSource(
        name="Nikkei",
        url="https://www.nikkei.com/business/finance/insurance/",
        anthropic_api_key="",
        nikkei_user="user@example.com",
        nikkei_pass="pass",
    )
    items = src.fetch()
    assert items == []


def test_skips_when_credentials_missing():
    src = NikkeiSource(
        name="Nikkei",
        url="https://www.nikkei.com/business/finance/insurance/",
        anthropic_api_key="sk-test",
        nikkei_user="",
        nikkei_pass="",
    )
    items = src.fetch()
    assert items == []


def test_parse_iso_handles_none():
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_parse_iso_handles_zulu():
    dt = _parse_iso("2026-05-10T12:00:00Z")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_handles_invalid():
    assert _parse_iso("not a date") is None


def test_nikkei_result_default_articles():
    r = NikkeiResult()
    assert r.articles == []
    assert r.error is None


def test_nikkei_article_required_fields():
    a = NikkeiArticle(title="t", url="https://nikkei.com/x")
    assert a.title == "t"
    assert a.published_at is None
    assert a.summary is None


def test_storage_state_path_resolved(tmp_path):
    src = NikkeiSource(
        name="Nikkei",
        url="https://www.nikkei.com/business/finance/insurance/",
        storage_state_path=tmp_path / "state.json",
    )
    assert src.storage_state_path == tmp_path / "state.json"
    assert isinstance(src.storage_state_path, Path)
