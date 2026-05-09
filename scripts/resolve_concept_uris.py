"""One-shot setup: resolve every watchlist entity to a NewsAPI.ai concept URI.

For each canonical name in `config/watchlists.yaml`, calls
  https://eventregistry.org/api/v1/suggestConceptsFast?prefix=<name>&conceptType=org

and picks the highest-score `org` match. Writes results to
`config/concept_uris.yaml`.

Idempotent — entries already resolved are skipped on re-run. To force a refresh
of one entity, remove its line from `concept_uris.yaml::resolved` before running.

Usage:
    .venv/bin/python scripts/resolve_concept_uris.py

Counts against your monthly NewsAPI.ai budget (one call per unresolved entity)
but is one-time / idempotent. Re-run after editing `watchlists.yaml`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import yaml

# Allow running from project root: .venv/bin/python scripts/resolve_concept_uris.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from news_agent.config import (  # noqa: E402
    ConceptUris,
    Secrets,
    load_concept_uris,
    load_watchlists,
)
from news_agent.store import Store  # noqa: E402

WATCHLISTS_PATH = ROOT / "config" / "watchlists.yaml"
CONCEPT_URIS_PATH = ROOT / "config" / "concept_uris.yaml"
DB_PATH = ROOT / "seen.db"

ENDPOINT = "https://eventregistry.org/api/v1/suggestConceptsFast"
TIMEOUT = 30.0
MIN_SCORE = 50  # filter out very loose matches


def resolve_one(canonical: str, api_key: str) -> str | None:
    """Return the best org URI for `canonical`, or None if no good match."""
    params = {
        "prefix": canonical,
        "source": "concepts",
        "lang": "eng",
        "conceptLang": "eng",
        "conceptType": "org",
        "count": 5,
        "apiKey": api_key,
    }
    try:
        resp = httpx.get(ENDPOINT, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"  ! {canonical}: request failed: {e}")
        return None

    candidates = data if isinstance(data, list) else data.get("results") or []
    org_candidates = [
        c for c in candidates
        if c.get("type") == "org" and (c.get("score") or 0) >= MIN_SCORE
    ]
    if not org_candidates:
        return None
    org_candidates.sort(key=lambda c: c.get("score") or 0, reverse=True)
    return org_candidates[0].get("uri")


def main() -> int:
    secrets = Secrets()
    if not secrets.newsapi_ai_key:
        print("ERROR: NEWSAPI_AI_KEY not set in .env", file=sys.stderr)
        return 1

    watchlists = load_watchlists(WATCHLISTS_PATH)
    if CONCEPT_URIS_PATH.exists():
        existing = load_concept_uris(CONCEPT_URIS_PATH)
    else:
        existing = ConceptUris()

    all_canonicals = [e.canonical for e in watchlists.p1_japan + watchlists.p2_global]

    to_resolve = [c for c in all_canonicals if c not in existing.resolved]
    if not to_resolve:
        print(f"All {len(all_canonicals)} entities already resolved. Nothing to do.")
        return 0

    print(f"Resolving {len(to_resolve)} entities (skipping {len(existing.resolved)} cached)...")

    store = Store(DB_PATH)
    resolved = dict(existing.resolved)
    unresolved = list(existing.unresolved)
    new_resolved = 0
    new_unresolved = 0

    try:
        for name in to_resolve:
            print(f"  • {name:35} ", end="", flush=True)
            uri = resolve_one(name, secrets.newsapi_ai_key)
            store.record_api_call(
                provider="newsapi.ai",
                endpoint="suggestConceptsFast",
                query_name=f"resolve:{name}",
                article_count=None,
                http_status=200 if uri else None,
            )
            if uri:
                resolved[name] = uri
                new_resolved += 1
                print(f"→ {uri}")
            else:
                if name not in unresolved:
                    unresolved.append(name)
                new_unresolved += 1
                print("(no clean match — fallback to keyword)")
    finally:
        store.close()

    out = {"resolved": resolved, "unresolved": unresolved}
    with open(CONCEPT_URIS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=True)
    print()
    print(f"Wrote {CONCEPT_URIS_PATH}: {len(resolved)} resolved, {len(unresolved)} unresolved.")
    print(f"  This run: {new_resolved} newly resolved, {new_unresolved} marked unresolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
