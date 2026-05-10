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


def test_dumps_response_to_disk(store, tmp_path, monkeypatch):
    # Redirect dump dir into a tmp path so we don't pollute repo logs/.
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    # Force single-call path so we get exactly one dump per fetch.
    src = ClaudeResearchSource(
        name="dump-q", api_key="dummy", cadence_hours=12, store=store, two_stage=False
    )
    fake_text = MagicMock(type="text")
    fake_text.text = '{"headlines":[]}'
    fake_resp = MagicMock(content=[fake_text])
    fake_resp.model_dump.return_value = {"id": "msg_abc", "content": [{"type": "text", "text": "..."}]}

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_resp
        mock_anth.return_value = mock_client
        src.fetch()

    dumps = list((tmp_path / "dumps").glob("dump_q__*.json"))
    assert len(dumps) == 1
    payload = dumps[0].read_text()
    assert "msg_abc" in payload
    assert "dump-q" in payload  # query_name appears in envelope


SAMPLE_XML = """<news_digest as_of="2026-05-10 14:30">
  <bucket name="A_japanese_insurers">
    <item>
      <headline_ja>東京海上、Q1決算を発表</headline_ja>
      <original_headline lang="ja">東京海上、Q1決算を発表</original_headline>
      <company>東京海上HD</company>
      <published_jst>2026-05-10 09:00 JST</published_jst>
      <source>日本経済新聞</source>
      <url>https://www.nikkei.com/x</url>
      <one_line_context_ja>純利益が前年同期比で増加。</one_line_context_ja>
      <other_sources>Reuters, Bloomberg</other_sources>
    </item>
  </bucket>
  <bucket name="B_japan_regulation">
    <item>該当なし</item>
  </bucket>
  <bucket name="C_global_sector">
    <item>
      <headline_ja>Munich Re、Q1利益が増加</headline_ja>
      <original_headline lang="en">Munich Re Q1 profit rises</original_headline>
      <company>Munich Re</company>
      <published_jst>2026-05-10 12:00 JST</published_jst>
      <source>Reuters</source>
      <url>https://reuters.com/y</url>
      <one_line_context_ja>P&amp;C combined ratio improved.</one_line_context_ja>
      <other_sources></other_sources>
    </item>
  </bucket>
  <bucket name="D_rating_actions">
    <item>該当なし</item>
  </bucket>
  <coverage_notes>
    <searches_run>11</searches_run>
    <gaps>特になし</gaps>
  </coverage_notes>
</news_digest>"""


def test_bucket_xml_parses_minimal_response(store, tmp_path, monkeypatch):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(
        name="bx-q",
        api_key="dummy",
        cadence_hours=12,
        store=store,
        prompt_strategy="bucket_xml",
    )
    fake_text = MagicMock(type="text")
    fake_text.text = SAMPLE_XML
    fake_resp = MagicMock(content=[fake_text])
    fake_resp.model_dump.return_value = {"id": "msg_xml"}

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_resp
        mock_anth.return_value = mock_client
        items = src.fetch()

    assert len(items) == 2  # one A item, one C item; sentinels skipped
    titles = [it.title for it in items]
    assert "東京海上、Q1決算を発表" in titles
    assert "Munich Re、Q1利益が増加" in titles
    # raw_text carries bucket label
    assert any(it.raw_text.startswith("[A]") for it in items)
    assert any(it.raw_text.startswith("[C]") for it in items)
    # source from <source> field, not query name
    assert any(it.source == "日本経済新聞" for it in items)
    assert any(it.source == "Reuters" for it in items)
    # published_at parsed and in UTC
    for it in items:
        assert it.published_at is not None
        assert it.published_at.tzinfo is not None


def test_bucket_xml_uses_system_param_and_max_tokens(store):
    src = ClaudeResearchSource(
        name="bx-q",
        api_key="dummy",
        cadence_hours=12,
        store=store,
        prompt_strategy="bucket_xml",
    )
    fake_text = MagicMock(type="text")
    fake_text.text = SAMPLE_XML

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(content=[fake_text])
        mock_anth.return_value = mock_client
        src.fetch()

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 12000
        assert "system" in kwargs
        assert "specialist news analyst" in kwargs["system"]
        # JST datetime injected
        assert "JST" in kwargs["system"]


def test_bucket_xml_handles_malformed_xml(store):
    src = ClaudeResearchSource(
        name="bx-q",
        api_key="dummy",
        cadence_hours=12,
        store=store,
        prompt_strategy="bucket_xml",
    )
    fake_text = MagicMock(type="text")
    fake_text.text = "this is not xml at all"

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(content=[fake_text])
        mock_anth.return_value = mock_client
        items = src.fetch()
    assert items == []


def test_two_stage_makes_two_calls(store, tmp_path, monkeypatch):
    from news_agent.sources import claude_research as cr_mod

    monkeypatch.setattr(cr_mod, "RESPONSE_DUMP_DIR", tmp_path / "dumps")

    src = ClaudeResearchSource(name="ts-q", api_key="dummy", cadence_hours=12, store=store)
    discovery_text = MagicMock(type="text")
    discovery_text.text = "- タイトル: Tokio Marine Q1\n  URL: https://x\n  媒体: Reuters\n  公開日時: 2026-05-10T00:00:00Z\n  要約: 要約"
    discovery_resp = MagicMock(content=[discovery_text])
    discovery_resp.model_dump.return_value = {"id": "stage1"}

    structuring_text = MagicMock(type="text")
    structuring_text.text = '{"headlines":[{"title":"Tokio Marine Q1","url":"https://x","source":"Reuters","published_at":"2026-05-10T00:00:00Z","summary_ja":"要約"}]}'
    structuring_resp = MagicMock(content=[structuring_text])
    structuring_resp.model_dump.return_value = {"id": "stage2"}

    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [discovery_resp, structuring_resp]
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


def test_prompt_override_wins(store):
    src = ClaudeResearchSource(
        name="q",
        api_key="dummy",
        cadence_hours=12,
        store=store,
        prompt_override="OVERRIDE PROMPT {max_headlines}",
    )
    fake_text = MagicMock(type="text")
    fake_text.text = '{"headlines":[]}'
    with patch("news_agent.sources.claude_research.Anthropic") as mock_anth:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(content=[fake_text])
        mock_anth.return_value = mock_client
        src.fetch()

        sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert sent.startswith("OVERRIDE PROMPT")
        assert "30" in sent  # max_headlines default formatted in
