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
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from anthropic import Anthropic

from .base import RawItem, Source

# Where raw Claude responses are persisted (one file per successful call).
# Created lazily on first dump; logs/ is already gitignored at repo root.
RESPONSE_DUMP_DIR = Path("logs/claude_research")

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


# Two-stage prompts. Stage 1 (discovery) uses web_search and asks for a loose
# bullet-list — relieving Claude of any schema pressure during search. Stage 2
# (structuring) takes the bullets and converts them to strict JSON via Haiku
# without tools (cheap, ~$0.005/call).
DISCOVERY_PROMPT_TEMPLATE = """\
あなたは保険・再保険業界を専門とするシニア金融ニュース調査員です。

`web_search` ツールを使用して、{since_iso} から {until_iso} （現在）の間に
公開された保険セクターのニュース・ヘッドラインを収集してください。

カバー範囲:
  - 日本の保険会社（東京海上、損保ジャパン、MS&AD、第一生命、日本生命、
    明治安田生命、住友生命、かんぽ生命、トーア再保険、T&D、ソニー生命、
    アフラック生命 など）
  - グローバル大手保険会社（Allianz、AXA、Zurich、Munich Re、Swiss Re、
    AIG、Chubb、MetLife、Manulife、Lloyd's、Berkshire再保険 など）
  - 資本市場イベント: IPO、M&A、増資、自社株買い
  - 規制動向: 金融庁、EIOPA、NAIC、格付け機関の動き
  - 格付け変更（上方/下方修正、Outlook変更）
  - 戦略動向: 市場参入/撤退、提携、ジョイントベンチャー

8〜15個の異なる検索クエリで、日本＋グローバル＋業界トレンドを幅広くカバー
してください。報道機関、IRページ、規制当局のサイトを優先します。

各ヘッドラインを以下の形式で箇条書き報告してください（JSONではなくフリーテキスト）:

- タイトル: <原語のままのタイトル>
  URL: <絶対URL>
  媒体: <Reuters | Nikkei | Bloomberg | ...>
  公開日時: <できる限り正確な ISO 8601 タイムゾーン付き>
  要約: <1〜2文の日本語要約>

最大{max_headlines}件まで。同一イベントの重複は1件に統合。
**指定の時間範囲（過去24時間）より古い記事は除外してください。**"""


STRUCTURING_PROMPT_TEMPLATE = """\
以下のヘッドライン一覧を、指定のJSONスキーマに正確に変換してください。
出力はJSONのみ。前置き、説明文、Markdownフェンス（```）は一切不要。

スキーマ:
{{
  "headlines": [
    {{
      "title": "...",
      "url": "https://...",
      "source": "...",
      "published_at": "ISO 8601 タイムゾーン付き",
      "summary_ja": "..."
    }}
  ]
}}

ヘッドライン一覧:
---
{discovery_text}
---"""


