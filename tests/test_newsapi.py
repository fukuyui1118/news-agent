from unittest.mock import MagicMock, patch

import pytest

from news_agent.budget import BudgetConfig, BudgetGuard
from news_agent.sources.newsapi import NewsApiSource, _parse_pubdate
from news_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def budget(store):
    return BudgetGuard(
        config=BudgetConfig(monthly_cap=100, per_cycle_hard_cap=10),
        store=store,
    )


# --- query body construction -----------------------------------------------


def test_body_includes_concept_uris_and_keywords():
    src = NewsApiSource(
        name="P1 Japan EN",
        api_key="dummy",
        lang="eng",
        concept_uris=["http://en.wikipedia.org/wiki/Tokio_Marine_Holdings"],
        keywords=["fallback term"],
        date_start="2026-05-09",
        date_end="2026-05-10",
    )
    body = src._build_query_body()
    qf = body["query"]["$query"]
    assert qf["lang"] == "eng"
    assert qf["dateStart"] == "2026-05-09"
    # New shape: $and[ {$or: [{conceptUri:..}, {keyword:..}]}, {categoryUri:..} ]
    and_block = qf["$and"]
    or_block = next(c for c in and_block if "$or" in c)
    or_clauses = or_block["$or"]
    assert {"conceptUri": "http://en.wikipedia.org/wiki/Tokio_Marine_Holdings"} in or_clauses
    assert {"keyword": "fallback term"} in or_clauses
    cat_block = next(c for c in and_block if "categoryUri" in c)
    assert "Insurance" in cat_block["categoryUri"]
    assert body["articlesCount"] == 100
    assert body["articlesSortBy"] == "date"
    assert body["apiKey"] == "dummy"


def test_body_no_category_when_disabled():
    src = NewsApiSource(
        name="x", api_key="dummy", lang="eng",
        concept_uris=["uri1"], category_uri=None,
    )
    body = src._build_query_body()
    qf = body["query"]["$query"]
    # Without category, no $and wrapper — single $or block at top
    assert "$and" not in qf
    assert qf["$or"] == [{"conceptUri": "uri1"}]


def test_body_combines_uris_and_keywords_in_or():
    src = NewsApiSource(
        name="x", api_key="dummy", lang="eng",
        concept_uris=["u1", "u2"], keywords=["k1"], category_uri=None,
    )
    body = src._build_query_body()
    or_clauses = body["query"]["$query"]["$or"]
    assert len(or_clauses) == 3
    assert {"conceptUri": "u1"} in or_clauses
    assert {"conceptUri": "u2"} in or_clauses
    assert {"keyword": "k1"} in or_clauses


def test_no_key_returns_empty():
    src = NewsApiSource(
        name="x", api_key="", lang="eng", concept_uris=["uri"], keywords=[]
    )
    assert src.fetch() == []


def test_empty_query_returns_empty():
    src = NewsApiSource(
        name="x", api_key="dummy", lang="eng", concept_uris=[], keywords=[]
    )
    assert src.fetch() == []


# --- live fetch (mocked httpx) ---------------------------------------------


def _fake_response(status_code=200, articles=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = {
        "articles": {"results": articles or []}
    }
    return resp


def test_fetch_parses_articles(budget):
    sample_article = {
        "title": "Tokio Marine reports Q4 earnings beat",
        "url": "https://example.com/article-1",
        "body": "Tokio Marine Holdings posted record profit...",
        "dateTime": "2026-05-10T12:00:00Z",
    }
    src = NewsApiSource(
        name="P1 Japan EN",
        api_key="dummy",
        lang="eng",
        concept_uris=["http://en.wikipedia.org/wiki/Tokio_Marine_Holdings"],
        budget=budget,
    )
    with patch("news_agent.sources.newsapi.httpx.post") as mock_post:
        mock_post.return_value = _fake_response(200, [sample_article])
        items = src.fetch()
    assert len(items) == 1
    assert items[0].title == "Tokio Marine reports Q4 earnings beat"
    assert items[0].url == "https://example.com/article-1"
    assert items[0].source == "P1 Japan EN"
    assert items[0].source_tier == 2


def test_fetch_handles_non_200(budget):
    src = NewsApiSource(
        name="P1 Japan EN", api_key="dummy", lang="eng",
        concept_uris=["uri"], budget=budget,
    )
    with patch("news_agent.sources.newsapi.httpx.post") as mock_post:
        mock_post.return_value = _fake_response(429)
        mock_post.return_value.text = "rate limited"
        items = src.fetch()
    assert items == []


def test_fetch_handles_empty_results(budget):
    src = NewsApiSource(
        name="P1 Japan EN", api_key="dummy", lang="eng",
        concept_uris=["uri"], budget=budget,
    )
    with patch("news_agent.sources.newsapi.httpx.post") as mock_post:
        mock_post.return_value = _fake_response(200, [])
        items = src.fetch()
    assert items == []


def test_fetch_skipped_when_budget_exhausted(store):
    cfg = BudgetConfig(monthly_cap=10, per_cycle_hard_cap=1)
    b = BudgetGuard(config=cfg, store=store)
    # Burn the per-cycle cap
    with b.guard(endpoint="getArticles") as record:
        record(article_count=1, http_status=200)
    src = NewsApiSource(
        name="P1 Japan EN", api_key="dummy", lang="eng",
        concept_uris=["uri"], budget=b,
    )
    with patch("news_agent.sources.newsapi.httpx.post") as mock_post:
        items = src.fetch()
    mock_post.assert_not_called()
    assert items == []


# --- pubdate parsing -------------------------------------------------------


def test_parse_pubdate_dateTime():
    dt = _parse_pubdate({"dateTime": "2026-05-10T12:00:00Z"})
    assert dt is not None and dt.year == 2026


def test_parse_pubdate_date_time_split():
    dt = _parse_pubdate({"date": "2026-05-10", "time": "12:34:56"})
    assert dt is not None and dt.hour == 12


def test_parse_pubdate_missing():
    assert _parse_pubdate({}) is None
