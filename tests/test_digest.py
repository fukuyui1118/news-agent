from unittest.mock import MagicMock

from news_agent.digest import run_digest
from news_agent.mailer import DigestPayload, Mailer
from news_agent.store import Store, StoryRow
from news_agent.summarizer import Summary, Summarizer


def _row(priority: str = "P1", title: str = "Test") -> StoryRow:
    return StoryRow(
        url_hash="abc" + title,
        url=f"https://example.com/{title}",
        title=title,
        source="TestSource",
        published_at="2026-05-09T12:00:00",
        priority=priority,
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


def test_digest_summarizes_p1_and_p2_and_sends_one_email():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = [
        _row(priority="P1", title="A"),
        _row(priority="P2", title="B"),
    ]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.return_value = Summary(headline="見出し", bullets="- a")
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_digest(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 2
    assert result["sent"] is True
    mailer.send_digest.assert_called_once()
    payload = mailer.send_digest.call_args[0][0]
    assert isinstance(payload, DigestPayload)
    assert {e.priority for e in payload.entries} == {"P1", "P2"}


def test_digest_within_dedup_suppresses_similar_titles():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = [
        _row(priority="P1", title="Tokio Marine reports Q4 earnings beat"),
        _row(priority="P1", title="Tokio Marine ups Q4 guidance after earnings"),
        _row(priority="P2", title="Allianz exits cyber business"),
    ]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.return_value = Summary(headline="x", bullets="- y")
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_digest(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 2  # second TM story dropped as dup
    assert result["suppressed_dup"] == 1
    assert result["sent"] is True


def test_digest_handles_summarizer_failure():
    store = MagicMock(spec=Store)
    store.digest_eligible_stories.return_value = [
        _row(priority="P1", title="ok"),
        _row(priority="P2", title="bad"),
    ]
    summarizer = MagicMock(spec=Summarizer)
    summarizer.summarize.side_effect = [
        Summary(headline="ok", bullets="- a"),
        RuntimeError("API fail"),
    ]
    mailer = MagicMock(spec=Mailer)
    mailer.dry_run = True

    result = run_digest(store=store, summarizer=summarizer, mailer=mailer)

    assert result["summarized"] == 1
    assert result["failed"] == 1
    assert result["sent"] is True
