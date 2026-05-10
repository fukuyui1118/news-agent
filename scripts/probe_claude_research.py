"""Compare Claude Research prompt strategies side-by-side.

Fires two real Anthropic calls (cadence bypassed via store=None) and prints
a comparison: parsed item counts, fresh-rate (<=24h), bucket distribution,
URL overlap. Both responses are persisted under logs/claude_research/.

Usage:
    .venv/bin/python scripts/probe_claude_research.py

Cost: roughly $0.80-$2.50 total (two Opus 4.7 + web_search + Haiku).
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
    bucket_counts: dict[str, int] = {}
    for it in items:
        if it.published_at is not None:
            age_h = (now - it.published_at).total_seconds() / 3600
            if age_h <= 24:
                fresh += 1
            age_str = f"{age_h:6.1f}h"
        else:
            age_str = "  ?  "
        rows.append((age_str, it.title, it.url, it.source))
        # Bucket label is encoded in raw_text as "[A] ..." for bucket-XML
        if it.raw_text and it.raw_text.startswith("["):
            letter = it.raw_text[1:2]
            bucket_counts[letter] = bucket_counts.get(letter, 0) + 1

    print(f"fresh (<=24h): {fresh}/{len(items)}")
    if bucket_counts:
        print("bucket distribution: " + ", ".join(
            f"{k}={v}" for k, v in sorted(bucket_counts.items())
        ))
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

    print("Probing Claude Research strategies (two_stage vs bucket_xml)...")
    print("Cost estimate: ~$0.80-$2.50. Sequential calls.\n")

    two_stage = ClaudeResearchSource(
        name="probe_two_stage",
        api_key=api_key,
        store=None,                  # bypass cadence
        max_headlines=30,
        max_search_uses=12,
        prompt_strategy="two_stage",
    )
    items_two_stage = two_stage.fetch()

    bucket_xml = ClaudeResearchSource(
        name="probe_bucket_xml",
        api_key=api_key,
        store=None,
        max_headlines=30,
        max_search_uses=12,
        prompt_strategy="bucket_xml",
    )
    items_bucket = bucket_xml.fetch()

    _summarise("TWO_STAGE (current default)", items_two_stage, _latest_dump("probe_two_stage__"))
    _summarise("BUCKET_XML (new strategy)", items_bucket, _latest_dump("probe_bucket_xml__"))

    urls_a = {it.url for it in items_two_stage}
    urls_b = {it.url for it in items_bucket}
    overlap = urls_a & urls_b
    print("\n=========== DIFF ===========")
    print(f"two_stage = {len(urls_a)}, bucket_xml = {len(urls_b)}, overlap = {len(overlap)}")

    only_b = urls_b - urls_a
    if only_b:
        print(f"\nURLs only in BUCKET_XML ({len(only_b)}):")
        for u in list(only_b)[:10]:
            print(f"  {u}")
    only_a = urls_a - urls_b
    if only_a:
        print(f"\nURLs only in TWO_STAGE ({len(only_a)}):")
        for u in list(only_a)[:10]:
            print(f"  {u}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
