from unittest.mock import MagicMock

from news_agent.curator import _parse_curator_json, curate_digest
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


# ---- _parse_curator_json ----------------------------------------------------


def test_parse_curator_json_clean():
    parsed = _parse_curator_json('{"entries": [{"url": "u"}]}')
    assert parsed == {"entries": [{"url": "u"}]}


def test_parse_curator_json_strips_fences():
    parsed = _parse_curator_json('```json\n{"entries": []}\n```')
    assert parsed == {"entries": []}


def test_parse_curator_json_with_preamble():
    parsed = _parse_curator_json('Here is the JSON:\n{"entries": [{"a":1}]}\nthanks')
    assert parsed == {"entries": [{"a": 1}]}


def test_parse_curator_json_invalid_returns_none():
    assert _parse_curator_json("not json at all") is None


# ---- curate_digest happy path ----------------------------------------------


def test_curate_digest_returns_entries_from_claude_json():
    rows = [_row(priority="P1", title="A"), _row(priority="P2", title="B")]
    summarizer = _mock_summarizer_with_response(
        '{"entries":['
        '{"priority":"P1","headline_ja":"見出しA","original_title":"A",'
        '"source":"src","url":"https://example.com/A",'
        '"summary_bullets":["項目1","項目2"]},'
        '{"priority":"P2","headline_ja":"見出しB","original_title":"B",'
        '"source":"src","url":"https://example.com/B",'
        '"summary_bullets":["項目3"]}'
        ']}'
    )

    entries = curate_digest(rows, summarizer)
    assert len(entries) == 2
    assert {e.priority for e in entries} == {"P1", "P2"}
    assert "項目1" in entries[0].summary_bullets
    assert entries[0].summary_bullets.startswith("- ")  # bullet prefix added


def test_curate_digest_empty_input_returns_empty():
    summarizer = MagicMock(spec=Summarizer)
    summarizer.client = MagicMock()
    assert curate_digest([], summarizer) == []
    summarizer.client.messages.create.assert_not_called()


# ---- fallback paths --------------------------------------------------------


def test_curate_digest_falls_back_when_invalid_json():
    rows = [_row(title="A")]
    summarizer = _mock_summarizer_with_response("garbage not json")
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")

    entries = curate_digest(rows, summarizer)
    assert len(entries) == 1
    assert entries[0].headline_ja == "fb"
    summarizer.summarize.assert_called_once()


def test_curate_digest_falls_back_when_curator_returns_no_entries():
    rows = [_row(title="A"), _row(title="B")]
    summarizer = _mock_summarizer_with_response('{"entries": []}')
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")

    entries = curate_digest(rows, summarizer)
    assert len(entries) == 2  # fallback summarized both
    assert summarizer.summarize.call_count == 2


def test_curate_digest_falls_back_on_api_exception():
    rows = [_row(title="A")]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.client = MagicMock()
    summarizer.client.messages.create.side_effect = RuntimeError("boom")
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")

    entries = curate_digest(rows, summarizer)
    assert len(entries) == 1
    summarizer.summarize.assert_called_once()


# ---- cap + truncation ------------------------------------------------------


def test_curate_digest_caps_output_at_max_entries():
    """Even if Claude returns more entries than max_entries, the result is sliced."""
    rows = [_row(title=f"R{i}") for i in range(40)]
    # Claude returns 25 entries — more than max_entries=15
    inner = ",".join(
        f'{{"priority":"P1","headline_ja":"H{i}","original_title":"R{i}","source":"s",'
        f'"url":"https://example.com/R{i}","summary_bullets":["a"]}}'
        for i in range(25)
    )
    summarizer = _mock_summarizer_with_response(f'{{"entries":[{inner}]}}')

    entries = curate_digest(rows, summarizer, max_entries=15)
    assert len(entries) == 15


def test_curate_digest_truncates_input_to_2x_max_entries():
    """Only the top max_entries*2 rows are sent to Claude (rest ignored)."""
    rows = [_row(title=f"R{i}") for i in range(100)]
    summarizer = _mock_summarizer_with_response('{"entries":[]}')
    # Force fallback path so we can count summarize() calls
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")

    entries = curate_digest(rows, summarizer, max_entries=15)

    # Curator passed candidates=rows[:30] to Haiku; on empty parse falls back
    # to per-row summarize on those candidates, then caps at max_entries=15.
    assert len(entries) == 15
    assert summarizer.summarize.call_count == 15

    # Verify the prompt only mentioned the first 30 rows (not all 100).
    sent_prompt = summarizer.client.messages.create.call_args.kwargs["messages"][0][
        "content"
    ]
    assert "R29" in sent_prompt
    assert "R30" not in sent_prompt
    assert "R99" not in sent_prompt


def test_curate_digest_uses_max_tokens_8192():
    rows = [_row(title="A")]
    summarizer = _mock_summarizer_with_response('{"entries":[]}')
    summarizer.summarize.return_value = Summary(headline="fb", bullets="- f")
    curate_digest(rows, summarizer)
    kwargs = summarizer.client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 8192
