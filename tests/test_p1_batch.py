from unittest.mock import MagicMock

from news_agent.digest import run_p1_batch
from news_agent.mailer import Mailer, P1BatchPayload
from news_agent.store import Store, StoryRow
from news_agent.summarizer import Summary, Summarizer


def _row(title: str) -> StoryRow:
    return StoryRow(
        url_hash=f"hash-{title}",
        url=f"https://example.com/{title}",
        title=title,
        source="TestSource",
        published_at="2026-05-09T12:00:00",
        priority="P1",
    )


def test_empty_returns_not_sent():
    store = MagicMock(spec=Store)
    store.unemailed_stories.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_p1_batch(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 0
    assert result["sent"] is False
    mailer.send_p1_batch.assert_not_called()


def test_summarizes_unique_stories_and_sends():
    store = MagicMock(spec=Store)
    store.unemailed_stories.return_value = [
        _row("Tokio Marine reports Q4 earnings beat"),
        _row("Allianz exits cyber business in Japan"),
    ]
    store.recently_emailed_titles.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.return_value = Summary(headline="見出し", bullets="- a")
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_p1_batch(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 2
    assert result["suppressed_dup"] == 0
    assert result["sent"] is True
    assert summarizer.summarize.call_count == 2
    assert store.mark_emailed.call_count == 2
    payload = mailer.send_p1_batch.call_args[0][0]
    assert isinstance(payload, P1BatchPayload)
    assert len(payload.entries) == 2


def test_suppresses_duplicates_against_recent():
    store = MagicMock(spec=Store)
    store.unemailed_stories.return_value = [
        _row("Tokio Marine reports Q4 earnings beat"),
    ]
    store.recently_emailed_titles.return_value = [
        "Tokio Marine ups Q4 guidance after earnings"
    ]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.return_value = Summary(headline="x", bullets="- y")
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_p1_batch(store=store, summarizer=summarizer, mailer=mailer)

    assert result["suppressed_dup"] == 1
    assert result["summarized"] == 0
    assert result["sent"] is False
    summarizer.summarize.assert_not_called()
    store.mark_suppressed_dup.assert_called_once()


def test_suppresses_duplicates_within_batch():
    store = MagicMock(spec=Store)
    store.unemailed_stories.return_value = [
        _row("Tokio Marine reports Q4 earnings beat"),
        _row("Tokio Marine ups Q4 guidance after earnings"),
        _row("Allianz exits cyber business"),
    ]
    store.recently_emailed_titles.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.return_value = Summary(headline="x", bullets="- y")
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_p1_batch(store=store, summarizer=summarizer, mailer=mailer)

    # First Tokio Marine story is summarized, second is suppressed (duplicate of first), Allianz is summarized.
    assert result["summarized"] == 2
    assert result["suppressed_dup"] == 1
    assert result["sent"] is True


def test_summarizer_failure_does_not_kill_batch():
    store = MagicMock(spec=Store)
    store.unemailed_stories.return_value = [_row("ok"), _row("bad")]
    store.recently_emailed_titles.return_value = []
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.side_effect = [
        Summary(headline="ok", bullets="- a"),
        RuntimeError("API fail"),
    ]
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_p1_batch(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 1
    assert result["failed"] == 1
    assert result["sent"] is True
