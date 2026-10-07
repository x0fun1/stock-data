"""Configurable event recency decay in observed exchange sessions."""

from __future__ import annotations

import math
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts import HORIZONS, parse_timestamp
from ..data_checks import EXCHANGE_TIMEZONES


def half_life_for(event_type: str, horizon: str, policy: dict[str, Any]) -> float:
    overrides = policy.get("half_life_sessions_by_event_type", {})
    value = overrides.get(event_type, overrides.get("default", HORIZONS[horizon])) if isinstance(overrides, dict) else HORIZONS[horizon]
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(HORIZONS[horizon])
    if not math.isfinite(number) or number <= 0:
        raise ValueError("news half-life values must be finite positive session counts")
    return number


def time_decay(event_published_at: str, bars: list[dict[str, Any]], event_type: str, horizon: str, policy: dict[str, Any], *, market: str = "US") -> dict[str, float | int]:
    half_life = half_life_for(event_type, horizon, policy)
    event_day = parse_timestamp(event_published_at).astimezone(ZoneInfo(EXCHANGE_TIMEZONES[market])).date()
    session_dates = [date.fromisoformat(str(row["date"])[:10]) for row in bars]
    age_sessions = sum(session_day > event_day for session_day in session_dates)
    decay = 0.5 ** (age_sessions / half_life)
    return {"age_sessions": age_sessions, "half_life_sessions": half_life, "time_decay": decay}
