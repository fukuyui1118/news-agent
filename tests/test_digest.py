from unittest.mock import MagicMock, patch

from news_agent.digest import run_digest
from news_agent.mailer import DigestEntry, DigestPayload, Mailer
from news_agent.store import Store, StoryRow
from news_agent.summarizer import Summarizer


def _row(priority: str = "P1", title: str = "Test") -> StoryRow:
    return StoryRow(
        url_hash="abc" + title,
        url=f"https://example.com/{title}",
        title=title,
        source="TestSource",
        published_at="2026-05-09T12:00:00",
        priority=priority,
    )


def _entry(priority: str = "P1", title: str = "Test") -> DigestEntry:
    return DigestEntry(
        priority=priority,
        headline_ja="日本語見出し",
        original_title=title,
        source="TestSource",
        url=f"https://example.com/{title}",
        summary_bullets="- a\n- b",
    )


def test_digest_empty_returns_not_sent():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_digest(store=store, summarizer=summarizer, mailer=mailer)
    assert result["sent"] is False
    mailer.send_digest.assert_not_called()


def test_digest_default_lookback_is_12h():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    run_digest(store=store, summarizer=summarizer, mailer=mailer)
    store.digest_eligible_stories.assert_called_once()
    kwargs = store.digest_eligible_stories.call_args.kwargs
    assert kwargs["hours"] == 12


def test_digest_calls_composer_and_sends_mailer():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = [
        _row(priority="P1", title="A"),
        _row(priority="P2", title="B"),
    ]
    summarizer = MagicMock(spec=Summarizer)
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    with patch(
        "news_agent.digest.compose_email",
        return_value=[_entry(priority="P1", title="A"), _entry(priority="P2", title="B")],
    ) as mock_compose:
        result = run_digest(store=store, summarizer=summarizer, mailer=mailer)

    mock_compose.assert_called_once()
    assert result["summarized"] == 2
    assert result["sent"] is True
    mailer.send_digest.assert_called_once()
    payload = mailer.send_digest.call_args[0][0]
    assert isinstance(payload, DigestPayload)
    assert {e.priority for e in payload.entries} == {"P1", "P2"}


def test_digest_composer_empty_skips_send():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = [_row(title="A")]
    summarizer = MagicMock(spec=Summarizer)
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    with patch("news_agent.digest.compose_email", return_value=[]):
        result = run_digest(store=store, summarizer=summarizer, mailer=mailer)

    assert result["sent"] is False
    assert result["summarized"] == 0
    mailer.send_digest.assert_not_called()
