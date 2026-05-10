"""AI-based email composer: one Claude Opus call drafts the digest's
Japanese headlines + summaries + structured email body.

Replaces `curator.py`. Same input/output contract for the mailer:
takes `StoryRow` objects from `digest_eligible_stories`, returns
`DigestEntry` objects ready for `mailer.send_digest`.

Falls back to per-row `Summarizer.summarize` (Haiku) on parse/API
failure. Hard-caps at `max_entries`.
"""
from __future__ import annotations

import json
import re
import time

import structlog

from .mailer import DigestEntry
from .store import StoryRow
from .summarizer import Article, Summarizer

log = structlog.get_logger()

DEFAULT_EMAIL_MODEL = "claude-opus-4-7"

EMAIL_PROMPT_TEMPLATE = """\
あなたは保険・再保険セクター担当のシニア・エディターです。
過去12時間で収集された P1（日本/規制/金融）と P2（グローバル保険）の
ヘッドラインを、東京の機関投資家デスク向けダイジェストに編集してください。

# タスク
1. 同一イベントの重複（複数媒体の同記事）は1件に統合し、媒体名を集約
2. P1 を上位、P2 を下位に並べる
3. 各イベントに30文字以内の日本語ヘッドラインと、3〜5項目の箇条書き要約を作成
4. 重要度の低い P2 は除外可（最大 {max_entries} 件まで）
5. 宣伝的・誇張的な表現を避け、財務・戦略事実（金額、当事者、日付）を優先

# 入力ヘッドライン（過去12時間、P1優先）
{rows_text}

# 出力（JSONのみ。前置き・コードフェンス無し）
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
}}"""


def compose_email(
    rows: list[StoryRow],
    summarizer: Summarizer,
    *,
    max_entries: int = 15,
    model: str = DEFAULT_EMAIL_MODEL,
) -> list[DigestEntry]:
    """Single Opus call → ranked, deduplicated DigestEntry list.

    Falls back to per-row Haiku summarize on parse/API failure.
    Hard-caps output at max_entries. Reuses `summarizer.client` so callers
    don't need to pass api_key separately.
    """
    if not rows:
        return []

    candidates = rows[: max_entries * 2]

    rows_text = "\n".join(
        f"[{r.priority}] {r.title} | source={r.source} | url={r.url} | published_at={r.published_at or ''}"
        for r in candidates
    )
    prompt = EMAIL_PROMPT_TEMPLATE.format(
        rows_text=rows_text,
        max_entries=max_entries,
    )

    client = summarizer.client  # reuse existing Anthropic client
    t0 = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning("ai_email.api.failed", error=f"{type(e).__name__}: {e}")
        return _fallback_per_row(candidates, summarizer, max_entries=max_entries)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    text = "".join(
        (b.text or "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    parsed = _parse_email_json(text)
    if parsed is None:
        log.warning("ai_email.parse.failed", preview=text[:200])
        return _fallback_per_row(candidates, summarizer, max_entries=max_entries)

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
        log.warning("ai_email.empty_entries", row_count=len(rows))
        return _fallback_per_row(candidates, summarizer, max_entries=max_entries)

    entries = entries[:max_entries]
    log.info(
        "ai_email.done",
        input_rows=len(rows),
        candidate_rows=len(candidates),
        output_entries=len(entries),
        elapsed_ms=elapsed_ms,
        model=model,
    )
    return entries


def _parse_email_json(text: str) -> dict | None:
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
    return obj if isinstance(obj, dict) else None


def _fallback_per_row(
    rows: list[StoryRow], summarizer: Summarizer, *, max_entries: int = 15
) -> list[DigestEntry]:
    """Per-row Summarizer call when batched email-compose fails. Capped."""
    rows = rows[:max_entries]
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
            log.warning("ai_email.fallback.summarize_failed", url=r.url, error=str(e))
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
    log.info("ai_email.fallback.done", input_rows=len(rows), output_entries=len(entries))
    return entries
