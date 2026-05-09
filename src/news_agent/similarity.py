from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

DEFAULT_THRESHOLD = 0.30
DEFAULT_K = 3

_PUNCT_RE = re.compile(r"[^\w\s぀-ヿ一-鿿]")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """NFKC-normalize, lowercase, strip punctuation. Preserves CJK and word chars."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def shingles(text: str, k: int = DEFAULT_K) -> set[str]:
    """Return word tokens + character k-grams. Robust to mixed JP/EN titles."""
    text = normalize(text)
    if not text:
        return set()
    tokens = {tok for tok in text.split() if tok}
    compact = text.replace(" ", "")
    grams: set[str] = set()
    if len(compact) >= k:
        grams = {compact[i : i + k] for i in range(len(compact) - k + 1)}
    return tokens | grams


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_duplicate(
    title: str,
    recent_titles: Iterable[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> bool:
    a = shingles(title)
    if not a:
        return False
    for t in recent_titles:
        if jaccard(a, shingles(t)) >= threshold:
            return True
    return False
