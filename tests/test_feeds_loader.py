from pathlib import Path

from news_agent.config import (
    ClaudeResearchQuery,
    ConceptUris,
    Feeds,
    NativeRSSFeed,
    load_concept_uris,
    load_feeds,
)


def test_real_feeds_yaml_parses():
    feeds = load_feeds(Path("config/feeds.yaml"))
    assert isinstance(feeds, Feeds)
    assert len(feeds.native_rss) >= 1
    # Phase 8: claude_research replaced NewsAPI; expect at least one query.
    assert len(feeds.claude_research.queries) >= 1


def test_native_rss_feeds_have_url():
    feeds = load_feeds(Path("config/feeds.yaml"))
    for f in feeds.native_rss:
        assert isinstance(f, NativeRSSFeed)
        assert f.url.startswith("https://")
        assert f.name


def test_claude_research_query_well_formed():
    feeds = load_feeds(Path("config/feeds.yaml"))
    for q in feeds.claude_research.queries:
        assert isinstance(q, ClaudeResearchQuery)
        assert q.name
        assert q.model.startswith("claude-")
        assert q.cadence_hours > 0
        assert q.max_headlines > 0


def test_concept_uris_yaml_parses():
    cu = load_concept_uris(Path("config/concept_uris.yaml"))
    assert isinstance(cu, ConceptUris)
    assert isinstance(cu.resolved, dict)
    assert isinstance(cu.unresolved, list)
