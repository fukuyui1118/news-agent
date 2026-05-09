from news_agent.store import canonicalize_url, hash_item


def test_strips_utm_params():
    a = canonicalize_url("https://example.com/article?utm_source=x&id=42")
    b = canonicalize_url("https://example.com/article?id=42")
    assert a == b


def test_lowercases_host_and_strips_www():
    a = canonicalize_url("https://WWW.Example.com/Path")
    b = canonicalize_url("https://example.com/Path")
    assert a == b


def test_strips_trailing_slash():
    a = canonicalize_url("https://example.com/article/")
    b = canonicalize_url("https://example.com/article")
    assert a == b


def test_preserves_meaningful_query():
    a = canonicalize_url("https://example.com/?id=1&page=2")
    assert "id=1" in a
    assert "page=2" in a


def test_hash_stable_across_title_whitespace():
    h1 = hash_item("https://example.com/x", "  Hello World  ")
    h2 = hash_item("https://example.com/x", "Hello World")
    assert h1 == h2


def test_hash_case_insensitive_title():
    h1 = hash_item("https://example.com/x", "Hello World")
    h2 = hash_item("https://example.com/x", "hello world")
    assert h1 == h2
