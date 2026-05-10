"""One-time interactive helper to obtain an Inoreader OAuth refresh token.

Uses the standard installed-app pattern: spins up a one-shot HTTP server on
http://localhost:8765/callback, opens the auth URL in the browser, captures
the redirect, exchanges the code for tokens, prints the refresh token.

Workflow:
  1. Register an application at https://www.inoreader.com/developers/.
     - Redirect URI (paste EXACTLY): http://localhost:8765/callback
     - Scope: read
  2. Set INOREADER_APP_ID and INOREADER_APP_SECRET in your shell or .env.
  3. Run this script:
        .venv/bin/python scripts/inoreader_oauth_bootstrap.py
  4. The script opens the auth URL in your default browser. Log in to
     Inoreader and click Allow. The browser briefly hits localhost,
     which the script captures and acknowledges with a success page.
  5. The script prints the long-lived refresh token. Add it to .env
     on both your laptop and EC2:
        INOREADER_REFRESH_TOKEN=<paste here>

The agent picks this up automatically; no other code changes needed.
"""
from __future__ import annotations

import http.server
import os
import secrets as _secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from news_agent.inoreader_oauth import (  # noqa: E402
    InoreaderAuthError,
    build_authorization_url,
    exchange_code_for_tokens,
)

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"

SUCCESS_HTML = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Inoreader OAuth complete</title></head>
<body style="font-family: -apple-system, sans-serif; padding: 4em; max-width: 40em">
<h2>✅ Authorization captured</h2>
<p>You can close this tab. Return to the terminal to see your refresh token.</p>
</body></html>
"""

ERROR_HTML_TEMPLATE = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OAuth error</title></head>
<body style="font-family: -apple-system, sans-serif; padding: 4em; max-width: 40em">
<h2>❌ OAuth error</h2>
<pre>{detail}</pre>
<p>Return to the terminal for diagnostics.</p>
</body></html>
"""


class _State:
    """Mutable holder shared between the HTTP handler and the main thread."""
    code: str | None = None
    state: str | None = None
    error: str | None = None
    expected_state: str = ""


def _build_handler(holder: _State):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence default access log
            return

        def do_GET(self):  # noqa: N802 — name dictated by stdlib
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            err = (qs.get("error") or [None])[0]
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            if err:
                holder.error = f"Inoreader returned error={err}: {qs.get('error_description', [''])[0]}"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(ERROR_HTML_TEMPLATE.replace(b"{detail}", holder.error.encode()))
                return
            if not code:
                holder.error = "callback missing 'code' query parameter"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(ERROR_HTML_TEMPLATE.replace(b"{detail}", holder.error.encode()))
                return
            if state != holder.expected_state:
                holder.error = (
                    f"state mismatch (CSRF check): expected {holder.expected_state!r}, "
                    f"got {state!r}"
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(ERROR_HTML_TEMPLATE.replace(b"{detail}", holder.error.encode()))
                return
            holder.code = code
            holder.state = state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(SUCCESS_HTML)

    return Handler


def main() -> int:
    app_id = os.environ.get("INOREADER_APP_ID", "").strip()
    app_secret = os.environ.get("INOREADER_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print(
            "ERROR: set INOREADER_APP_ID and INOREADER_APP_SECRET in your environment\n"
            "or .env before running this script. Get them from\n"
            "https://www.inoreader.com/developers/ after registering an app.",
            file=sys.stderr,
        )
        return 2

    holder = _State()
    holder.expected_state = _secrets.token_urlsafe(16)

    auth_url = build_authorization_url(
        app_id, redirect_uri=REDIRECT_URI, state=holder.expected_state
    )

    # Bind the server first so we know the port is free before opening the browser.
    try:
        server = socketserver.TCPServer(
            (CALLBACK_HOST, CALLBACK_PORT), _build_handler(holder)
        )
    except OSError as e:
        print(
            f"ERROR: cannot bind {CALLBACK_HOST}:{CALLBACK_PORT}: {e}\n"
            f"Is something else using the port? Try: lsof -ti :{CALLBACK_PORT} | xargs kill",
            file=sys.stderr,
        )
        return 1

    print()
    print("=" * 70)
    print("Inoreader OAuth bootstrap")
    print("=" * 70)
    print(f"Listening for callback on {REDIRECT_URI} ...")
    print()
    print("Opening this URL in your browser (Cmd-click if it doesn't open):")
    print()
    print(f"  {auth_url}")
    print()
    print("Log in to Inoreader and click Allow. The browser will redirect to")
    print(f"{REDIRECT_URI} (this script captures it).")
    print()

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        webbrowser.open(auth_url)
    except Exception:
        # webbrowser failure isn't fatal — user can copy-paste the URL.
        pass

    # Wait for the callback. The handler sets holder.code or holder.error.
    print("Waiting for OAuth callback (Ctrl-C to abort) ...")
    try:
        while holder.code is None and holder.error is None:
            server_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        server.shutdown()
        return 130

    server.shutdown()
    server.server_close()

    if holder.error:
        print(f"\nERROR: {holder.error}", file=sys.stderr)
        return 1

    print("\nAuthorization code received. Exchanging for tokens ...")
    try:
        tokens = exchange_code_for_tokens(
            app_id=app_id,
            app_secret=app_secret,
            code=holder.code,
            redirect_uri=REDIRECT_URI,
        )
    except InoreaderAuthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    expires = tokens.get("expires_in")
    if not refresh:
        print(f"ERROR: response missing refresh_token: {tokens!r}", file=sys.stderr)
        return 1

    print()
    print("=" * 70)
    print("SUCCESS — tokens obtained.")
    print("=" * 70)
    print()
    print(f"access_token (1 hr TTL):  {(access or '<none>')[:24]}...")
    print(f"expires_in:               {expires} sec")
    print()
    print("REFRESH TOKEN — add this line to .env on laptop and EC2:")
    print()
    print(f"INOREADER_REFRESH_TOKEN={refresh}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
