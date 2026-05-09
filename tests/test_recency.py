from datetime import datetime, timedelta, timezone

from news_agent.agent import RECENCY_HOURS, apply_recency_filter
from news_agent.sources.base import RawItem


def _item(**kw) -> RawItem:
    return RawItem(
        url=kw.get("url", "https://example.com/x"),
        title=kw.get("title", "title"),
        published_at=kw.get("published_at"),
        source=kw.get("source", "TestSrc"),
        raw_text=kw.get("raw_text", ""),
        source_tier=kw.get("source_tier", 2),
    )


def test_keeps_items_within_window():
    fresh = datetime.now(timezone.utc) - timedelta(hours=2)
    kept, no_pub, old = apply_recency_filter([_item(published_at=fresh)])
    assert len(kept) == 1
    assert no_pub == 0
    assert old == 0


def test_drops_items_older_than_24h():
    stale = datetime.now(timezone.utc) - timedelta(hours=RECENCY_HOURS + 1)
    kept, no_pub, old = apply_recency_filter([_item(published_at=stale)])
    assert len(kept) == 0
    assert old == 1
    assert no_pub == 0


def test_skips_items_with_none_pubdate():
    kept, no_pub, old = apply_recency_filter([_item(published_at=None)])
    assert len(kept) == 0
    assert no_pub == 1
    assert old == 0


def test_skips_items_with_naive_datetime():
    naive = datetime(2026, 5, 10, 12, 0, 0)  # no tzinfo
    kept, no_pub, old = apply_recency_filter([_item(published_at=naive)])
    assert len(kept) == 0
    assert no_pub == 1
    assert old == 0


def test_mixed_batch_partitions_correctly():
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    items = [
        _item(title="a", published_at=fresh),
        _item(title="b", published_at=None),
        _item(title="c", published_at=stale),
        _item(title="d", published_at=fresh),
    ]
    kept, no_pub, old = apply_recency_filter(items)
    assert {i.title for i in kept} == {"a", "d"}
    assert no_pub == 1
    assert old == 1


def test_boundary_at_exactly_24h_drops():
    edge = datetime.now(timezone.utc) - timedelta(hours=RECENCY_HOURS, seconds=1)
    kept, _, old = apply_recency_filter([_item(published_at=edge)])
    assert len(kept) == 0
    assert old == 1
