from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from news_agent.sources.claude_research import (
    ClaudeResearchSource,
    _parse_iso,
    _strip_json_fences,
)
from news_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


# ---- helpers --------------------------------------------------------------


def test_strip_json_fences_with_json_label():
    assert _strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_json_fences_no_label():
    assert _strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_json_fences_no_fences():
    assert _strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_parse_iso_zulu():
    dt = _parse_iso("2026-05-10T12:00:00Z")
    assert dt is not None and dt.year == 2026


def test_parse_iso_none():
    assert _parse_iso(None) is None


# ---- skip paths -----------------------------------------------------------


def test_skips_when_no_api_key(store):
    src = ClaudeResearchSource(name="x", api_key="", store=store)
    items = src.fetch()
    assert items == []


def test_skips_within_cadence(store):
    # Pre-populate api_usage with a successful call 1h ago.
    store.conn.execute(
        """
        INSERT INTO api_usage (called_at, provider, endpoint, query_name, error)
        VALUES (?, 'anthropic', 'getArticles_research', 'q', NULL)
        """,
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
    )
    store.conn.commit()
    src = ClaudeResearchSource(name="q", api_key="dummy", cadence_hours=12, store=store)
    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        items = src.fetch()
    mock_anth.assert_not_called()  # never instantiated
    assert items == []


def test_runs_when_outside_cadence(store):
    # Pre-populate api_usage with a successful call 13h ago.
    store.conn.execute(
        """
        INSERT INTO api_usage (called_at, provider, endpoint, query_name, error)
        VALUES (?, 'anthropic', 'getArticles_research', 'q', NULL)
        """,
        ((datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),),
    )
    store.conn.commit()
    src = ClaudeResearchSource(name="q", api_key="dummy", cadence_hours=12, store=store)

    fake_text_block = MagicMock(type="text")
    fake_text_block.text = (
        '{"headlines":[{"title":"t","url":"https://x","source":"s",'
        '"published_at":"2026-05-10T00:00:00Z","summary_ja":"要約"}]}'
    )
    fake_resp = MagicMock(content=[fake_text_block])

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_resp
        mock_anth.return_value = mock_client
        items = src.fetch()
    assert len(items) == 1
    assert items[0].title == "t"
    assert items[0].url == "https://x"
    assert items[0].source == "q"
    assert items[0].source_tier == 1
    # api_usage row recorded
    cur = store.conn.execute(
        "SELECT COUNT(*) FROM api_usage WHERE query_name='q' AND http_status=200"
    )
    assert cur.fetchone()[0] >= 1


def test_handles_anthropic_error(store):
    src = ClaudeResearchSource(name="q", api_key="dummy", cadence_hours=12, store=store)
    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("boom")
        mock_anth.return_value = mock_client
        items = src.fetch()
    assert items == []
    cur = store.conn.execute(
        "SELECT error FROM api_usage WHERE query_name='q' ORDER BY id DESC LIMIT 1"
    )
    error = cur.fetchone()[0]
    assert "RuntimeError" in error


def test_handles_invalid_json(store):
    src = ClaudeResearchSource(name="q", api_key="dummy", cadence_hours=12, store=store)
    bad_text = MagicMock(type="text")
    bad_text.text = "not json"
    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(content=[bad_text])
        mock_anth.return_value = mock_client
        items = src.fetch()
    assert items == []
