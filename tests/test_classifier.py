from news_agent.classifier import classify
from news_agent.config import Watchlists, WatchlistEntry


def _wl():
    return Watchlists(
        p1_japan=[
            WatchlistEntry(canonical="Tokio Marine", aliases=["東京海上"]),
        ],
        p2_global=[
            WatchlistEntry(canonical="Munich Re", aliases=["Munich Reinsurance"]),
            WatchlistEntry(
                canonical="Allianz",
                aliases=["Allianz SE"],
                exclude=["Allianz Arena"],
            ),
        ],
    )


def test_p1_match_english():
    m = classify("Tokio Marine reports Q4 earnings", _wl())
    assert m.priority == "P1"
    assert m.canonical == "Tokio Marine"


def test_p1_match_japanese():
    m = classify("東京海上、新事業を発表", _wl())
    assert m.priority == "P1"


def test_p1_wins_over_p2():
    m = classify("Tokio Marine and Munich Re announce partnership", _wl())
    assert m.priority == "P1"


def test_p2_match():
    m = classify("Munich Re raises profit guidance", _wl())
    assert m.priority == "P2"
    assert m.canonical == "Munich Re"


def test_p3_default():
    m = classify("Bond yields fall on Fed comments", _wl())
    assert m.priority == "P3"


def test_word_boundary_avoids_substring():
    m = classify("AllianzfooBar reports earnings", _wl())
    assert m.priority == "P3"


def test_exclude_blocks_match():
    m = classify("Bayern Munich beats rivals at Allianz Arena", _wl())
    assert m.priority == "P3"
