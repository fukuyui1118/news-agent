"""Cross-product query generator for Google News RSS.

Every (P1 + P2 entity in watchlists.yaml) × (bucket in query_buckets.yaml) →
one Google News query, scoped to the last `recency_hours` via Google's `when:`
operator. No filtering, no opt-in flags — every entity is covered.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Bucket, Watchlists, WatchlistEntry


@dataclass
class GeneratedQuery:
    entity_canonical: str
    entity_priority: str  # "P1" or "P2"
    bucket_name: str
    query: str  # ready to URL-encode


def _join_or(terms: list[str]) -> str:
    """OR-join terms, wrapping multi-word ASCII terms in quotes for phrase match."""
    out: list[str] = []
    for raw in terms:
        t = raw.strip()
        if not t:
            continue
        if " " in t and t.isascii() and not (t.startswith('"') and t.endswith('"')):
            out.append(f'"{t}"')
        else:
            out.append(t)
    return "(" + " OR ".join(out) + ")"


def build_query(entry: WatchlistEntry, bucket: Bucket, recency_hours: int) -> str:
    entity_terms = [entry.canonical] + list(entry.aliases)
    return f"{_join_or(entity_terms)} {_join_or(bucket.keywords)} when:{recency_hours}h"


def generate_google_news_queries(
    *,
    watchlists: Watchlists,
    buckets: list[Bucket],
    recency_hours: int = 24,
) -> list[GeneratedQuery]:
    """Generate one query per (entity, bucket) pair across both P1 and P2.

    Coverage guarantee: every entry in `watchlists.p1_japan` and
    `watchlists.p2_global` produces exactly `len(buckets)` queries. No
    filtering. The unit test enforces `len(out) == (P1+P2) * len(buckets)`.
    """
    out: list[GeneratedQuery] = []
    for entry in watchlists.p1_japan:
        for bucket in buckets:
            out.append(
                GeneratedQuery(
                    entity_canonical=entry.canonical,
                    entity_priority="P1",
                    bucket_name=bucket.name,
                    query=build_query(entry, bucket, recency_hours),
                )
            )
    for entry in watchlists.p2_global:
        for bucket in buckets:
            out.append(
                GeneratedQuery(
                    entity_canonical=entry.canonical,
                    entity_priority="P2",
                    bucket_name=bucket.name,
                    query=build_query(entry, bucket, recency_hours),
                )
            )
    return out
