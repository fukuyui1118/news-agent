from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from news_agent.ai_classifier import _parse_classifier_json, classify_items
from news_agent.config import WatchlistEntry, Watchlists
from news_agent.sources.base import RawItem


def _item(title: str = "Test", source: str = "src") -> RawItem:
    return RawItem(
        url=f"https://example.com/{title}",
        title=title,
        published_at=datetime.now(timezone.utc),
        source=source,
        raw_text="",
        source_tier=2,
    )


def _watchlists() -> Watchlists:
    return Watchlists(
        p1_japan=[WatchlistEntry(canonical="Tokio Marine"), WatchlistEntry(canonical="MS&AD")],
        p2_global=[WatchlistEntry(canonical="Munich Re"), WatchlistEntry(canonical="Allianz")],
    )


def _mock_anthropic(text: str):
    fake_text = MagicMock(type="text")
    fake_text.text = text
    fake_resp = MagicMock(content=[fake_text])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = fake_resp
    return mock_client


# ---- _parse_classifier_json ----


def test_parse_classifier_json_clean():
    assert _parse_classifier_json('{"p1": [0, 2], "p2": [1]}') == {"p1": [0, 2], "p2": [1]}


def test_parse_classifier_json_with_fences():
    assert _parse_classifier_json('```json\n{"p1": [0]}\n```') == {"p1": [0]}


def test_parse_classifier_json_with_preamble():
    assert _parse_classifier_json('Here:\n{"p1": [], "p2": [3]}\nthanks') == {"p1": [], "p2": [3]}


def test_parse_classifier_json_invalid_returns_none():
    assert _parse_classifier_json("not json") is None


# ---- classify_items happy path ----


def test_classify_items_returns_priority_map():
    items = [_item("A"), _item("B"), _item("C"), _item("D")]
    mock_client = _mock_anthropic('{"p1": [0, 2], "p2": [1]}')
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        out = classify_items(items, _watchlists(), api_key="dummy")
    assert out == {0: "P1", 1: "P2", 2: "P1"}
    # idx 3 not in output → P3 implicitly


def test_classify_items_empty_input_returns_empty():
    out = classify_items([], _watchlists(), api_key="dummy")
    assert out == {}


def test_classify_items_p1_wins_over_p2_for_duplicate_index():
    items = [_item("A")]
    mock_client = _mock_anthropic('{"p1": [0], "p2": [0]}')
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        out = classify_items(items, _watchlists(), api_key="dummy")
    assert out == {0: "P1"}


def test_classify_items_drops_out_of_range_indices():
    items = [_item("A"), _item("B")]
    mock_client = _mock_anthropic('{"p1": [99], "p2": [-1, 0]}')
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        out = classify_items(items, _watchlists(), api_key="dummy")
    # Only idx 0 is in [0,2) range
    assert out == {0: "P2"}


# ---- classify_items fallback paths ----


def test_classify_items_falls_back_to_empty_on_invalid_json():
    items = [_item("A")]
    mock_client = _mock_anthropic("garbage")
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        out = classify_items(items, _watchlists(), api_key="dummy")
    assert out == {}


def test_classify_items_falls_back_on_api_exception():
    items = [_item("A")]
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("boom")
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        out = classify_items(items, _watchlists(), api_key="dummy")
    assert out == {}


def test_classify_items_injects_watchlist_entities_into_prompt():
    items = [_item("A")]
    mock_client = _mock_anthropic('{"p1": [], "p2": []}')
    with patch("news_agent.ai_classifier.Anthropic", return_value=mock_client):
        classify_items(items, _watchlists(), api_key="dummy")
    sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Tokio Marine" in sent
    assert "MS&AD" in sent
    assert "Munich Re" in sent
    assert "Allianz" in sent
