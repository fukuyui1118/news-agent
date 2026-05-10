"""Claude Opus 4.7 + web_search as a curated insurance-news research source.

Each instance = one "research query" (e.g. "JP-focused insurance research").
Fires at most once per `cadence_hours`; cadence is enforced by checking the
api_usage table for the most recent successful call of this query_name.

Two-stage pipeline:
  Stage 1 (discovery)   — Opus + web_search, free-text bullet list.
                          Watchlist entities are injected from YAML at runtime.
                          Ends with a COVERAGE_NOTES block (searches_run,
                          tier1_aggregators_hit, fallback_used, gaps).
  Stage 2 (structuring) — Haiku, no tools. Converts bullets to strict JSON
                          with category, tier, published_confidence fields.

Output: classifier+relevance gate run downstream on each RawItem.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog
from anthropic import Anthropic

from .base import RawItem, Source

if TYPE_CHECKING:
    from ..config import Watchlists

# Where raw Claude responses are persisted (one file per successful call).
# Created lazily on first dump; logs/ is already gitignored at repo root.
RESPONSE_DUMP_DIR = Path("logs/claude_research")

log = structlog.get_logger()

PROVIDER = "anthropic"

# Tool schema. Versioned name comes from Anthropic docs.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


# Stage 1 — discovery prompt. Opus + web_search; emits a bullet list with
# tier/category/confidence labels and a trailing COVERAGE_NOTES block.
DISCOVERY_PROMPT_TEMPLATE = """\
あなたは保険・再保険セクターを担当するシニア金融ニュース調査員です。
東京の機関投資家デスク向けに、トレード判断に資するヘッドラインを収集します。

# 時間範囲（厳守）
基準時刻（JST）: {now_jst}
収集対象: {since_iso} 〜 {until_iso}

このウインドウ外の記事は最終出力から除外します。ただし、ウインドウ内の
記事が5件未満となった場合に限り、直近72時間まで遡って補完してよい。
補完した記事には要約末尾に「[T-{{時間}}h]」を付記すること。

# 優先度（検索予算の配分指針）
Tier 1（必ず探す・見つけたら必ず収録）:
  - 格付けアクション（AM Best / S&P / Moody's / Fitch / R&I / JCR）
  - 資本市場取引（社債・劣後債・ハイブリッド・CATボンド・サイドカー・IPO）
  - M&A、戦略出資、政策保有株売却
  - 金融庁・EIOPA・NAIC・PRA・BMA・IAIS の規制アクションや行政処分
  - 大型損害事象、災害発生に伴う保険金支払見込み

Tier 2（余力があれば収録）:
  - 決算・業績見通し修正
  - 経営体制変更、CEO/CFO 交代
  - 再保険更改の条件
  - 新商品の開示

Tier 3（除外）:
  - 支店開設、スポンサー、CSR、表彰、セミナー告知

# 監視対象エンティティ
日本Tier 1: {p1_entities}
グローバル: {global_entities}

# 検索戦略
合計 8〜15 クエリを目安に、以下の順序で実行:

1. アグリゲータ・スイープ（最優先・少ないクエリで広範カバー）
   - site:release.tdnet.info （TDnet 適時開示、過去24h）
   - site:fsa.go.jp/news 過去30日
   - artemis.bm/news/ と reinsurancene.ws の最新ページを web_fetch
   - 保険毎日新聞、Nikkei 保険業界トップページ

2. 格付け機関の直接照会（Tier 1 必須）
   - site:ambest.com 直近の press release
   - site:spglobal.com/ratings、site:moodys.com、site:fitchratings.com

3. エンティティ別クエリ（スイープで未検出の主要社のみ、3社ずつ束ねる）
   例: "東京海上 OR MS&AD OR SOMPO 社債 OR 格付 2026"

4. 日付フィルタを必ず付与
   - Google系クエリには `after:{since_date}` を付ける
   - 日本語クエリには年月（例: "2026年5月"）を含める

# 重複の扱い
同一イベントが複数媒体に出ている場合は、最も一次情報に近い1件を残し、
他媒体は要約末尾に「他: Reuters, Bloomberg」のように列挙してよい。

