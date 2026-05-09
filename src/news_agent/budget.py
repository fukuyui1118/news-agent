"""API call budget guard for paid services like NewsAPI.ai.

Reads usage history from the `api_usage` SQLite table; gates new calls when
caps are hit. Logs every call attempt (success or failure) for the --stats
readout and weekly pruning.

Caps for NewsAPI.ai (default):
  - per-cycle hard cap (kills emergency loops): 8 calls
  - rolling 30-day cap (the contractual budget):  4,800 calls
  - daily soft warning (heads-up signal):         200 calls/day in JST
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

from .store import Store

log = structlog.get_logger()


class BudgetExceeded(RuntimeError):
    """Raised when a budget cap would be violated. Caller catches and skips."""


@dataclass
class BudgetConfig:
    provider: str = "newsapi.ai"
    monthly_cap: int = 4800
    per_cycle_hard_cap: int = 8
    daily_soft_warning: int = 200
    timezone_name: str = "Asia/Tokyo"


@dataclass
class BudgetGuard:
    config: BudgetConfig
    store: Store
    cycle_calls: int = field(default=0)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset_cycle(self) -> None:
        with self._lock:
            self.cycle_calls = 0

    def _preflight(self, query_name: str | None) -> None:
        # Per-cycle hard cap.
        if self.cycle_calls >= self.config.per_cycle_hard_cap:
            raise BudgetExceeded(
                f"per_cycle_hard_cap reached ({self.config.per_cycle_hard_cap})"
            )
        # 30-day monthly cap. (Lock held — store.api_call_count reads conn.)
        used = self.store.api_call_count(provider=self.config.provider, hours=24 * 30)
        if used >= self.config.monthly_cap:
            raise BudgetExceeded(
                f"monthly_cap reached ({used}/{self.config.monthly_cap})"
            )
        today = self.store.api_call_count_today(
            provider=self.config.provider, timezone_name=self.config.timezone_name
        )
        if today >= self.config.daily_soft_warning:
            log.warning(
                "newsapi.daily_soft_warning",
                today=today,
                threshold=self.config.daily_soft_warning,
                query_name=query_name,
            )

    @contextmanager
    def guard(self, *, endpoint: str, query_name: str | None = None):
        """Use:  with budget.guard(endpoint=..., query_name=...) as record:
                    record(article_count=N, http_status=200)

        Pre-flights against caps. The yielded `record` callable inserts an
        `api_usage` row when the call completes. Always logs (success or fail).
        """
        with self._lock:
            try:
                self._preflight(query_name)
            except BudgetExceeded as e:
                self.store.record_api_call(
                    provider=self.config.provider,
                    endpoint=endpoint,
                    query_name=query_name,
                    error=f"budget_exceeded: {e}",
                )
                raise

        t0 = time.monotonic()
        outcome: dict = {}

        def record(
            *,
            article_count: int | None = None,
            http_status: int | None = None,
            error: str | None = None,
        ) -> None:
            outcome["article_count"] = article_count
            outcome["http_status"] = http_status
            outcome["error"] = error

        try:
            yield record
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            with self._lock:
                self.cycle_calls += 1
                self.store.record_api_call(
                    provider=self.config.provider,
                    endpoint=endpoint,
                    query_name=query_name,
                    article_count=outcome.get("article_count"),
                    elapsed_ms=elapsed_ms,
                    http_status=outcome.get("http_status"),
                    error=outcome.get("error"),
                )

    def usage_summary(self) -> dict:
        used_30d = self.store.api_call_count(
            provider=self.config.provider, hours=24 * 30
        )
        today = self.store.api_call_count_today(
            provider=self.config.provider, timezone_name=self.config.timezone_name
        )
        # Rough month-end projection: extrapolate today's rate × 30.
        projected = (today * 30) if today > 0 else used_30d
        return {
            "used_30d": used_30d,
            "monthly_cap": self.config.monthly_cap,
            "today": today,
            "daily_soft_warning": self.config.daily_soft_warning,
            "projected_month_end": projected,
            "cycle_calls": self.cycle_calls,
            "per_cycle_hard_cap": self.config.per_cycle_hard_cap,
        }
