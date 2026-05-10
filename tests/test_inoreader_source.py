from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from news_agent.inoreader_oauth import InoreaderAuthError, InoreaderClient
from news_agent.sources.inoreader import (
    InoreaderSource,
    _published_to_datetime,
    parse_tag_url,
)


# ---- parse_tag_url ----


def test_parse_tag_url_simple():
    out = parse_tag_url("https://www.inoreader.com/stream/user/1003696320/tag/asahi")
    assert out == ("1003696320", "asahi")


def test_parse_tag_url_decodes_url_encoded_tag():
    out = parse_tag_url(
        "https://www.inoreader.com/stream/user/1003696320/tag/Generic%20Japanese"
    )
    assert out == ("1003696320", "Generic Japanese")


def test_parse_tag_url_strips_trailing_slash():
    out = parse_tag_url("https://www.inoreader.com/stream/user/123/tag/foo/")
    assert out == ("123", "foo")


def test_parse_tag_url_returns_none_on_other_url():
    assert parse_tag_url("https://www.example.com/feed/rss") is None
    assert parse_tag_url("https://www.inoreader.com/folder/abc") is None


# ---- _published_to_datetime ----


def test_published_to_datetime_unix_seconds():
    dt = _published_to_datetime(1715291432)
    assert dt is not None
    assert dt.tzinfo is timezone.utc
    assert dt.year == 2024


def test_published_to_datetime_string_value():
    dt = _published_to_datetime("1715291432")
    assert dt is not None and dt.tzinfo is timezone.utc


def test_published_to_datetime_zero_returns_none():
    assert _published_to_datetime(0) is None
    assert _published_to_datetime("0") is None


def test_published_to_datetime_garbage_returns_none():
    assert _published_to_datetime("abc") is None
    assert _published_to_datetime(None) is None
    assert _published_to_datetime("") is None


# ---- InoreaderSource.fetch ----


def _client_returning(items: list[dict]) -> InoreaderClient:
    c = MagicMock(spec=InoreaderClient)
    c.fetch_tag.return_value = items
    return c


def test_inoreader_source_constructor_rejects_bad_url():
    client = MagicMock(spec=InoreaderClient)
    with pytest.raises(ValueError):
        InoreaderSource(
            name="bad", tag_url="https://example.com/feed", client=client, tier=2
        )


def test_inoreader_source_fetch_maps_canonical_url_and_published_at():
    client = _client_returning([
        {
            "id": "1",
            "title": "Tokio Marine Q1 earnings beat",
            "published": 1715291432,
            "canonical": [{"href": "https://www.asahi.com/articles/x"}],
            "alternate": [{"href": "https://news.google.com/rss/articles/CBM..."}],
            "summary": {"content": "summary text"},
            "origin": {"title": "朝日新聞"},
        },
    ])
    src = InoreaderSource(
        name="Inoreader: 朝日生命",
        tag_url="https://www.inoreader.com/stream/user/123/tag/asahi",
        client=client,
        tier=2,
    )
    items = src.fetch()
    assert len(items) == 1
    item = items[0]
    # canonical takes precedence over alternate (which is the Google News wrapper)
    assert item.url == "https://www.asahi.com/articles/x"
    assert item.title == "Tokio Marine Q1 earnings beat"
    assert item.published_at is not None and item.published_at.tzinfo is timezone.utc
    assert item.source == "朝日新聞"  # from origin.title
    assert item.raw_text == "summary text"
    assert item.source_tier == 2

    # Client was called with the correct user_id and decoded tag.
    client.fetch_tag.assert_called_once_with("123", "asahi", n=50)


def test_inoreader_source_falls_back_to_alternate_when_canonical_missing():
    client = _client_returning([
        {
            "title": "X",
            "published": 1715291432,
            "alternate": [{"href": "https://news.google.com/rss/articles/CBMx"}],
            "origin": {"title": "Reuters"},
        },
    ])
    src = InoreaderSource(
        name="t", tag_url="https://www.inoreader.com/stream/user/1/tag/t",
        client=client, tier=2,
    )
    items = src.fetch()
    assert len(items) == 1
    assert items[0].url.startswith("https://news.google.com/")


def test_inoreader_source_skips_items_missing_url_or_title():
    client = _client_returning([
        {"title": "no url"},
        {"canonical": [{"href": "https://x"}]},  # missing title
        {"title": "ok", "canonical": [{"href": "https://ok"}]},
    ])
    src = InoreaderSource(
        name="t", tag_url="https://www.inoreader.com/stream/user/1/tag/t",
        client=client, tier=2,
    )
    items = src.fetch()
    assert len(items) == 1
    assert items[0].title == "ok"


def test_inoreader_source_returns_empty_on_auth_error():
    client = MagicMock(spec=InoreaderClient)
    client.fetch_tag.side_effect = InoreaderAuthError("token revoked")
    src = InoreaderSource(
        name="t", tag_url="https://www.inoreader.com/stream/user/1/tag/t",
        client=client, tier=2,
    )
    assert src.fetch() == []


def test_inoreader_source_falls_back_to_feed_name_when_origin_missing():
    client = _client_returning([
        {
            "title": "headline",
            "published": 1715291432,
            "canonical": [{"href": "https://x.com/y"}],
            # no origin field
        },
    ])
    src = InoreaderSource(
        name="Inoreader: foo",
        tag_url="https://www.inoreader.com/stream/user/1/tag/foo",
        client=client, tier=2,
    )
    items = src.fetch()
    assert items[0].source == "Inoreader: foo"
