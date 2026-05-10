"""Fire two Claude Research calls (current vs minimal prompt) and compare.

Bypasses cadence by passing store=None. Both calls dump full responses to
logs/claude_research/. Prints a side-by-side comparison to stdout.

Usage:
    .venv/bin/python scripts/probe_claude_research.py

Cost: roughly $0.60-$2.00 total (two Opus 4.7 + web_search calls).
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

from news_agent.sources.claude_research import (  # noqa: E402
    PROMPT_TEMPLATE,
    ClaudeResearchSource,
)

MINIMAL_PROMPT = """\
Use web_search to find insurance-sector news headlines from the last 24 hours.
Cover Japan-focused stories AND global capital-markets, M&A, regulation, and ratings news.
Search broadly with multiple distinct queries.

Return JSON only — no prose, no markdown fences:
{{
  "headlines": [
    {{
      "title": "...",
      "url": "https://...",
      "source": "...",
      "published_at": "ISO 8601 with timezone",
      "summary_ja": "1-2 sentence Japanese summary"
    }}
  ]
}}

Up to {max_headlines} headlines. Skip duplicates of the same event.
"""


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
        rows.append((age_str, it.title, it.url))

    print(f"fresh (<=24h): {fresh}/{len(items)}")
    print("first 10 (age, title):")
    for age, title, url in rows[:10]:
        print(f"  {age}  {title[:100]}")
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

    print("Probing Claude Research (current prompt + minimal prompt)...")
    print("Cost estimate: ~$0.60-$2.00. Two calls, sequential.\n")

    current = ClaudeResearchSource(
        name="probe_current",
        api_key=api_key,
        store=None,                  # bypass cadence
        max_headlines=30,
        max_search_uses=12,
    )
    items_current = current.fetch()

    minimal = ClaudeResearchSource(
        name="probe_minimal",
        api_key=api_key,
        store=None,
        max_headlines=30,
        max_search_uses=12,
        prompt_override=MINIMAL_PROMPT,
    )
    items_minimal = minimal.fetch()

    _summarise("CURRENT PROMPT", items_current, _latest_dump("probe_current__"))
    _summarise("MINIMAL PROMPT", items_minimal, _latest_dump("probe_minimal__"))

    urls_current = {it.url for it in items_current}
    urls_minimal = {it.url for it in items_minimal}
    overlap = urls_current & urls_minimal
    print("\n=========== DIFF ===========")
    print(f"current = {len(urls_current)}, minimal = {len(urls_minimal)}, overlap = {len(overlap)}")
    only_minimal = urls_minimal - urls_current
    if only_minimal:
        print(f"\nURLs only in MINIMAL ({len(only_minimal)}):")
        for u in list(only_minimal)[:10]:
            print(f"  {u}")
    only_current = urls_current - urls_minimal
    if only_current:
        print(f"\nURLs only in CURRENT ({len(only_current)}):")
        for u in list(only_current)[:10]:
            print(f"  {u}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
