"""Digest curator: one Claude call that aggregates + prioritizes the day's
P1+P2 stories into a ranked, deduplicated digest.

Replaces the old per-row summarize loop in `digest.py`. Inputs are StoryRow
objects from the last 12h; output is `DigestEntry` objects compatible with
the existing mailer.

Falls back to per-row summarize() if the batched Claude call fails to parse
or returns empty entries — never silent-empties a digest run.
"""
from __future__ import annotations

import json
import re

import structlog
from anthropic import Anthropic

from .mailer import DigestEntry
from .store import StoryRow
from .summarizer import Article, Summarizer

log = structlog.get_logger()

DEFAULT_CURATOR_MODEL = "claude-haiku-4-5"

CURATOR_PROMPT_TEMPLATE = """\
あなたは保険・再保険セクター担当のシニア・エディターです。
以下のヘッドライン群（過去12時間で収集）を読み、機関投資家デスク向けの
ダイジェストに編集してください。

# タスク
1. 同一イベントの重複（複数媒体の同記事）は1件に統合し、媒体名を集約
2. Tier 1 イベント（格付け / 資本市場取引 / M&A / 規制 / 大型損害）を上位に配置
3. 各イベントに日本語ヘッドライン1行（30文字以内）と要約箇条書き（3〜5項目、各40文字以内）を作成
4. 重要度の低いものは除外してよい（最大{max_entries}件）
5. priority は元の P1 / P2 を尊重する

# 入力ヘッドライン
{rows_text}

# 出力（JSONのみ、前置き・コードフェンス無し）
{{
  "entries": [
    {{
      "priority": "P1 | P2",
      "headline_ja": "...",
      "original_title": "...",
      "source": "...",
      "url": "https://...",
      "summary_bullets": ["...", "...", "..."]
    }}
  ]
}}
"""


def curate_digest(
    rows: list[StoryRow],
    summarizer: Summarizer,
    *,
    max_entries: int = 15,
    model: str = DEFAULT_CURATOR_MODEL,
) -> list[DigestEntry]:
    """One Claude call that aggregates, prioritizes, and summarizes the
    digest in Japanese. Returns DigestEntry objects compatible with the
    existing mailer.

    On failure (empty parse / invalid JSON / API error) falls back to
    per-row Summarizer.summarize() so the digest never silent-empties.
    """
    if not rows:
        return []

    rows_text = "\n".join(
        f"[{r.priority}] {r.title} | source={r.source} | url={r.url} | published_at={r.published_at or ''}"
        for r in rows
    )
    prompt = CURATOR_PROMPT_TEMPLATE.format(
        rows_text=rows_text,
        max_entries=max_entries,
    )

    client = summarizer.client  # reuse the existing Anthropic client
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning("curator.api.failed", error=f"{type(e).__name__}: {e}")
        return _fallback_per_row(rows, summarizer)

    text = "".join(
        (b.text or "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    parsed = _parse_curator_json(text)
    if parsed is None:
        log.warning("curator.parse.failed", preview=text[:200])
        return _fallback_per_row(rows, summarizer)

    entries: list[DigestEntry] = []
    for item in parsed.get("entries", []):
        url = (item.get("url") or "").strip()
        title = (item.get("original_title") or "").strip()
        if not url or not title:
            continue
        bullets_value = item.get("summary_bullets")
        if isinstance(bullets_value, list):
            bullets = "\n".join(f"- {b}" for b in bullets_value if b)
        else:
            bullets = str(bullets_value or "")
        entries.append(
            DigestEntry(
                priority=(item.get("priority") or "P2").strip(),
                headline_ja=(item.get("headline_ja") or title).strip(),
                original_title=title,
                source=(item.get("source") or "").strip(),
                url=url,
                summary_bullets=bullets,
            )
        )

    if not entries:
        log.warning("curator.empty_entries", row_count=len(rows))
        return _fallback_per_row(rows, summarizer)

    log.info(
        "curator.done",
        input_rows=len(rows),
        output_entries=len(entries),
        model=model,
    )
    return entries


def _parse_curator_json(text: str) -> dict | None:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError:
        return None


def _fallback_per_row(
    rows: list[StoryRow], summarizer: Summarizer
) -> list[DigestEntry]:
    """Per-row Summarizer call — used when batched curation fails."""
    entries: list[DigestEntry] = []
    for r in rows:
        article = Article(
            title=r.title,
            source=r.source,
            url=r.url,
            raw_text=r.title,
            published_at=None,
            entity=None,
        )
        try:
            summary = summarizer.summarize(article)
        except Exception as e:
            log.warning("curator.fallback.summarize_failed", url=r.url, error=str(e))
            continue
        entries.append(
            DigestEntry(
                priority=r.priority,
                headline_ja=summary.headline,
                original_title=r.title,
                source=r.source,
                url=r.url,
                summary_bullets=summary.bullets,
            )
        )
    log.info("curator.fallback.done", input_rows=len(rows), output_entries=len(entries))
    return entries
