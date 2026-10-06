"""Validate news-time market reactions using only frozen OHLCV bars."""

from __future__ import annotations

import math
import statistics
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import parse_timestamp


EXCHANGE = {
    "US": {"timezone": "America/New_York", "close": time(16, 0)},
    "HK": {"timezone": "Asia/Hong_Kong", "close": time(16, 0)},
}


def _returns(closes: list[float]) -> list[float]:
    return [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)) if closes[index - 1] > 0]


def _price_direction(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return "positive" if value > 0 else "negative" if value < 0 else "flat"


def _reaction_status(event_direction: str, price_direction: str) -> str:
    if event_direction == "positive" and price_direction == "positive":
        return "confirmed_positive"
    if event_direction == "positive" and price_direction == "negative":
        return "sell_the_news_or_expectations_too_high"
    if event_direction == "negative" and price_direction == "positive":
        return "bad_news_priced_in_or_relief"
    if event_direction == "negative" and price_direction == "negative":
        return "confirmed_negative"
    return "not_classifiable"


def validate_reaction(
    *, published_at: str, event_direction: str, market: str, bars: list[dict[str, Any]],
) -> dict[str, Any]:
    if market not in EXCHANGE:
        return {"status": "unavailable", "warning": f"no session-time mapping for market {market!r}"}
    if not bars:
        return {"status": "unavailable", "warning": "frozen market history contains no bars"}

    exchange = EXCHANGE[market]
    local = parse_timestamp(published_at).astimezone(ZoneInfo(exchange["timezone"]))
    local_day = local.date()
    same_session_eligible = local.timetz().replace(tzinfo=None) <= exchange["close"]
    dates = [str(row["date"])[:10] for row in bars]
    if dates and local_day.isoformat() < dates[0]:
        return {
            "status": "unavailable", "market_session_date": None,
            "session_mapping_rule": "first frozen exchange session ending after publication",
            "daily_close_return": None, "opening_gap_return": None,
            "abnormal_volume_z": None, "pre_event_volatility_5d": None,
            "post_event_volatility_5d": None, "volatility_change": None,
            "sector_relative_return": None,
            "warning": "news predates the available frozen OHLCV window; no market reaction is assessed",
        }
    candidate_indices = [index for index, day in enumerate(dates)
                         if day >= local_day.isoformat() and (day > local_day.isoformat() or same_session_eligible)]
    if not candidate_indices:
        return {
            "status": "unavailable", "market_session_date": None,
            "session_mapping_rule": "first frozen exchange session ending after publication",
            "daily_close_return": None, "opening_gap_return": None,
            "abnormal_volume_z": None, "pre_event_volatility_5d": None,
            "post_event_volatility_5d": None, "volatility_change": None,
            "sector_relative_return": None,
            "warning": "no frozen market session follows this news item within the snapshot",
        }
    index = candidate_indices[0]
    warning = "daily OHLCV cannot isolate intraday reaction or establish causality"
    previous_close: float | None = None
    daily_return: float | None = None
    opening_gap: float | None = None
    if index > 0:
        previous_close = float(bars[index - 1]["close"])
        current_close = float(bars[index]["close"])
        current_open = float(bars[index]["open"])
        if previous_close > 0:
            daily_return = current_close / previous_close - 1
            opening_gap = current_open / previous_close - 1

    past_volumes = [float(row["volume"]) for row in bars[max(0, index - 20):index]]
    abnormal_volume_z: float | None = None
    if len(past_volumes) >= 5:
        mean_volume = statistics.mean(past_volumes)
        std_volume = statistics.stdev(past_volumes)
        if std_volume > 0:
            abnormal_volume_z = (float(bars[index]["volume"]) - mean_volume) / std_volume

    closes = [float(row["close"]) for row in bars]
    returns = _returns(closes)
    # returns[k] is from bars[k] to bars[k+1].
    pre_returns = returns[max(0, index - 6):max(0, index - 1)]
    post_returns = returns[max(0, index - 1):min(len(returns), index + 4)]
    pre_vol = statistics.stdev(pre_returns) if len(pre_returns) >= 3 else None
    post_vol = statistics.stdev(post_returns) if len(post_returns) >= 3 else None
    vol_change = post_vol - pre_vol if post_vol is not None and pre_vol is not None else None
    price_direction = _price_direction(daily_return)
    return {
        "status": _reaction_status(event_direction, price_direction),
        "market_session_date": dates[index],
        "session_mapping_rule": "same local exchange session if published by 16:00; otherwise first later frozen session",
        "daily_close_return": daily_return,
        "opening_gap_return": opening_gap,
        "price_direction": price_direction,
        "abnormal_volume_z": abnormal_volume_z,
        "pre_event_volatility_5d": pre_vol,
        "post_event_volatility_5d": post_vol,
        "volatility_change": vol_change,
        "sector_relative_return": None,
        "warning": warning,
    }
