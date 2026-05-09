from datetime import datetime, timezone

import pytest

from news_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_insert_returns_hash_first_time(store):
    h = store.insert_if_new(
        url="https://example.com/a",
        title="Hello",
        source="test",
        published_at=datetime.now(timezone.utc),
        priority="P3",
    )
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex


def test_insert_duplicate_returns_none(store):
    kwargs = dict(
        url="https://example.com/a",
        title="Hello",
        source="test",
        published_at=None,
        priority="P3",
    )
    assert store.insert_if_new(**kwargs) is not None
    assert store.insert_if_new(**kwargs) is None


def test_dedup_normalizes_url(store):
    store.insert_if_new(
        url="https://example.com/a?utm_source=x",
        title="Hello",
        source="test",
        published_at=None,
        priority="P3",
    )
    h = store.insert_if_new(
        url="https://www.example.com/a/",
        title="Hello",
        source="test",
        published_at=None,
        priority="P3",
    )
    assert h is None


def test_dropped_priority_persists_reason(store):
    h = store.insert_if_new(
        url="https://example.com/dropped",
        title="No keywords here",
        source="test",
        published_at=None,
        priority="DROPPED",
        dropped_reason="no business keyword found",
    )
    assert h is not None
    row = store.conn.execute(
        "SELECT priority, dropped_reason FROM seen WHERE url_hash=?", (h,)
    ).fetchone()
    assert row[0] == "DROPPED"
    assert row[1] == "no business keyword found"


def test_digest_eligible_returns_p1_p2(store):
    # P1 + P2 are both included regardless of email status (Phase 4: re-summarize for daily digest)
    store.insert_if_new(url="https://example.com/p1", title="p1", source="t",
                       published_at=None, priority="P1")
    store.insert_if_new(url="https://example.com/p2", title="p2", source="t",
                       published_at=None, priority="P2")
    # P3 — never in digest under Phase 4
    store.insert_if_new(url="https://example.com/p3", title="p3", source="t",
                       published_at=None, priority="P3")
    # DROPPED — never eligible
    store.insert_if_new(url="https://example.com/d", title="d", source="t",
                       published_at=None, priority="DROPPED",
                       dropped_reason="no keyword")

    rows = store.digest_eligible_stories(hours=24)
    titles = {r.title for r in rows}
    assert titles == {"p1", "p2"}


def test_digest_includes_already_emailed_p1(store):
    h = store.insert_if_new(url="https://example.com/p1", title="p1", source="t",
                           published_at=None, priority="P1")
    store.mark_emailed(url_hash=h, summary="already sent in 3-hour batch")
    rows = store.digest_eligible_stories(hours=24)
    assert len(rows) == 1
    assert rows[0].title == "p1"


def test_unemailed_stories_p1_only(store):
    store.insert_if_new(url="https://example.com/p1a", title="a", source="t",
                       published_at=None, priority="P1")
    h = store.insert_if_new(url="https://example.com/p1b", title="b", source="t",
                           published_at=None, priority="P1")
    store.mark_emailed(url_hash=h, summary="sent")
    store.insert_if_new(url="https://example.com/p2", title="c", source="t",
                       published_at=None, priority="P2")

    rows = store.unemailed_stories(priority="P1")
    titles = {r.title for r in rows}
    assert titles == {"a"}


def test_recently_emailed_titles(store):
    h = store.insert_if_new(url="https://example.com/x", title="recent P1", source="t",
                           published_at=None, priority="P1")
    store.mark_emailed(url_hash=h, summary="sent")

    titles = store.recently_emailed_titles(hours=24, priority="P1")
    assert "recent P1" in titles


def test_mark_suppressed_dup_marks_emailed(store):
    h = store.insert_if_new(url="https://example.com/dup", title="dup", source="t",
                           published_at=None, priority="P1")
    store.mark_suppressed_dup(url_hash=h)
    rows = store.unemailed_stories(priority="P1")
    assert all(r.url_hash != h for r in rows)


def test_mark_emailed_sets_summary_and_timestamp(store):
    h = store.insert_if_new(
        url="https://example.com/a",
        title="x",
        source="test",
        published_at=None,
        priority="P1",
    )
    assert h is not None
    store.mark_emailed(url_hash=h, summary="test summary")
    row = store.conn.execute(
        "SELECT summary, emailed_at FROM seen WHERE url_hash=?", (h,)
    ).fetchone()
    assert row[0] == "test summary"
    assert row[1] is not None
