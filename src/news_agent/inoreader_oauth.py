"""OAuth-authenticated client for the Inoreader REST API.

Obtains an access token from a long-lived refresh token at first use,
caches it in memory until expiry, and auto-refreshes on 401. If the
token-refresh endpoint returns a rotated refresh token (Inoreader
occasionally rotates them), persists the new value back to `.env`
so the next process start picks it up.

Used by `sources/inoreader.py` to fetch tag-stream contents with
the article's true publish time (`published`), which the public-share
RSS export does not expose.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger()

OAUTH_TOKEN_URL = "https://www.inoreader.com/oauth2/token"
OAUTH_AUTH_URL = "https://www.inoreader.com/oauth2/auth"
API_BASE = "https://www.inoreader.com/reader/api/0"

# Inoreader access tokens are 1 hour TTL; refresh ~5 min early.
TOKEN_REFRESH_MARGIN_SEC = 300


class InoreaderAuthError(Exception):
    """Raised when OAuth credentials are invalid or refresh fails permanently."""


class InoreaderClient:
    """Thin synchronous client for Inoreader's Google-Reader-compatible REST API.

    Construct once per process and pass to multiple `InoreaderSource` instances.
    Thread-safe enough for the agent's concurrent fetch_all loop because token
    refresh is idempotent — worst case is two parallel refreshes, both winning.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        refresh_token: str,
        env_path: Path | None = None,
        timeout_sec: float = 20.0,
    ) -> None:
        if not app_id or not app_secret or not refresh_token:
            raise InoreaderAuthError(
                "InoreaderClient requires app_id, app_secret, and refresh_token"
            )
        self.app_id = app_id
        self.app_secret = app_secret
        self.refresh_token = refresh_token
        self.env_path = env_path
        self.timeout_sec = timeout_sec
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0

    # ---- token management ------------------------------------------------

    def _ensure_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token
        return self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        """Exchange the refresh token for a new access token via /oauth2/token.

        Updates `self.refresh_token` if the response rotates it, and persists
        the new value to `.env` (best-effort).
        """
        try:
            resp = httpx.post(
                OAUTH_TOKEN_URL,
                data={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                timeout=self.timeout_sec,
            )
        except httpx.HTTPError as e:
            raise InoreaderAuthError(f"oauth/token request failed: {e}") from e

        if resp.status_code != 200:
            raise InoreaderAuthError(
                f"oauth/token returned {resp.status_code}: {resp.text[:200]}"
            )

        body = resp.json()
        access = body.get("access_token")
        ttl = int(body.get("expires_in", 3600))
        new_refresh = body.get("refresh_token")
        if not access:
            raise InoreaderAuthError(
                f"oauth/token response missing access_token: {body!r}"
            )

        self._access_token = access
        self._access_token_expires_at = time.time() + ttl - TOKEN_REFRESH_MARGIN_SEC

        if new_refresh and new_refresh != self.refresh_token:
            old = self.refresh_token
            self.refresh_token = new_refresh
            log.info("inoreader.refresh_token.rotated", old_prefix=old[:8])
            self._persist_refresh_token(new_refresh)

        log.info(
            "inoreader.access_token.refreshed",
            expires_in_sec=ttl,
        )
        return access

    def _persist_refresh_token(self, new_token: str) -> None:
        """Best-effort write of rotated refresh token back to `.env`.

        If env_path is None or the file is read-only, log a warning. The
        in-memory token is still good for the rest of this process.
        """
        if self.env_path is None or not self.env_path.exists():
            log.warning(
                "inoreader.refresh_token.persist_skipped",
                reason="no env_path or file missing",
            )
            return
        try:
            text = self.env_path.read_text()
            new_text, n = re.subn(
                r"^INOREADER_REFRESH_TOKEN=.*$",
                f"INOREADER_REFRESH_TOKEN={new_token}",
                text,
                count=1,
                flags=re.MULTILINE,
            )
            if n == 0:
                # Append if not present.
                new_text = text.rstrip() + f"\nINOREADER_REFRESH_TOKEN={new_token}\n"
            self.env_path.write_text(new_text)
            log.info("inoreader.refresh_token.persisted", path=str(self.env_path))
        except OSError as e:
            log.warning(
                "inoreader.refresh_token.persist_failed",
                error=f"{type(e).__name__}: {e}",
            )

    # ---- API call --------------------------------------------------------

    def fetch_tag(self, user_id: str, tag: str, *, n: int = 50) -> list[dict]:
        """GET /reader/api/0/stream/contents/user/<userid>/tag/<tag>?n=<n>.

        Returns the API's `items` array. Raises InoreaderAuthError if the
        refresh token is permanently invalid; returns an empty list on
        transient HTTP errors after logging.
        """
        url = f"{API_BASE}/stream/contents/user/{user_id}/tag/{tag}"
        params = {"n": str(n), "output": "json"}

        access = self._ensure_access_token()
        headers = {"Authorization": f"Bearer {access}"}

        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=self.timeout_sec)
        except httpx.HTTPError as e:
            log.error("inoreader.fetch.http_error", tag=tag, error=str(e))
            return []

        # On 401, retry once after forcing a refresh.
        if resp.status_code == 401:
            log.info("inoreader.access_token.401_retry", tag=tag)
            self._access_token = None
            try:
                access = self._refresh_access_token()
            except InoreaderAuthError:
                raise
            headers["Authorization"] = f"Bearer {access}"
            resp = httpx.get(url, params=params, headers=headers, timeout=self.timeout_sec)

        if resp.status_code != 200:
            log.error(
                "inoreader.fetch.bad_status",
                tag=tag,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return []

        body = resp.json()
        items = body.get("items") or []
        if not isinstance(items, list):
            log.warning("inoreader.fetch.bad_items", tag=tag, type=type(items).__name__)
            return []
        return items


# ---- bootstrap helpers (used by scripts/inoreader_oauth_bootstrap.py) -----


def build_authorization_url(app_id: str, *, redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
                            scope: str = "read", state: str = "news_agent") -> str:
    """Construct the OAuth authorization URL the user visits in browser."""
    from urllib.parse import urlencode
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return f"{OAUTH_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(
    *,
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
    timeout_sec: float = 20.0,
) -> dict:
    """Exchange the authorization code for access/refresh tokens.

    Returns the raw JSON body. Raises InoreaderAuthError on non-200.
    """
    resp = httpx.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=timeout_sec,
    )
    if resp.status_code != 200:
        raise InoreaderAuthError(
            f"oauth/token (code exchange) returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()
