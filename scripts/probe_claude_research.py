"""One-shot probe of the Claude Research two-stage pipeline.

Fires a single real Anthropic call (cadence bypassed via store=None) and prints
parsed item count, fresh-rate (<=24h), and a sample of headlines. The raw
discovery + structuring responses are persisted under logs/claude_research/.

Usage:
    .venv/bin/python scripts/probe_claude_research.py

Cost: roughly $0.40-$1.20 (one Opus 4.7 + web_search call + one Haiku call).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from news_agent.config import load_watchlists  # noqa: E402
from news_agent.sources.claude_research import (  # noqa: E402
    ClaudeResearchSource,
)


def _summarise(label: str, items, response_path_hint: str = ""):
    print(f"\n=========== {label} ===========")
    print(f"parsed headlines: {len(items)}")
    if response_path_hint:
        print(f"raw dump: {response_path_hint}")
    if not items:
        print("(no items)")
        return

    now = datetime.now(timezone.utc)
    fresh = 0
    rows = []
    for it in items:
        if it.published_at is not None:
            age_h = (now - it.published_at).total_seconds() / 3600
            if age_h <= 24:
                fresh += 1
            age_str = f"{age_h:6.1f}h"
        else:
            age_str = "  ?  "
        rows.append((age_str, it.title, it.url, it.source))

    print(f"fresh (<=24h): {fresh}/{len(items)}")
    print("first 12 (age, source, title):")
    for age, title, url, source in rows[:12]:
        print(f"  {age}  [{source[:18]:18}]  {title[:80]}")
        print(f"           {url}")


def _latest_dump(prefix: str) -> str:
    d = ROOT / "logs" / "claude_research"
    if not d.exists():
        return ""
    files = sorted(d.glob(f"{prefix}*.json"))
    return str(files[-1]) if files else ""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY missing in env", file=sys.stderr)
        return 2

    print("Probing Claude Research two-stage pipeline...")
    print("Cost estimate: ~$0.40-$1.20.\n")

    watchlists = load_watchlists(ROOT / "config" / "watchlists.yaml")

    src = ClaudeResearchSource(
        name="probe_two_stage",
        api_key=api_key,
        watchlists=watchlists,
        store=None,                  # bypass cadence
        max_headlines=30,
        max_search_uses=12,
    )
    items = src.fetch()

    _summarise("CLAUDE RESEARCH (two-stage)", items, _latest_dump("probe_two_stage__"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
