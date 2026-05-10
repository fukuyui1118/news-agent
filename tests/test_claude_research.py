from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from news_agent.config import WatchlistEntry, Watchlists
from news_agent.sources.claude_research import (
    ClaudeResearchSource,
    _parse_coverage_notes,
    _parse_iso,
    _strip_json_fences,
)
from news_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def watchlists():
    return Watchlists(
        p1_japan=[
            WatchlistEntry(canonical="Tokio Marine"),
            WatchlistEntry(canonical="MS&AD"),
        ],
        p2_global=[
            WatchlistEntry(canonical="Munich Re"),
            WatchlistEntry(canonical="Allianz"),
        ],
    )


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


def test_parse_coverage_notes_full_block():
    text = (
        "- some headline\n\n"
        "COVERAGE_NOTES:\n"
        "  searches_run: 11\n"
        "  tier1_aggregators_hit: TDnet, FSA, AM Best\n"
        "  fallback_used: false\n"
        "  gaps: 特になし\n"
    )
    cov = _parse_coverage_notes(text)
    assert cov["searches_run"] == 11
    assert cov["tier1_aggregators_hit"] == 3
    assert cov["fallback_used"] is False
    assert cov["gaps"] == "特になし"
    assert "searches_run: 11" in cov["raw"]


def test_parse_coverage_notes_fallback_true():
    text = "COVERAGE_NOTES:\nfallback_used: true\nsearches_run: 4\n"
    cov = _parse_coverage_notes(text)
    assert cov["fallback_used"] is True
    assert cov["searches_run"] == 4


def test_parse_coverage_notes_missing_block():
    cov = _parse_coverage_notes("just bullets, no notes")
    assert cov["searches_run"] is None
    assert cov["fallback_used"] is None
    assert cov["raw"] == ""


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


def _two_stage_responses(structuring_text: str, discovery_text: str = "- bullet"):
    """Build (discovery_resp, structuring_resp) MagicMock pair."""
    d = MagicMock(content=[MagicMock(type="text", text=discovery_text)])
    d.model_dump.return_value = {"id": "stage1"}
    d.content[0].text = discovery_text
    s = MagicMock(content=[MagicMock(type="text", text=structuring_text)])
    s.model_dump.return_value = {"id": "stage2"}
    s.content[0].text = structuring_text
    return d, s


def test_runs_when_outside_cadence(store, tmp_path, monkeypatch):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")
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

    structuring_payload = (
        '{"headlines":[{"title":"t","url":"https://x","source":"s",'
        '"published_at":"2026-05-10T00:00:00Z","summary_ja":"要約"}]}'
    )
    discovery, structuring = _two_stage_responses(structuring_payload)

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
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


def test_handles_invalid_json(store, tmp_path, monkeypatch):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")
    src = ClaudeResearchSource(name="q", api_key="dummy", cadence_hours=12, store=store)
    discovery, structuring = _two_stage_responses("not json")
    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
        mock_anth.return_value = mock_client
        items = src.fetch()
    assert items == []


def test_dumps_response_to_disk(store, tmp_path, monkeypatch):
    # Redirect dump dir into a tmp path so we don't pollute repo logs/.
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(
        name="dump-q", api_key="dummy", cadence_hours=12, store=store
    )
    discovery, structuring = _two_stage_responses('{"headlines":[]}')
    discovery.model_dump.return_value = {"id": "msg_disc"}
    structuring.model_dump.return_value = {"id": "msg_struct"}

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
        mock_anth.return_value = mock_client
        src.fetch()

    discovery_dumps = list((tmp_path / "dumps").glob("dump_q__*__discovery.json"))
    structuring_dumps = list((tmp_path / "dumps").glob("dump_q__*__structuring.json"))
    assert len(discovery_dumps) == 1
    assert len(structuring_dumps) == 1
    payload = discovery_dumps[0].read_text()
    assert "msg_disc" in payload
    assert "dump-q" in payload  # query_name appears in envelope


def test_two_stage_makes_two_calls(store, tmp_path, monkeypatch):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(name="ts-q", api_key="dummy", cadence_hours=12, store=store)
    discovery, structuring = _two_stage_responses(
        '{"headlines":[{"title":"Tokio Marine Q1","url":"https://x","source":"Reuters",'
        '"published_at":"2026-05-10T00:00:00Z","summary_ja":"要約"}]}',
        discovery_text=(
            "- タイトル: Tokio Marine Q1\n  URL: https://x\n  媒体: Reuters\n"
            "  公開日時: 2026-05-10T00:00:00Z\n  要約: 要約"
        ),
    )

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
        mock_anth.return_value = mock_client
        items = src.fetch()

    assert len(items) == 1
    assert items[0].title == "Tokio Marine Q1"
    # Two dumps: discovery + structuring
    discovery_dumps = list((tmp_path / "dumps").glob("*__discovery.json"))
    structuring_dumps = list((tmp_path / "dumps").glob("*__structuring.json"))
    assert len(discovery_dumps) == 1
    assert len(structuring_dumps) == 1
    # Two API calls
    assert mock_client.messages.create.call_count == 2
    # First call had tools, second did not
    first_call_kwargs = mock_client.messages.create.call_args_list[0].kwargs
    second_call_kwargs = mock_client.messages.create.call_args_list[1].kwargs
    assert "tools" in first_call_kwargs
    assert "tools" not in second_call_kwargs


# ---- new tests for prompt-rewrite contract --------------------------------


def test_watchlists_injected_into_discovery_prompt(store, tmp_path, monkeypatch, watchlists):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(
        name="wl-q", api_key="dummy", cadence_hours=12,
        store=store, watchlists=watchlists,
    )
    discovery, structuring = _two_stage_responses('{"headlines":[]}')

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
        mock_anth.return_value = mock_client
        src.fetch()

    discovery_prompt = mock_client.messages.create.call_args_list[0].kwargs[
        "messages"
    ][0]["content"]
    # P1 entities listed under 日本Tier 1
    assert "Tokio Marine" in discovery_prompt
    assert "MS&AD" in discovery_prompt
    # P2 entities listed under グローバル
    assert "Munich Re" in discovery_prompt
    assert "Allianz" in discovery_prompt
    # JST timestamp injected
    assert "JST" in discovery_prompt
    # COVERAGE_NOTES instruction present
    assert "COVERAGE_NOTES" in discovery_prompt


def test_coverage_notes_forwarded_to_record_api_call(store, tmp_path, monkeypatch, watchlists):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(
        name="cov-q", api_key="dummy", cadence_hours=12,
        store=store, watchlists=watchlists,
    )
    discovery_text = (
        "- タイトル: Some headline\n  URL: https://x\n  要約: 要約\n\n"
        "COVERAGE_NOTES:\n"
        "  searches_run: 9\n"
        "  tier1_aggregators_hit: TDnet, FSA\n"
        "  fallback_used: true\n"
        "  gaps: 特になし\n"
    )
    discovery, structuring = _two_stage_responses('{"headlines":[]}', discovery_text=discovery_text)

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery, structuring]
        mock_anth.return_value = mock_client
        src.fetch()

    cur = store.conn.execute(
        "SELECT searches_run, tier1_aggregators_hit, fallback_used "
        "FROM api_usage WHERE query_name='cov-q' ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    assert row == (9, 2, 1)  # 2 aggregators (TDnet + FSA); fallback_used stored as 1
