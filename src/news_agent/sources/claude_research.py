"""Claude Opus 4.7 + web_search as a curated insurance-news research source.

Each instance = one "research query" (e.g. "JP-focused insurance research").
Fires at most once per `cadence_hours`; cadence is enforced by checking the
api_usage table for the most recent successful call of this query_name.

Output: Claude returns JSON conforming to a fixed schema. We map each
headline to a RawItem; classifier+relevance gate run downstream.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import structlog
from anthropic import Anthropic

from .base import RawItem, Source

log = structlog.get_logger()

PROVIDER = "anthropic"

# Tool schema. Event-Registry-style versioned name comes from Anthropic docs.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# Japanese-output prompt. Kept inline because it embeds the schema and runs
# every time the source fires.
PROMPT_TEMPLATE = """\
あなたは保険・再保険業界を専門とするシニア金融ニュース調査員です。
日本市場と、グローバルの資本市場・規制・M&A・格付け動向に重点を置きます。

`web_search` ツールを使用して、過去24時間以内に公開されたニュースのうち、
以下のテーマに該当するヘッドラインを収集してください:
  - 日本の保険会社（東京海上、損保ジャパン、MS&AD、第一生命、日本生命、
    明治安田生命、住友生命、かんぽ生命、トーア再保険、T&D、ソニー生命、
    アフラック生命 など）
  - グローバル大手保険会社（Allianz、AXA、Zurich、Munich Re、Swiss Re、
    AIG、Chubb、MetLife、Manulife、Lloyd's、Berkshire再保険 など）
  - 資本市場イベント: IPO、M&A、増資、自社株買い
  - 規制動向: 金融庁、EIOPA、NAIC、格付け機関の動き
  - 格付け変更（上方/下方修正、Outlook変更）
  - 戦略動向: 市場参入/撤退、提携、ジョイントベンチャー

5〜15個の異なる検索クエリで、日本＋グローバル＋業界トレンドを幅広くカバー
してください。報道機関、IRページ、規制当局のサイトを優先します。

出力は以下のJSONスキーマに完全準拠させ、その他のテキスト（前置き、Markdown
フェンス等）は一切含めないでください:

{{
  "headlines": [
    {{
      "title": "記事の正確なタイトル（原語のまま）",
      "url": "https://full-url",
      "source": "Reuters | Nikkei | Bloomberg | ...",
      "published_at": "ISO 8601 タイムゾーン付き、例: 2026-05-10T08:30:00Z",
      "category": "japan_carrier | global_carrier | m_and_a | regulatory | rating | capital_market | strategy | other",
      "summary_ja": "1〜2文の日本語要約（財務・戦略事実を中心に）"
    }}
  ]
}}

最大{max_headlines}件まで。同一イベントの重複（複数媒体の同記事）は1件に統合。
24時間より古い記事は除外してください。"""


def _strip_json_fences(text: str) -> str:
    """Pull a JSON object out of a Claude response.

    Handles three observed cases:
      1. plain JSON
      2. ```json ... ``` markdown fences
      3. preamble prose + JSON (Claude sometimes ignores 'no prose' instruction)
    """
    text = text.strip()
    # Case 2: markdown fences
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Case 3: anything wrapped around { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class ClaudeResearchSource(Source):
    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        model: str = "claude-opus-4-7",
        cadence_hours: int = 12,
        tier: int = 1,
        max_headlines: int = 30,
        max_search_uses: int = 12,
        store=None,        # for cadence check + api_usage logging
    ) -> None:
        self.name = name
        self.tier = tier
        self.api_key = api_key
        self.model = model
        self.cadence_hours = cadence_hours
        self.max_headlines = max_headlines
        self.max_search_uses = max_search_uses
        self.store = store

    # ---- cadence ----------------------------------------------------------

    def _within_cadence(self) -> bool:
        if self.store is None:
            return False
        last = self.store.last_successful_call_at(
            provider=PROVIDER, query_name=self.name
        )
        if last is None:
            return False
        age = datetime.now(timezone.utc) - last
        return age < timedelta(hours=self.cadence_hours)

    # ---- fetch -----------------------------------------------------------

    def fetch(self) -> list[RawItem]:
        if not self.api_key:
            log.warning("claude_research.skipped", name=self.name, reason="no_api_key")
            return []
        if self._within_cadence():
            last = self.store.last_successful_call_at(
                provider=PROVIDER, query_name=self.name
            )
            log.info(
                "claude_research.skipped",
                name=self.name,
                reason="cadence",
                last_call=last.isoformat() if last else None,
                cadence_hours=self.cadence_hours,
            )
            return []

        prompt = PROMPT_TEMPLATE.format(max_headlines=self.max_headlines)

        client = Anthropic(api_key=self.api_key)
        t0 = time.monotonic()
        http_status = None
        article_count = 0
        error: str | None = None

        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=8192,
                tools=[{**WEB_SEARCH_TOOL, "max_uses": self.max_search_uses}],
                messages=[{"role": "user", "content": prompt}],
            )
            http_status = 200
            items = self._parse_response(resp)
            article_count = len(items)
        except BaseException as e:
            error = f"{type(e).__name__}: {e}"
            log.error("claude_research.failed", name=self.name, error=error)
            items = []
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if self.store is not None:
                self.store.record_api_call(
                    provider=PROVIDER,
                    endpoint="getArticles_research",
                    query_name=self.name,
                    article_count=article_count,
                    elapsed_ms=elapsed_ms,
                    http_status=http_status,
                    error=error,
                )

        log.info(
            "claude_research.done",
            name=self.name,
            article_count=article_count,
            elapsed_ms=elapsed_ms,
            error=error,
        )
        return items

    def _parse_response(self, resp) -> list[RawItem]:
        # Find the final text block (Claude appends prose after tool_use blocks).
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text = block.text or ""
        text = _strip_json_fences(text)
        if not text:
            log.warning("claude_research.empty_text", name=self.name)
            return []

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.error(
                "claude_research.json_parse_failed",
                name=self.name,
                error=str(e),
                preview=text[:300],
            )
            return []

        headlines = data.get("headlines") or []
        items: list[RawItem] = []
        for h in headlines:
            url = (h.get("url") or "").strip()
            title = (h.get("title") or "").strip()
            if not url or not title:
                continue
            published_at = _parse_iso(h.get("published_at"))
            summary_ja = (h.get("summary_ja") or "").strip()
            # Body = JP summary (the classifier scans title+raw_text for entities;
            # JP names like 東京海上 will match watchlist aliases).
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    published_at=published_at,
                    source=self.name,
                    raw_text=summary_ja,
                    source_tier=self.tier,
                )
            )
        return items


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
