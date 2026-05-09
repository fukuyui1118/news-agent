from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Relevance


@dataclass
class GateResult:
    relevant: bool
    reason: str


_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def _build_pattern(term: str) -> re.Pattern[str]:
    if _ASCII_RE.match(term):
        return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def is_relevant(text: str, source_tier: int, relevance: Relevance) -> GateResult:
    if source_tier == 1:
        return GateResult(True, "tier-1 source, gate skipped")
    for kw in relevance.business_keywords:
        if _build_pattern(kw).search(text):
            return GateResult(True, f"keyword:{kw}")
    return GateResult(False, "no business keyword found")
