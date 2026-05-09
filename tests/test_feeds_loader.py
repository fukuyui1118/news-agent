from pathlib import Path

from news_agent.config import (
    ConceptUris,
    Feeds,
    NativeRSSFeed,
    NewsApiQuery,
    load_concept_uris,
    load_feeds,
)


def test_real_feeds_yaml_parses():
    feeds = load_feeds(Path("config/feeds.yaml"))
    assert isinstance(feeds, Feeds)
    assert len(feeds.native_rss) >= 1
    assert len(feeds.newsapi.queries) == 6


def test_native_rss_feeds_have_url():
    feeds = load_feeds(Path("config/feeds.yaml"))
    for f in feeds.native_rss:
        assert isinstance(f, NativeRSSFeed)
        assert f.url.startswith("https://")
        assert f.name


def test_newsapi_queries_well_formed():
    feeds = load_feeds(Path("config/feeds.yaml"))
    names = [q.name for q in feeds.newsapi.queries]
    # Per the agreed plan: 6 queries, two of each language pattern
    assert "P1 Japan EN" in names
    assert "P1 Japan JP" in names
    assert "Sector EN" in names
    assert "Sector JP" in names
    for q in feeds.newsapi.queries:
        assert isinstance(q, NewsApiQuery)
        assert q.lang in ("eng", "jpn")
        assert q.sort_by in ("date", "rel", "sourceImportance")
        assert q.articles_count <= 100
        # At least one of conceptUris or keyword_fallback must be non-empty
        assert q.concept_uri_keys or q.keyword_fallback


def test_newsapi_caps_present():
    feeds = load_feeds(Path("config/feeds.yaml"))
    assert feeds.newsapi.monthly_cap == 4800
    assert feeds.newsapi.per_cycle_hard_cap == 8
    assert feeds.newsapi.daily_soft_warning == 200


def test_concept_uris_yaml_parses():
    cu = load_concept_uris(Path("config/concept_uris.yaml"))
    assert isinstance(cu, ConceptUris)
    # Empty until resolve_concept_uris.py runs — that's fine
    assert isinstance(cu.resolved, dict)
    assert isinstance(cu.unresolved, list)
