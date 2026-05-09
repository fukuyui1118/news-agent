from pathlib import Path

from news_agent.config import (
    TopicQueries,
    TopicQuery,
    load_topic_queries,
)


# ---- YAML round-trip ------------------------------------------------------


def test_real_yaml_loads_and_parses():
    tq = load_topic_queries(Path("config/topic_queries.yaml"))
    assert isinstance(tq, TopicQueries)
    assert len(tq.queries) >= 1
    for q in tq.queries:
        assert q.name
        assert q.query
        assert isinstance(q.tier, int)


def test_real_yaml_contains_japanese_industry_query():
    tq = load_topic_queries(Path("config/topic_queries.yaml"))
    names = [q.name for q in tq.queries]
    assert any("保険業界" in n for n in names)


def test_real_yaml_contains_global_industry_query():
    tq = load_topic_queries(Path("config/topic_queries.yaml"))
    names_lower = [q.name.lower() for q in tq.queries]
    assert any("insurance industry" in n for n in names_lower)


# ---- shape ----------------------------------------------------------------


def test_topic_query_minimal():
    q = TopicQuery(name="x", query="foo OR bar")
    assert q.tier == 3  # default


def test_topic_queries_container():
    tq = TopicQueries(
        queries=[
            TopicQuery(name="a", query="q1"),
            TopicQuery(name="b", query="q2", tier=2),
        ]
    )
    assert len(tq.queries) == 2
    assert tq.queries[1].tier == 2
