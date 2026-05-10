"""AI-based classifier: one Claude Opus call per cycle assigns P1/P2 to
freshly-fetched items. Items not in either bucket are implicitly P3.

Replaces the layered regex pipeline (`classifier.py` watchlist match +
`relevance.py` keyword gate) with a single LLM judgment that handles the
"is this business-relevant insurance news" question coherently.

Returns a dict `{idx: priority}` mapping. Indices missing from the dict are
P3. On any parse / API failure, returns {} — caller treats all items as P3
(safe default; preserves visibility in the dashboard but skips emailing).
"""
from __future__ import annotations

import json
import re
import time

import structlog
from anthropic import Anthropic

from .config import Watchlists
from .sources.base import RawItem

log = structlog.get_logger()

DEFAULT_CLASSIFIER_MODEL = "claude-opus-4-7"

CLASSIFIER_PROMPT_TEMPLATE = """\
あなたは保険・再保険セクターを担当するシニア・ニュース・エディターです。
東京の機関投資家デスク向けに、以下のヘッドラインを P1 / P2 / それ以外 (=P3)
に分類してください。

# 分類基準
- P1: 日本の保険会社（{p1_entities}）、または金融規制・財務に関する重要ニュース。
       格付けアクション、資本市場取引（社債・劣後債・ハイブリッド・CATボンド・IPO）、
       M&A、戦略出資・政策保有株売却、金融庁/EIOPA/NAIC/PRA/IAIS/BMA等の規制動向、
       大型損害事象。
- P2: グローバル保険会社（{global_entities}）の事業ニュース。決算、経営体制変更、
       商品、再保険更改条件など。Japanの規制対象でないもの。
- P3 (=出力に含めない): 上記以外。スポーツスポンサー、コンサート会場の名称イベント、
       CSR、表彰、無関係な広告、求人、ノイズ。

# ヘッドライン一覧（インデックス付き）
{rows_text}

# 出力（JSONのみ。前置き・コードフェンス無し）
{{
  "p1": [<該当するインデックス>],
  "p2": [<該当するインデックス>]
}}

P3 は出力に含めません。p1 と p2 のどちらにも入らないインデックスが
自動的に P3 と判定されます。"""


def classify_items(
    items: list[RawItem],
    watchlists: Watchlists,
    *,
    api_key: str,
    model: str = DEFAULT_CLASSIFIER_MODEL,
) -> dict[int, str]:
    """Single Opus call. Returns {idx: "P1"|"P2"} for matched items.

    Indices missing from the returned dict are implicitly P3.

    On parse/API failure returns {} — caller persists everything as P3
    (safer than over-promoting on a bad classify).
    """
    if not items:
        return {}

    p1_names = ", ".join(e.canonical for e in watchlists.p1_japan) or "(未指定)"
    global_names = ", ".join(e.canonical for e in watchlists.p2_global) or "(未指定)"

    rows_text = "\n".join(
        f"[{idx}] {item.title} | source={item.source}"
        for idx, item in enumerate(items)
    )
    prompt = CLASSIFIER_PROMPT_TEMPLATE.format(
        p1_entities=p1_names,
        global_entities=global_names,
        rows_text=rows_text,
    )

    client = Anthropic(api_key=api_key)
    t0 = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.error("ai_classifier.api.failed", error=f"{type(e).__name__}: {e}")
        return {}

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    text = "".join(
        (b.text or "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    parsed = _parse_classifier_json(text)
    if parsed is None:
        log.error("ai_classifier.parse.failed", preview=text[:300])
        return {}

    p1_set = {int(i) for i in parsed.get("p1", []) if isinstance(i, (int, float))}
    p2_set = {int(i) for i in parsed.get("p2", []) if isinstance(i, (int, float))}
    # P1 wins if Claude lists an index in both arrays.
    out: dict[int, str] = {idx: "P2" for idx in p2_set}
    out.update({idx: "P1" for idx in p1_set})
    # Drop indices outside the input range.
    out = {idx: pri for idx, pri in out.items() if 0 <= idx < len(items)}

    log.info(
        "ai_classifier.done",
        input_items=len(items),
        p1=sum(1 for v in out.values() if v == "P1"),
        p2=sum(1 for v in out.values() if v == "P2"),
        p3=len(items) - len(out),
        elapsed_ms=elapsed_ms,
        model=model,
    )
    return out


def _parse_classifier_json(text: str) -> dict | None:
    """Extract a JSON object with `p1` / `p2` arrays. Tolerates code fences
    and surrounding prose. Returns None on hard parse failure."""
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj
