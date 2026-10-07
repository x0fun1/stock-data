"""Neutral access to normalized OHLCV in a frozen snapshot."""

from __future__ import annotations

import math
from datetime import date
from typing import Any


def market_bars(block: dict[str, Any]) -> list[dict[str, Any]]:
    if not block.get("_data_validation", {}).get("forecast_eligible", False):
        raise ValueError("research requires source-verified daily session/data gates; use an abstention report for unverified data")
    bars = block.get("data", {}).get("bars")
    if not isinstance(bars, list):
        raise ValueError("market data must contain normalized daily bars")
    result: list[dict[str, Any]] = []
    for bar in bars:
        row = {"date": str(bar["date"])[:10]}
        for key in ("open", "high", "low", "close", "volume"):
            value = float(bar[key])
            if not math.isfinite(value):
                raise ValueError(f"non-finite {key} in market bar")
            row[key] = value
        result.append(row)
    return result


def simple_return(start: float, end: float) -> float:
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0:
        raise ValueError("simple return requires finite positive start and finite end")
    return end / start - 1.0


def forward_returns(closes: list[float], horizon: int) -> list[float | None]:
    """Map session t to close[t+h]/close[t]-1; the final horizon rows are unlabeled."""
    result: list[float | None] = [None] * len(closes)
    for index in range(max(0, len(closes) - horizon)):
        result[index] = simple_return(closes[index], closes[index + horizon])
    return result


def non_overlapping(indices: list[int], horizon: int) -> list[int]:
    kept: list[int] = []
    last_end = -1
    for index in sorted(indices):
        if index > last_end:
            kept.append(index)
            last_end = index + horizon
    return kept
