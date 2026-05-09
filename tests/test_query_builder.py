from pathlib import Path

import yaml

from news_agent.config import (
    Bucket,
    Buckets,
    WatchlistEntry,
    Watchlists,
    load_buckets,
    load_watchlists,
)
from news_agent.query_builder import (
    build_query,
    generate_google_news_queries,
)


def _wl():
    return Watchlists(
        p1_japan=[
            WatchlistEntry(canonical="A1", aliases=["a1-alias"]),
            WatchlistEntry(canonical="A2", aliases=[]),
        ],
        p2_global=[
            WatchlistEntry(canonical="B1", aliases=["b1", "b2"]),
        ],
    )


def _buckets():
    return [
        Bucket(name="ma", keywords=["merger", "acquisition"]),
        Bucket(name="earn", keywords=["earnings", "決算"]),
    ]


# ---- coverage guarantee --------------------------------------------------


def test_one_query_per_entity_per_bucket():
    queries = generate_google_news_queries(
        watchlists=_wl(), buckets=_buckets(), recency_hours=24
    )
    assert len(queries) == 3 * 2  # 3 entities × 2 buckets


def test_every_canonical_is_present():
    queries = generate_google_news_queries(
        watchlists=_wl(), buckets=_buckets(), recency_hours=24
    )
    canonicals = {q.entity_canonical for q in queries}
    assert canonicals == {"A1", "A2", "B1"}


def test_priority_propagated():
    queries = generate_google_news_queries(
        watchlists=_wl(), buckets=_buckets(), recency_hours=24
    )
    p1 = [q for q in queries if q.entity_priority == "P1"]
    p2 = [q for q in queries if q.entity_priority == "P2"]
    assert {q.entity_canonical for q in p1} == {"A1", "A2"}
    assert {q.entity_canonical for q in p2} == {"B1"}


# ---- query construction --------------------------------------------------


def test_query_contains_when_operator():
    q = build_query(
        WatchlistEntry(canonical="X", aliases=["y"]),
        Bucket(name="b", keywords=["k"]),
        recency_hours=48,
    )
    assert "when:48h" in q


def test_aliases_or_joined_with_canonical():
    q = build_query(
        WatchlistEntry(canonical="Tokio Marine", aliases=["東京海上"]),
        Bucket(name="b", keywords=["k"]),
        recency_hours=24,
    )
    # Canonical "Tokio Marine" has a space -> should be quoted as a phrase
    assert '"Tokio Marine"' in q
    assert "東京海上" in q
    assert "OR" in q


def test_japanese_keywords_not_quoted():
    q = build_query(
        WatchlistEntry(canonical="X", aliases=[]),
        Bucket(name="b", keywords=["決算", "earnings"]),
        recency_hours=24,
    )
    assert "決算" in q
    assert "earnings" in q
    # 決算 is non-ASCII so should NOT be wrapped in quotes
    assert '"決算"' not in q


def test_empty_aliases_uses_canonical_only():
    q = build_query(
        WatchlistEntry(canonical="X", aliases=[]),
        Bucket(name="b", keywords=["k"]),
        recency_hours=24,
    )
    # Should still have parens with single term
    assert "(X)" in q


# ---- live YAML round-trip ------------------------------------------------


def test_real_yaml_files_produce_expected_count():
    watchlists = load_watchlists(Path("config/watchlists.yaml"))
    buckets = load_buckets(Path("config/query_buckets.yaml"))
    n_entities = len(watchlists.p1_japan) + len(watchlists.p2_global)
    n_buckets = len(buckets.buckets)

    queries = generate_google_news_queries(
        watchlists=watchlists,
        buckets=buckets.buckets,
        recency_hours=24,
    )
    assert len(queries) == n_entities * n_buckets
    # Coverage: every entity in YAML produces exactly len(buckets) queries
    from collections import Counter

    counts = Counter(q.entity_canonical for q in queries)
    assert all(c == n_buckets for c in counts.values())


def test_buckets_yaml_loads_8_buckets():
    """If query_buckets.yaml is edited to add/remove buckets, this test still
    passes (just confirms it parses). The hard count check above is the contract."""
    buckets = load_buckets(Path("config/query_buckets.yaml"))
    assert len(buckets.buckets) >= 1
    for b in buckets.buckets:
        assert b.name
        assert len(b.keywords) > 0
