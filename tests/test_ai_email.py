from unittest.mock import MagicMock

from news_agent.ai_email import _parse_email_json, compose_email
from news_agent.store import StoryRow
from news_agent.summarizer import Summary, Summarizer


def _row(priority: str = "P1", title: str = "Test") -> StoryRow:
    return StoryRow(
        url_hash="h" + title,
        url=f"https://example.com/{title}",
        title=title,
        source="src",
        published_at="2026-05-09T12:00:00",
        priority=priority,
    )


def _mock_summarizer_with_response(text: str) -> Summarizer:
    s = MagicMock(spec=Summarizer)
    s.client = MagicMock()
    s.client.messages.create.return_value = MagicMock(
        content=[MagicMock(type="text", text=text)]
    )
    return s


# ---- _parse_email_json ----


def test_parse_email_json_clean():
    assert _parse_email_json('{"entries": []}') == {"entries": []}


def test_parse_email_json_with_fences():
    assert _parse_email_json('```json\n{"entries": [{"a":1}]}\n```') == {"entries": [{"a": 1}]}


def test_parse_email_json_with_preamble():
    assert _parse_email_json('Sure:\n{"entries":[]}\ndone') == {"entries": []}


def test_parse_email_json_invalid_returns_none():
    assert _parse_email_json("nope") is None


# ---- compose_email happy path ----


def test_compose_email_returns_entries_from_claude_json():
    rows = [_row(priority="P1", title="A"), _row(priority="P2", title="B")]
    summarizer = _mock_summarizer_with_response(
        '{"entries":['
        '{"priority":"P1","headline_ja":"見出しA","original_title":"A",'
        '"source":"src","url":"https://example.com/A","summary_bullets":["項目1","項目2"]},'
        '{"priority":"P2","headline_ja":"見出しB","original_title":"B",'
        '"source":"src","url":"https://example.com/B","summary_bullets":["項目3"]}'
        ']}'
    )
    entries = compose_email(rows, summarizer)
    assert len(entries) == 2
    assert {e.priority for e in entries} == {"P1", "P2"}
    assert entries[0].summary_bullets.startswith("- ")


def test_compose_email_empty_input_returns_empty():
    summarizer = MagicMock(spec=Summarizer)
    summarizer.client = MagicMock()
    assert compose_email([], summarizer) == []
    summarizer.client.messages.create.assert_not_called()


# ---- cap + truncation ----


def test_compose_email_caps_output_at_max_entries():
    rows = [_row(title=f"R{i}") for i in range(40)]
    inner = ",".join(
        f'{{"priority":"P1","headline_ja":"H{i}","original_title":"R{i}","source":"s",'
        f'"url":"https://example.com/R{i}","summary_bullets":["a"]}}'
        for i in range(25)
    )
    summarizer = _mock_summarizer_with_response(f'{{"entries":[{inner}]}}')
    entries = compose_email(rows, summarizer, max_entries=15)
    assert len(entries) == 15


def test_compose_email_truncates_input_to_2x_max_entries():
    rows = [_row(title=f"R{i}") for i in range(100)]
    summarizer = _mock_summarizer_with_response('{"entries":[]}')
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")

    entries = compose_email(rows, summarizer, max_entries=15)

    assert len(entries) == 15  # fallback respects cap
    assert summarizer.summarize.call_count == 15

    sent = summarizer.client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "R29" in sent
    assert "R30" not in sent


def test_compose_email_uses_max_tokens_8192():
    rows = [_row(title="A")]
    summarizer = _mock_summarizer_with_response('{"entries":[]}')
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")
    compose_email(rows, summarizer)
    assert summarizer.client.messages.create.call_args.kwargs["max_tokens"] == 8192


# ---- fallback paths ----


def test_compose_email_falls_back_when_invalid_json():
    rows = [_row(title="A")]
    summarizer = _mock_summarizer_with_response("garbage")
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")
    entries = compose_email(rows, summarizer)
    assert len(entries) == 1
    assert entries[0].headline_ja == "fb"


def test_compose_email_falls_back_on_api_exception():
    rows = [_row(title="A")]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.client = MagicMock()
    summarizer.client.messages.create.side_effect = RuntimeError("boom")
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")
    entries = compose_email(rows, summarizer)
    assert len(entries) == 1
    summarizer.summarize.assert_called_once()