# Phase 8.2: bucket-XML strategy (probe-only). System+user split, JST datetime
# injected, four buckets (JP issuers / JP regulation / global / rating actions),
# bilingual search enumerated per issuer, XML output. See plan section 20.
SYSTEM_PROMPT_BUCKET_XML = """\
You are a specialist news analyst covering the Japanese and global insurance sector for an institutional financial-markets desk in Tokyo. Your job is to retrieve, verify, deduplicate, translate, and categorize insurance-sector headlines from the last 24 hours, and to output them in Japanese.

# Hard constraints
1. ONLY include headlines whose publication timestamp is strictly within the 24-hour window ending at {now_jst} JST. If you cannot verify a publish datetime, exclude the item — do NOT estimate or infer.
2. Output language: Japanese (日本語) only. English-source headlines must be faithfully translated; preserve proper nouns (company names, product names, ratings symbols like "A+", "Aa3") in their original form.
3. Use the web_search tool aggressively. Run multiple queries in BOTH Japanese and English. Do not stop after one search — scale to 8–15 searches as needed for full coverage of the buckets below.
4. Deduplicate: if the same story appears across multiple outlets, keep one entry and list the others under <other_sources>.
5. Never fabricate. If a bucket has zero qualifying items, output <item>該当なし</item> for that bucket.
6. Do not summarize beyond a headline + one-line context. This is a headline scan, not a research note.

# Coverage scope

## Bucket A — Major 20 Japanese insurers (company-specific news)
Search each of the following by name in BOTH languages:
- 東京海上ホールディングス / Tokio Marine Holdings (incl. 東京海上日動)
- MS&ADインシュアランスグループHD / MS&AD (incl. 三井住友海上, あいおいニッセイ同和損保)
- SOMPOホールディングス / Sompo Holdings (incl. 損害保険ジャパン, Sompo International)
- 日本生命 / Nippon Life
- 第一生命ホールディングス / Dai-ichi Life Holdings
- 明治安田生命 / Meiji Yasuda Life
- 住友生命 / Sumitomo Life
- かんぽ生命 / Japan Post Insurance
- T&Dホールディングス / T&D Holdings (incl. 大同生命, 太陽生命)
- ソニーフィナンシャルグループ / Sony Financial (Sony Life)
- プルデンシャル生命 / Prudential of Japan
- アフラック生命 / Aflac Life Japan
- メットライフ生命 / MetLife Japan
- アクサ生命 / AXA Life Japan
- マニュライフ生命 / Manulife Japan
- ジブラルタ生命 / Gibraltar Life
- 富国生命 / Fukoku Mutual Life
- 朝日生命 / Asahi Mutual Life
- ライフネット生命 / Lifenet Insurance
- オリックス生命 / ORIX Life Insurance

Relevant news types: earnings, capital actions (debt/equity issuance, buybacks, hybrids), M&A, reinsurance deals, strategic shareholding sales, governance/misconduct, leadership changes, product launches, ratings actions.

## Bucket B — Japanese regulation and industry-body news
Sources to prioritize: 金融庁 (FSA), 生命保険協会 (LIAJ), 日本損害保険協会 (GIAJ), 日本銀行, 財務省, Nikkei, Bloomberg Japan, Reuters Japan, 保険毎日新聞, インシュアランス, R&I, JCR.
Topics: 経済価値ベースのソルベンシー規制 (ICS/ESR), 保険業法改正, IFRS17, cross-shareholding unwinds, FSA monitoring/inspection actions, tax/disclosure rule changes.

## Bucket C — Global insurance sector news (translated to Japanese)
Sources: Reinsurance News, Artemis.bm, Insurance Insider, Insurance Journal, S&P Global, Bloomberg, Reuters, FT, Financial News, AM Best, Fitch, Moody's, S&P Global Ratings press.
Topics: global M&A, capital-markets activity (cat bonds, sidecars, hybrid issuance, IPOs, sub debt), reinsurance pricing/renewals, rating actions on global insurers/reinsurers, large-loss events, regulatory changes (NAIC, PRA, EIOPA, IAIS, BMA).

## Bucket D — Rating actions (dedicated)
Any rating change, outlook revision, or watch placement on insurers/reinsurers globally — Japanese or non-Japanese — by AM Best, S&P, Moody's, Fitch, R&I, JCR. Surface every action separately even if also covered in A/B/C.

# Search strategy
- For each Japanese issuer, run at least one Japanese-language query (e.g. "東京海上 ニュース") and one English-language query (e.g. "Tokio Marine news").
- For ratings, query each agency's recent action page in addition to general searches.
- For regulation, query FSA and trade-body sites directly.
- Always check the source's timestamp before including. Reject if older than 24h or if timestamp is missing/ambiguous.

# Output format
Return ONLY the XML block below. No preamble, no postamble.

<news_digest as_of="{now_jst}">
  <bucket name="A_japanese_insurers">
    <item>
      <headline_ja>...</headline_ja>
      <original_headline lang="ja|en">...</original_headline>
      <company>...</company>
      <published_jst>YYYY-MM-DD HH:MM JST</published_jst>
      <source>...</source>
      <url>...</url>
      <one_line_context_ja>...</one_line_context_ja>
      <other_sources>...</other_sources>
    </item>
    <!-- repeat -->
  </bucket>
  <bucket name="B_japan_regulation">...</bucket>
  <bucket name="C_global_sector">...</bucket>
  <bucket name="D_rating_actions">...</bucket>
  <coverage_notes>
    <searches_run>N</searches_run>
    <gaps>記載すべきギャップがあればここに。なければ「特になし」。</gaps>
  </coverage_notes>
</news_digest>
"""


