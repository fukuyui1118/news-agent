from unittest.mock import MagicMock, patch

import httpx
import pytest

from news_agent.inoreader_oauth import (
    InoreaderAuthError,
    InoreaderClient,
    build_authorization_url,
    exchange_code_for_tokens,
)


def _mock_httpx_response(status_code: int, json_body: dict | None = None, text: str = ""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


# ---- constructor validation ----


def test_client_requires_credentials():
    with pytest.raises(InoreaderAuthError):
        InoreaderClient(app_id="", app_secret="s", refresh_token="r")
    with pytest.raises(InoreaderAuthError):
        InoreaderClient(app_id="a", app_secret="", refresh_token="r")
    with pytest.raises(InoreaderAuthError):
        InoreaderClient(app_id="a", app_secret="s", refresh_token="")


# ---- _refresh_access_token ----


def test_refresh_access_token_caches_value():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    body = {"access_token": "AT-1", "expires_in": 3600, "refresh_token": "r"}
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, body)) as post:
        token1 = client._ensure_access_token()
        # Second call should NOT hit the API — token cached and not expired.
        token2 = client._ensure_access_token()
    assert token1 == "AT-1"
    assert token1 == token2
    assert post.call_count == 1


def test_refresh_access_token_rotates_refresh_token():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r-old")
    body = {"access_token": "AT", "expires_in": 3600, "refresh_token": "r-new"}
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, body)):
        client._refresh_access_token()
    assert client.refresh_token == "r-new"


def test_refresh_access_token_raises_on_400():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(400, text="invalid_grant")):
        with pytest.raises(InoreaderAuthError):
            client._refresh_access_token()


def test_refresh_access_token_raises_on_network_error():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    with patch("news_agent.inoreader_oauth.httpx.post",
               side_effect=httpx.ConnectError("boom")):
        with pytest.raises(InoreaderAuthError):
            client._refresh_access_token()


# ---- fetch_tag ----


def test_fetch_tag_returns_items():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    refresh_body = {"access_token": "AT", "expires_in": 3600, "refresh_token": "r"}
    fetch_body = {"items": [{"id": "1", "title": "T1"}, {"id": "2", "title": "T2"}]}

    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, refresh_body)), \
         patch("news_agent.inoreader_oauth.httpx.get",
               return_value=_mock_httpx_response(200, fetch_body)) as get:
        items = client.fetch_tag("123", "asahi", n=20)
    assert len(items) == 2
    # Verify URL + Bearer header
    call_kwargs = get.call_args.kwargs
    assert "stream/contents/user/123/tag/asahi" in get.call_args.args[0]
    assert call_kwargs["headers"]["Authorization"] == "Bearer AT"


def test_fetch_tag_retries_on_401():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    # First refresh returns AT-1, then 401, then a new refresh returns AT-2.
    refresh_responses = [
        _mock_httpx_response(200, {"access_token": "AT-1", "expires_in": 3600, "refresh_token": "r"}),
        _mock_httpx_response(200, {"access_token": "AT-2", "expires_in": 3600, "refresh_token": "r"}),
    ]
    fetch_responses = [
        _mock_httpx_response(401, text="expired"),
        _mock_httpx_response(200, {"items": [{"id": "x"}]}),
    ]

    with patch("news_agent.inoreader_oauth.httpx.post", side_effect=refresh_responses), \
         patch("news_agent.inoreader_oauth.httpx.get", side_effect=fetch_responses):
        items = client.fetch_tag("u", "t")
    assert items == [{"id": "x"}]


def test_fetch_tag_returns_empty_on_500():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, {"access_token": "AT", "expires_in": 3600})), \
         patch("news_agent.inoreader_oauth.httpx.get",
               return_value=_mock_httpx_response(500, text="server error")):
        items = client.fetch_tag("u", "t")
    assert items == []


def test_fetch_tag_returns_empty_on_network_error():
    client = InoreaderClient(app_id="a", app_secret="s", refresh_token="r")
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, {"access_token": "AT", "expires_in": 3600})), \
         patch("news_agent.inoreader_oauth.httpx.get",
               side_effect=httpx.ConnectError("network down")):
        items = client.fetch_tag("u", "t")
    assert items == []


# ---- refresh-token persistence ----


def test_persist_refresh_token_updates_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-test\nINOREADER_REFRESH_TOKEN=old-value\nOTHER=foo\n"
    )
    client = InoreaderClient(
        app_id="a", app_secret="s", refresh_token="old-value", env_path=env_file
    )
    client._persist_refresh_token("new-value")
    text = env_file.read_text()
    assert "INOREADER_REFRESH_TOKEN=new-value" in text
    assert "ANTHROPIC_API_KEY=sk-test" in text
    assert "OTHER=foo" in text


def test_persist_refresh_token_appends_when_missing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk\n")
    client = InoreaderClient(
        app_id="a", app_secret="s", refresh_token="r", env_path=env_file
    )
    client._persist_refresh_token("freshly-added")
    text = env_file.read_text()
    assert "INOREADER_REFRESH_TOKEN=freshly-added" in text


# ---- bootstrap helpers ----


def test_build_authorization_url_includes_required_params():
    url = build_authorization_url("APP-ID-123")
    assert "client_id=APP-ID-123" in url
    assert "redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in url
    assert "response_type=code" in url
    assert "scope=read" in url


def test_exchange_code_for_tokens_returns_response_body():
    body = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(200, body)):
        out = exchange_code_for_tokens(app_id="a", app_secret="s", code="abc")
    assert out == body


def test_exchange_code_for_tokens_raises_on_400():
    with patch("news_agent.inoreader_oauth.httpx.post",
               return_value=_mock_httpx_response(400, text="invalid_grant")):
        with pytest.raises(InoreaderAuthError):
            exchange_code_for_tokens(app_id="a", app_secret="s", code="bad")
