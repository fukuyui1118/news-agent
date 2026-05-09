from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawItem:
    url: str
    title: str
    published_at: datetime | None
    source: str
    raw_text: str
    source_tier: int = 3


class Source(ABC):
    name: str
    tier: int

    @abstractmethod
    def fetch(self) -> list[RawItem]:
        ...
