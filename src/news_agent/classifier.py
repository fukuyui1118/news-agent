from __future__ import annotations

import re
from dataclasses import dataclass

from .config import WatchlistEntry, Watchlists


@dataclass
class Match:
    priority: str
    canonical: str
    matched_alias: str


_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def _build_pattern(term: str) -> re.Pattern[str]:
    if _ASCII_RE.match(term):
        return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def _match_entry(text: str, entry: WatchlistEntry) -> str | None:
    for excl in entry.exclude:
        if _build_pattern(excl).search(text):
            return None
    for alias in [entry.canonical, *entry.aliases]:
        if _build_pattern(alias).search(text):
            return alias
    return None


def classify(text: str, watchlists: Watchlists) -> Match:
    for entry in watchlists.p1_japan:
        alias = _match_entry(text, entry)
        if alias:
            return Match(priority="P1", canonical=entry.canonical, matched_alias=alias)
    for entry in watchlists.p2_global:
        alias = _match_entry(text, entry)
        if alias:
            return Match(priority="P2", canonical=entry.canonical, matched_alias=alias)
    return Match(priority="P3", canonical="", matched_alias="")