USER_PROMPT_BUCKET_XML = """\
基準日時(JST): {now_jst}

過去24時間以内に公開された保険セクターのヘッドライン一覧を、上記システムプロンプトの仕様に従って収集・出力してください。日本語ソースと英語ソースの両方を網羅し、英語ソースは日本語訳を付けてください。各記事の公開時刻が24時間以内であることを必ず検証してください。
"""


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
        prompt_override: str | None = None,
        two_stage: bool = True,
        structuring_model: str = "claude-haiku-4-5",
        prompt_strategy: str = "two_stage",  # "single" | "two_stage" | "bucket_xml"
    ) -> None:
        self.name = name
        self.tier = tier
        self.api_key = api_key
        self.model = model
        self.cadence_hours = cadence_hours
        self.max_headlines = max_headlines
        self.max_search_uses = max_search_uses
        self.store = store
        self.prompt_override = prompt_override
        self.two_stage = two_stage
        self.structuring_model = structuring_model
        self.prompt_strategy = prompt_strategy

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

        try:
            if self.prompt_strategy == "bucket_xml" and self.prompt_override is None:
                items = self._fetch_bucket_xml(client)
            elif self.two_stage and self.prompt_override is None:
                items = self._fetch_two_stage(client)
            else:
                template = self.prompt_override or PROMPT_TEMPLATE
                prompt = template.format(max_headlines=self.max_headlines)
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=8192,
                    tools=[{**WEB_SEARCH_TOOL, "max_uses": self.max_search_uses}],
                    messages=[{"role": "user", "content": prompt}],
                )
                self._dump_response(resp, elapsed_ms=int((time.monotonic() - t0) * 1000))
                items = self._parse_response(resp)
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
                )

        log.info(
            "claude_research.done",
            name=self.name,
            article_count=article_count,
            elapsed_ms=elapsed_ms,
            error=error,
        )
        return items

    # ---- two-stage path -------------------------------------------------

    def _fetch_two_stage(self, client: Anthropic) -> list[RawItem]:
        """Discovery (Opus + web_search, prose) → Structuring (Haiku, JSON)."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=24)
        discovery_prompt = DISCOVERY_PROMPT_TEMPLATE.format(
            since_iso=since.isoformat(timespec="minutes"),
            until_iso=now.isoformat(timespec="minutes"),
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
            return []

        # Stage 2 — structuring (no tools, cheap model)
        structuring_prompt = STRUCTURING_PROMPT_TEMPLATE.format(
            discovery_text=discovery_text
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
        )
        return self._parse_response(structuring_resp)

    # ---- bucket-XML strategy --------------------------------------------

    def _fetch_bucket_xml(self, client: Anthropic) -> list[RawItem]:
        """System+user split, JST datetime injected, XML output, 4 buckets."""
        now_jst = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
        system = SYSTEM_PROMPT_BUCKET_XML.format(now_jst=now_jst)
        user_msg = USER_PROMPT_BUCKET_XML.format(now_jst=now_jst)

        t0 = time.monotonic()
        resp = client.messages.create(
            model=self.model,
            max_tokens=12000,
            system=system,
            tools=[{**WEB_SEARCH_TOOL, "max_uses": self.max_search_uses}],
            messages=[{"role": "user", "content": user_msg}],
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        self._dump_response(resp, elapsed_ms=elapsed, suffix="bucket_xml")

        items, bucket_counts, coverage = self._parse_xml_response(resp)
        log.info(
            "claude_research.bucket_xml.done",
            name=self.name,
            elapsed_ms=elapsed,
            now_jst=now_jst,
            bucket_a=bucket_counts.get("A", 0),
            bucket_b=bucket_counts.get("B", 0),
            bucket_c=bucket_counts.get("C", 0),
            bucket_d=bucket_counts.get("D", 0),
            searches_run=coverage.get("searches_run"),
            gaps=coverage.get("gaps"),
        )
        return items

    def _parse_xml_response(self, resp) -> tuple[list[RawItem], dict, dict]:
        """Extract <item> rows from <news_digest>. Returns (items, counts, coverage_notes).

        Tolerant — single malformed <item> is skipped, not fatal. Root-level
        XML failure logs and returns empty lists.
        """
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                t = block.text or ""
                if t:
                    text_parts.append(t)
        text = "".join(text_parts).strip()

        # Strip markdown fences if present
        m = re.match(r"^```(?:xml)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
        if m:
            text = m.group(1).strip()

        # Locate <news_digest> ... </news_digest>
        start = text.find("<news_digest")
        end = text.rfind("</news_digest>")
        if start < 0 or end < 0:
            log.warning("claude_research.xml_parse_failed", name=self.name, reason="no_root", preview=text[:300])
            return [], {}, {}
        xml_blob = text[start : end + len("</news_digest>")]

        # Claude routinely writes unescaped `&` in text (e.g. "MS&AD", "P&C")
        # which trips ElementTree. Escape any `&` that isn't already part of
        # a named or numeric XML entity reference.
        xml_blob = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml_blob)

        try:
            root = ET.fromstring(xml_blob)
        except ET.ParseError as e:
            log.error(
                "claude_research.xml_parse_failed",
                name=self.name,
                error=str(e),
                preview=xml_blob[:300],
            )
            return [], {}, {}

        items: list[RawItem] = []
        bucket_counts: dict[str, int] = {}
        for bucket in root.findall("bucket"):
            bucket_name = bucket.get("name", "")
            bucket_letter = bucket_name[:1] if bucket_name else "?"
            for item_el in bucket.findall("item"):
                # Skip the empty-bucket sentinel
                raw = (item_el.text or "").strip()
                if raw == "該当なし" and len(item_el) == 0:
                    continue
                try:
                    item = self._xml_item_to_rawitem(item_el, bucket_letter)
                except Exception as ex:
                    log.warning(
                        "claude_research.xml_item_skipped",
                        name=self.name,
                        error=f"{type(ex).__name__}: {ex}",
                    )
                    continue
                if item is None:
                    continue
                items.append(item)
                bucket_counts[bucket_letter] = bucket_counts.get(bucket_letter, 0) + 1

        coverage = {}
        cov_el = root.find("coverage_notes")
        if cov_el is not None:
            sr = cov_el.find("searches_run")
            gp = cov_el.find("gaps")
            coverage["searches_run"] = (sr.text or "").strip() if sr is not None else None
            coverage["gaps"] = (gp.text or "").strip() if gp is not None else None

        return items, bucket_counts, coverage

    def _xml_item_to_rawitem(self, item_el, bucket_letter: str) -> RawItem | None:
        def _t(tag: str) -> str:
            el = item_el.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        url = _t("url")
        title = _t("headline_ja") or _t("original_headline")
        if not url or not title:
            return None
        published_at = _parse_jst_published(_t("published_jst"))
        source = _t("source") or self.name
        context = _t("one_line_context_ja")
        raw_text = f"[{bucket_letter}] {context}".strip() if context else f"[{bucket_letter}]"
        return RawItem(
            url=url,
            title=title,
            published_at=published_at,
            source=source,
            raw_text=raw_text,
            source_tier=self.tier,
        )

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
                # anthropic SDK message objects expose pydantic model_dump
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
        # Concatenate ALL text blocks. Claude with web_search returns content
        # as a sequence of (server_tool_use, tool_result, text, server_tool_use,
        # tool_result, text, …, final text). When Claude streams long JSON it
        # may split a single string value across multiple text blocks (e.g.
        # `"summary_ja": ` in one block, the JP value in the next). Joining
        # with "" reconstructs the original stream losslessly — joining with
        # "\n" injects newlines inside string values and breaks the JSON.
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


_JST_DATETIME_RE = re.compile(
    r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"
    r"(?:[T\s]+(?P<hh>\d{1,2}):(?P<mm>\d{2}))?"
)


def _parse_jst_published(value: str | None):
    """Parse the bucket-XML <published_jst> field as JST → UTC datetime.

    Handles all observed Claude variants:
      "2026-05-10 14:30 JST"
      "2026-05-10 14:30:00+09:00"
      "2026-05-09 (JST、約1日前)"        — annotated date, no time
      "2026-05-09T14:30 JST"
    """
    if not value:
        return None
    m = _JST_DATETIME_RE.search(value)
    if not m:
        return None
    try:
        dt = datetime(
            year=int(m.group("y")),
            month=int(m.group("m")),
            day=int(m.group("d")),
            hour=int(m.group("hh") or 0),
            minute=int(m.group("mm") or 0),
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc)
