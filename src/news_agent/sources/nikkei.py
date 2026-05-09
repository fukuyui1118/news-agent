from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from .base import RawItem, Source

log = structlog.get_logger()


class NikkeiArticle(BaseModel):
    title: str
    url: str
    published_at: str | None = None
    summary: str | None = None


class NikkeiResult(BaseModel):
    articles: list[NikkeiArticle] = Field(default_factory=list)
    error: str | None = None


_TASK_TEMPLATE = """\
あなたは日本経済新聞 (nikkei.com) の保険・金融セクションから記事リストを抽出するエージェントです。

手順:
1. 現在のセッションが nikkei.com にログイン済みか確認する。未ログインの場合は次の認証情報でログインする:
   - メール: sensitive_data の "nikkei_user"
   - パスワード: sensitive_data の "nikkei_pass"
   CAPTCHA や 2FA に遭遇した場合は中断し、error="auth_failed" として返す。
2. {url} に遷移する。
3. 記事リストの上位10件について、構造化出力スキーマ NikkeiResult.articles に以下を格納する:
   - title (日本語、必須)
   - url (絶対URL、必須)
   - published_at (ISO8601 推定可、任意)
   - summary (リード文があれば、任意)
4. 記事本文ページへの遷移は不要。一覧ページのみで完結させること。
5. 想定外のエラーが発生した場合は error フィールドに簡潔な理由を入れる。
"""


class NikkeiSource(Source):
    def __init__(
        self,
        *,
        name: str,
        url: str,
        tier: int = 2,
        nikkei_user: str = "",
        nikkei_pass: str = "",
        browser_use_model: str = "claude-sonnet-4-5",
        anthropic_api_key: str = "",
        storage_state_path: Path | str = Path("storage_state.json"),
        max_steps: int = 30,
        headless: bool = True,
    ) -> None:
        self.name = name
        self.url = url
        self.tier = tier
        self.nikkei_user = nikkei_user
        self.nikkei_pass = nikkei_pass
        self.browser_use_model = browser_use_model
        self.anthropic_api_key = anthropic_api_key
        self.storage_state_path = Path(storage_state_path)
        self.max_steps = max_steps
        self.headless = headless

    def fetch(self) -> list[RawItem]:
        if not self.anthropic_api_key:
            log.error("nikkei.skipped", reason="ANTHROPIC_API_KEY not set")
            return []
        if not self.nikkei_user or not self.nikkei_pass:
            log.error("nikkei.skipped", reason="NIKKEI_USER or NIKKEI_PASS not set in .env")
            return []
        try:
            return asyncio.run(self._fetch_async())
        except Exception as e:
            log.error("nikkei.fetch.failed", error=str(e), error_type=type(e).__name__)
            return []

    async def _fetch_async(self) -> list[RawItem]:
        from browser_use import Agent
        from browser_use.browser import BrowserProfile, BrowserSession
        from browser_use.llm.anthropic.chat import ChatAnthropic

        storage = (
            str(self.storage_state_path) if self.storage_state_path.exists() else None
        )
        profile = BrowserProfile(storage_state=storage, headless=self.headless)
        session = BrowserSession(browser_profile=profile)

        llm = ChatAnthropic(
            model=self.browser_use_model,
            api_key=self.anthropic_api_key,
            max_tokens=4096,
        )

        agent = Agent(
            task=_TASK_TEMPLATE.format(url=self.url),
            llm=llm,
            browser_session=session,
            output_model_schema=NikkeiResult,
            sensitive_data={
                "nikkei_user": self.nikkei_user,
                "nikkei_pass": self.nikkei_pass,
            },
            max_failures=3,
            step_timeout=120,
            calculate_cost=False,
            use_judge=False,
            use_thinking=False,
        )

        history = None
        try:
            history = await agent.run(max_steps=self.max_steps)
        finally:
            await self._persist_storage_state(session)
            try:
                await session.kill()
            except Exception:
                pass

        if history is None:
            return []

        result: NikkeiResult | None = history.structured_output
        if result is None:
            log.error(
                "nikkei.no_structured_output",
                steps=history.number_of_steps(),
                errors=history.errors(),
            )
            return []
        if result.error:
            log.error("nikkei.agent.error", error=result.error)
            return []

        items: list[RawItem] = []
        for art in result.articles:
            items.append(
                RawItem(
                    url=art.url,
                    title=art.title,
                    published_at=_parse_iso(art.published_at),
                    source=self.name,
                    raw_text=art.summary or art.title,
                    source_tier=self.tier,
                )
            )
        log.info("nikkei.fetched", count=len(items))
        return items

    async def _persist_storage_state(self, session) -> None:
        try:
            state = await session.export_storage_state()
            if state:
                import json

                self.storage_state_path.write_text(
                    json.dumps(state), encoding="utf-8"
                )
        except Exception as e:
            log.warning(
                "nikkei.storage_state.save_failed",
                error=str(e),
                error_type=type(e).__name__,
            )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