# 出力形式（フリーテキスト・箇条書き）
各ヘッドラインを以下のフォーマットで報告してください:

- タイトル: <原語のままのタイトル>
  URL: <絶対URL>
  媒体: <Reuters | Nikkei | TDnet | AM Best | ...>
  公開日時: <ISO 8601 タイムゾーン付き>
  公開日時の確度: <verified | inferred_high | inferred_low>
    verified=ページ上に明示的タイムスタンプ
    inferred_high=日付タグ＋通信社配信時刻で裏付け
    inferred_low=「2 days ago」等の相対表現のみ
  カテゴリ: <rating | capital_markets | m_and_a | regulatory |
            large_loss | earnings | leadership | reinsurance |
            product | other>
  優先度: <T1 | T2>
  対象企業: <主たる関連エンティティ>
  要約: <1〜2文の日本語要約>
  他媒体: <あれば列挙、なければ省略>

# 出力末尾に必ず以下を付記
COVERAGE_NOTES:
  searches_run: <実際の検索回数>
  tier1_aggregators_hit: <TDnet/FSA/Artemis/AM Best のうちアクセスしたもの>
  fallback_used: <true/false ウインドウ拡張の有無>
  gaps: <検索したが該当なしだったTier 1領域があれば記載>

# 除外条件
- ウインドウ外の記事（fallback条件を満たす場合を除く）
- 公開日時の確度が inferred_low の記事
- Tier 3 該当
- 同一イベントの重複（一次情報1件に統合）
最大 {max_headlines} 件。"""


# Stage 2 — structuring prompt. Haiku, no tools. Converts bullets to JSON.
# Receives discovery_text + parsed coverage_notes_text so the structurer can
# echo COVERAGE_NOTES back into the JSON envelope.
STRUCTURING_PROMPT_TEMPLATE = """\
以下のヘッドライン一覧を、指定のJSONスキーマに正確に変換してください。
出力はJSONのみ。前置き、説明文、Markdownフェンス（```）は一切不要。

スキーマ:
{{
  "as_of_jst": "{now_jst}",
  "fallback_used": <bool>,
  "searches_run": <int>,
  "headlines": [
    {{
      "title": "...",
      "url": "https://...",
      "source": "...",
      "published_at": "ISO 8601 タイムゾーン付き",
      "published_confidence": "verified | inferred_high",
      "category": "rating | capital_markets | m_and_a | regulatory | large_loss | earnings | leadership | reinsurance | product | other",
      "tier": "T1 | T2",
      "entity": "...",
      "summary_ja": "...",
      "other_sources": ["..."] | null
    }}
  ],
  "gaps": "..." | null
}}

# ルール
- published_confidence が "inferred_low" の項目は出力に含めない
- published_at が基準時刻 {now_jst} より24h以上前で、fallback_used=false
  の場合はその項目を除外
- titleの先頭40文字が酷似する項目（Jaccard類似度の目安）は最初の1件だけ残し、
  残りはother_sourcesに媒体名のみマージ
- カテゴリ・tier が不明な項目は category="other", tier="T2"

ヘッドライン一覧:
---
{discovery_text}
---

COVERAGE_NOTES:
---
{coverage_notes_text}
---"""


def _strip_json_fences(text: str) -> str:
    """Pull a JSON object out of a Claude response.

    Handles three observed cases:
      1. plain JSON
      2. ```json ... ``` markdown fences
      3. preamble prose + JSON (Claude sometimes ignores 'no prose' instruction)
    """
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


# Matches the COVERAGE_NOTES block at the end of the discovery prose.
_COVERAGE_BLOCK_RE = re.compile(
    r"COVERAGE_NOTES:\s*(?P<body>.+)\Z", re.DOTALL | re.IGNORECASE
)
_COVERAGE_FIELD_RE = re.compile(
    r"^\s*(?P<key>searches_run|tier1_aggregators_hit|fallback_used|gaps)\s*:\s*"
    r"(?P<val>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_coverage_notes(discovery_text: str) -> dict:
    """Extract the COVERAGE_NOTES block from Stage-1 prose.

    Returns a dict with keys: searches_run (int|None), tier1_aggregators_hit
    (int|None — count of comma-separated names), fallback_used (bool|None),
    gaps (str|None), raw (str — the original block text). All fields are
    best-effort; a missing/malformed block returns empty dict values, never
    raises.
    """
    out: dict = {
        "searches_run": None,
        "tier1_aggregators_hit": None,
        "fallback_used": None,
        "gaps": None,
        "raw": "",
    }
    m = _COVERAGE_BLOCK_RE.search(discovery_text)
    if not m:
        return out
    body = m.group("body").strip()
    out["raw"] = body
    for fm in _COVERAGE_FIELD_RE.finditer(body):
        key = fm.group("key").lower()
        val = fm.group("val").strip()
        if key == "searches_run":
            num = re.search(r"\d+", val)
            out["searches_run"] = int(num.group(0)) if num else None
        elif key == "tier1_aggregators_hit":
            # "TDnet, FSA, AM Best" → 3. Split on comma/slash only — multi-word
            # source names ("AM Best") must stay intact.
            names = [n.strip() for n in re.split(r"[,、/]", val)]
            names = [n for n in names if n and n.lower() != "none"]
            out["tier1_aggregators_hit"] = len(names) if names else 0
        elif key == "fallback_used":
            out["fallback_used"] = val.strip().lower().startswith(("true", "yes", "はい", "1"))
        elif key == "gaps":
            out["gaps"] = val
    return out


class ClaudeResearchSource(Source):
    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        watchlists: "Watchlists | None" = None,
        model: str = "claude-opus-4-7",
        cadence_hours: int = 12,
        tier: int = 1,
        max_headlines: int = 30,
        max_search_uses: int = 12,
        store=None,        # for cadence check + api_usage logging
        structuring_model: str = "claude-haiku-4-5",
    ) -> None:
        self.name = name
        self.tier = tier
        self.api_key = api_key
        self.watchlists = watchlists
        self.model = model
        self.cadence_hours = cadence_hours
        self.max_headlines = max_headlines
        self.max_search_uses = max_search_uses
        self.store = store
        self.structuring_model = structuring_model

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

        client = Anthropic(api_key=self.api_key)
        t0 = time.monotonic()
        http_status = None
        article_count = 0
        error: str | None = None
        coverage: dict = {}

        try:
            items, coverage = self._fetch_two_stage(client)
            http_status = 200
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
                    searches_run=coverage.get("searches_run"),
                    tier1_aggregators_hit=coverage.get("tier1_aggregators_hit"),
                    fallback_used=coverage.get("fallback_used"),
                )

        log.info(
            "claude_research.done",
            name=self.name,
            article_count=article_count,
            elapsed_ms=elapsed_ms,
            error=error,
            searches_run=coverage.get("searches_run"),
            fallback_used=coverage.get("fallback_used"),
            gaps=coverage.get("gaps"),
        )
        return items

    # ---- two-stage path -------------------------------------------------

    def _fetch_two_stage(self, client: Anthropic) -> tuple[list[RawItem], dict]:
        """Discovery (Opus + web_search, prose) → Structuring (Haiku, JSON)."""
        now_utc = datetime.now(timezone.utc)
        since = now_utc - timedelta(hours=24)
        now_jst = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M JST")

        if self.watchlists is None:
            p1_names: list[str] = []
            global_names: list[str] = []
        else:
            p1_names = [e.canonical for e in self.watchlists.p1_japan]
            global_names = [e.canonical for e in self.watchlists.p2_global]

        discovery_prompt = DISCOVERY_PROMPT_TEMPLATE.format(
            now_jst=now_jst,
            since_iso=since.isoformat(timespec="minutes"),
            until_iso=now_utc.isoformat(timespec="minutes"),
            since_date=since.strftime("%Y-%m-%d"),
            p1_entities=", ".join(p1_names) if p1_names else "(未指定)",
            global_entities=", ".join(global_names) if global_names else "(未指定)",
            max_headlines=self.max_headlines,
        )

        # Stage 1 — discovery with web_search
        s1_t0 = time.monotonic()
        discovery_resp = client.messages.create(
            model=self.model,
            max_tokens=8192,
            tools=[{**WEB_SEARCH_TOOL, "max_uses": self.max_search_uses}],
            messages=[{"role": "user", "content": discovery_prompt}],
        )
        s1_elapsed = int((time.monotonic() - s1_t0) * 1000)
        self._dump_response(discovery_resp, elapsed_ms=s1_elapsed, suffix="discovery")

        discovery_text = "".join(
            (b.text or "") for b in discovery_resp.content
            if getattr(b, "type", None) == "text"
        )
        if not discovery_text.strip():
            log.warning("claude_research.discovery.empty", name=self.name)
            return [], {}

        coverage = _parse_coverage_notes(discovery_text)

        # Stage 2 — structuring (no tools, cheap model)
        structuring_prompt = STRUCTURING_PROMPT_TEMPLATE.format(
            now_jst=now_jst,
            discovery_text=discovery_text,
            coverage_notes_text=coverage.get("raw") or "(none)",
        )
        s2_t0 = time.monotonic()
        structuring_resp = client.messages.create(
            model=self.structuring_model,
            max_tokens=8192,
            messages=[{"role": "user", "content": structuring_prompt}],
        )
        s2_elapsed = int((time.monotonic() - s2_t0) * 1000)
        self._dump_response(structuring_resp, elapsed_ms=s2_elapsed, suffix="structuring")

        log.info(
            "claude_research.two_stage.done",
            name=self.name,
            discovery_ms=s1_elapsed,
            structuring_ms=s2_elapsed,
            discovery_chars=len(discovery_text),
            searches_run=coverage.get("searches_run"),
            fallback_used=coverage.get("fallback_used"),
        )
        return self._parse_response(structuring_resp), coverage

    # ---- response persistence -------------------------------------------

    def _dump_response(self, resp, *, elapsed_ms: int, suffix: str = "") -> None:
        """Write the raw Anthropic response to logs/claude_research/.

        Best-effort. Any failure is logged and swallowed — must not break
        the fetch path. Uses pydantic `model_dump` when available, falls
        back to attribute introspection.
        """
        try:
            RESPONSE_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "claude"
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            tag = f"__{suffix}" if suffix else ""
            path = RESPONSE_DUMP_DIR / f"{slug}__{ts}{tag}.json"

            try:
                resp_payload = resp.model_dump(mode="json")
            except Exception:
                resp_payload = {
                    "id": getattr(resp, "id", None),
                    "model": getattr(resp, "model", None),
                    "stop_reason": getattr(resp, "stop_reason", None),
                    "content": [self._block_to_dict(b) for b in getattr(resp, "content", [])],
                    "usage": getattr(resp, "usage", None) and self._block_to_dict(resp.usage),
                }

            envelope = {
                "query_name": self.name,
                "model": self.model,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": elapsed_ms,
                "response": resp_payload,
            }
            path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))
            log.info("claude_research.dump.saved", path=str(path), name=self.name)
        except Exception as e:
            log.warning(
                "claude_research.dump.failed",
                name=self.name,
                error=f"{type(e).__name__}: {e}",
            )

    @staticmethod
    def _block_to_dict(block) -> dict:
        if hasattr(block, "model_dump"):
            try:
                return block.model_dump(mode="json")
            except Exception:
                pass
        return {k: str(v) for k, v in vars(block).items()} if hasattr(block, "__dict__") else {"repr": repr(block)}

    def _parse_response(self, resp) -> list[RawItem]:
        # Concatenate ALL text blocks. Joining with "" reconstructs the
        # original stream losslessly — joining with "\n" injects newlines
        # inside string values and breaks the JSON.
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                t = block.text or ""
                if t:
                    text_parts.append(t)
        text = "".join(text_parts)
        text = _strip_json_fences(text)
        if not text:
            log.warning("claude_research.empty_text", name=self.name)
            return []

        try:
            # strict=False allows literal control chars (\n, \t, \r, \0) inside
            # quoted strings — Claude sometimes embeds raw newlines in title/
            # summary fields, which is technically invalid JSON but harmless.
            data = json.loads(text, strict=False)
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
