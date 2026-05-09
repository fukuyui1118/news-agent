from news_agent.config import Relevance
from news_agent.relevance import is_relevant


def _rel():
    return Relevance(business_keywords=["earnings", "merger", "CEO", "S&P", "rating"])


def test_tier1_always_relevant_even_without_keywords():
    r = is_relevant("Random text without keywords", source_tier=1, relevance=_rel())
    assert r.relevant is True
    assert "tier-1" in r.reason


def test_tier3_with_keyword_passes():
    r = is_relevant("Company X reports Q1 earnings", source_tier=3, relevance=_rel())
    assert r.relevant is True
    assert "earnings" in r.reason


def test_tier3_without_keyword_fails():
    r = is_relevant("Random news about food and weather", source_tier=3, relevance=_rel())
    assert r.relevant is False
    assert r.reason == "no business keyword found"


def test_tier2_with_keyword_passes():
    r = is_relevant("Two firms announce a merger this week", source_tier=2, relevance=_rel())
    assert r.relevant is True


def test_word_boundary_matches_isolated_term():
    r = is_relevant("CEO steps down today", source_tier=3, relevance=_rel())
    assert r.relevant is True
    assert "CEO" in r.reason


def test_special_char_keyword_matches():
    r = is_relevant("S&P downgrades insurer", source_tier=3, relevance=_rel())
    assert r.relevant is True


def test_word_boundary_avoids_false_positive():
    # "rating" should not match in "berating" (substring) when word-boundary is used
    r = is_relevant("Critics berating the proposal", source_tier=3, relevance=_rel())
    assert r.relevant is False


# --- Japanese keyword tests ---


def _rel_jp():
    return Relevance(business_keywords=["買収", "決算", "中期経営計画", "行政処分"])


def test_japanese_keyword_substring_match():
    r = is_relevant("東京海上が大型買収を発表", source_tier=2, relevance=_rel_jp())
    assert r.relevant is True
    assert "買収" in r.reason


def test_japanese_no_keyword_drops():
    r = is_relevant("プロ野球チームが新人選手を獲得", source_tier=2, relevance=_rel_jp())
    assert r.relevant is False


def test_japanese_kessan_passes():
    r = is_relevant("第一生命が通期決算を発表、純利益は前年比15%増", source_tier=3, relevance=_rel_jp())
    assert r.relevant is True
    assert "決算" in r.reason


def test_japanese_regulatory_passes():
    r = is_relevant("金融庁が損保3社に行政処分を下した", source_tier=3, relevance=_rel_jp())
    assert r.relevant is True
    assert "行政処分" in r.reason
