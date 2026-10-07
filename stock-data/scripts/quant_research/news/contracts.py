"""Restricted input contract for News/Sentiment analysts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NewsInput:
    """Inputs visible to the News layer; deliberately contains no Quant result."""

    ticker: str
    market: str
    horizon: str
    asof_timestamp: str
    snapshot_id: str
    news_status: str
    source_sentiment: dict[str, Any] | None
    articles: tuple[dict[str, Any], ...]
    bars: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)
