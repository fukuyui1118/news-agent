"""One-time interactive helper to obtain an Inoreader OAuth refresh token.

Workflow:
  1. Register an application at https://www.inoreader.com/developers/.
     - Redirect URI: urn:ietf:wg:oauth:2.0:oob (out-of-band)
     - Scope: read
  2. Set INOREADER_APP_ID and INOREADER_APP_SECRET in your shell or .env.
  3. Run this script:
        .venv/bin/python scripts/inoreader_oauth_bootstrap.py
     It prints an authorization URL.
  4. Open the URL in a browser, log into Inoreader, click "Allow".
     Inoreader displays an authorization code on a blank page.
  5. Paste the code back into this script. It exchanges the code for
     tokens and prints the long-lived refresh token.
  6. Add the refresh token to .env on both your laptop and EC2:
        INOREADER_REFRESH_TOKEN=<paste here>

The agent picks this up automatically; no other code changes needed.
"""
from __future__ import annotations

import os
import sys
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

    auth_url = build_authorization_url(app_id)
    print()
    print("=" * 70)
    print("STEP 1 — open this URL in your browser, log in, click Allow:")
    print("=" * 70)
    print()
    print(auth_url)
    print()
    print("=" * 70)
    print("STEP 2 — Inoreader will display a code on a blank page.")
    print("           Copy it and paste below.")
    print("=" * 70)
    print()
    code = input("Authorization code: ").strip()
    if not code:
        print("No code entered; aborting.", file=sys.stderr)
        return 2

    try:
        tokens = exchange_code_for_tokens(
            app_id=app_id, app_secret=app_secret, code=code
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
    print(f"access_token     (1 hr TTL):  {access[:24] if access else '<none>'}...")
    print(f"expires_in:                   {expires} sec")
    print()
    print("REFRESH TOKEN — add this line to .env on laptop and EC2:")
    print()
    print(f"INOREADER_REFRESH_TOKEN={refresh}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
