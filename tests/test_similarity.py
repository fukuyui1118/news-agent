from news_agent.similarity import is_duplicate, jaccard, normalize, shingles


def test_normalize_lowercase_strips_punct():
    assert normalize("Tokio Marine: Q4 Earnings Beat!") == "tokio marine q4 earnings beat"


def test_normalize_preserves_japanese():
    out = normalize("東京海上、Q4決算で予想を上回る")
    assert "東京海上" in out
    assert "q4" in out


def test_shingles_returns_words_and_ngrams():
    s = shingles("Tokio Marine reports earnings")
    assert "tokio" in s
    assert "marine" in s


def test_jaccard_identical():
    a = shingles("Tokio Marine reports earnings")
    assert jaccard(a, a) == 1.0


def test_jaccard_disjoint():
    a = shingles("apple")
    b = shingles("banana")
    assert jaccard(a, b) < 0.2


def test_is_duplicate_paraphrase_english():
    a = "Tokio Marine reports Q4 earnings beat"
    b = "Tokio Marine ups Q4 guidance after earnings"
    assert is_duplicate(a, [b], threshold=0.30) is True


def test_is_duplicate_japanese_same_event():
    a = "東京海上、Q4決算で予想を上回る"
    b = "東京海上が Q4 決算予想を上回る"
    assert is_duplicate(a, [b], threshold=0.30) is True


def test_is_duplicate_unrelated():
    a = "Tokio Marine reports earnings"
    b = "Allianz exits cyber business"
    assert is_duplicate(a, [b], threshold=0.30) is False


def test_is_duplicate_empty_recent():
    assert is_duplicate("anything", [], threshold=0.30) is False


def test_is_duplicate_empty_title():
    assert is_duplicate("", ["something"], threshold=0.30) is False
