from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from anthropic import Anthropic

DEFAULT_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """あなたは保険・再保険業界のニュースを日本語で要約するビジネスアナリストです。

出力形式:
1行目: 日本語の簡潔な見出し（30文字以内）
2行目: 空行
3〜7行目: 「- 」で始まる箇条書き（3〜5項目、各40文字以内）

ルール:
- 財務・戦略事実（金額、当事者名、日付）を優先する
- 宣伝的・誇張的な表現は使わない
- 情報が不足している場合は、その旨を1項目で簡潔に記載する
- 出力は必ず日本語で行う
- 上記のフォーマット以外の文字（前置き、注釈、英訳など）は出力しない"""


@dataclass
class Summary:
    headline: str
    bullets: str

    def as_full_text(self) -> str:
        if self.bullets:
            return f"{self.headline}\n\n{self.bullets}"
        return self.headline


@dataclass
class Article:
    title: str
    source: str
    url: str
    raw_text: str
    published_at: datetime | None
    entity: str | None


class Summarizer:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def summarize(self, article: Article) -> Summary:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._build_user_message(article)}],
        )
        return self._parse(response.content[0].text.strip())

    @staticmethod
    def _build_user_message(article: Article) -> str:
        published = article.published_at.isoformat() if article.published_at else "(unknown)"
        entity_line = f"監視対象企業: {article.entity}\n" if article.entity else ""
        return (
            f"記事タイトル: {article.title}\n"
            f"ソース: {article.source}\n"
            f"公開日: {published}\n"
            f"URL: {article.url}\n"
            f"{entity_line}"
            f"\n"
            f"記事本文:\n{article.raw_text}"
        )

    @staticmethod
    def _parse(text: str) -> Summary:
        text = text.strip()
        if not text:
            return Summary(headline="(要約なし)", bullets="")
        lines = [line.rstrip() for line in text.split("\n")]
        headline = lines[0].strip() or "(要約なし)"
        bullet_start = 1
        while bullet_start < len(lines) and not lines[bullet_start].lstrip().startswith("-"):
            bullet_start += 1
        bullets = "\n".join(line for line in lines[bullet_start:] if line.strip())
        return Summary(headline=headline, bullets=bullets)
