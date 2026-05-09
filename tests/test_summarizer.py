from datetime import datetime
from unittest.mock import MagicMock, patch

from news_agent.summarizer import Article, Summarizer


def _article():
    return Article(
        title="Tokio Marine reports Q4 earnings beat",
        source="TestSource",
        url="https://example.com/x",
        raw_text="Tokio Marine reported a profit of $1B, beating analyst estimates of $850M.",
        published_at=datetime(2026, 5, 1),
        entity="Tokio Marine",
    )


_SAMPLE_RESPONSE = """東京海上、Q4決算で予想を上回る

- 第4四半期に10億ドルの利益を計上
- アナリスト予想の8.5億ドルを上回る
- 株価は3%上昇で反応"""


def test_summarize_returns_structured_summary():
    with patch("news_agent.summarizer.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=_SAMPLE_RESPONSE)]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client

        s = Summarizer(api_key="fake")
        summary = s.summarize(_article())

    assert summary.headline == "東京海上、Q4決算で予想を上回る"
    assert "10億ドル" in summary.bullets
    assert summary.bullets.startswith("-")


def test_summary_as_full_text_joins_with_blank_line():
    with patch("news_agent.summarizer.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=_SAMPLE_RESPONSE)]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client
        s = Summarizer(api_key="fake")
        summary = s.summarize(_article())

    full = summary.as_full_text()
    assert full.startswith("東京海上")
    assert "\n\n-" in full


def test_prompt_structure_japanese_system():
    with patch("news_agent.summarizer.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=_SAMPLE_RESPONSE)]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client

        s = Summarizer(api_key="fake", model="claude-haiku-4-5")
        s.summarize(_article())

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert "日本語" in kwargs["system"]
    assert kwargs["max_tokens"] == 500
    user_content = kwargs["messages"][0]["content"]
    assert "Tokio Marine" in user_content
    assert "監視対象企業" in user_content


def test_no_entity_omits_entity_line():
    article = _article()
    article.entity = None
    msg = Summarizer._build_user_message(article)
    assert "監視対象企業" not in msg


def test_parse_handles_no_blank_line():
    parsed = Summarizer._parse("見出しだけ\n- 箇条書き1\n- 箇条書き2")
    assert parsed.headline == "見出しだけ"
    assert "箇条書き1" in parsed.bullets


def test_parse_handles_empty_input():
    parsed = Summarizer._parse("")
    assert parsed.headline == "(要約なし)"
    assert parsed.bullets == ""
