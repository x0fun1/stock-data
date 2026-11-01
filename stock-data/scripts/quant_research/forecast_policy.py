"""Shared analysis-policy constants for the news-focused runtime.

The former fixed 52-week / 5-session forecast policy was removed in v4.  What
remains is the market-data window used by descriptive price statistics and the
request/stage contract shared by the collector, freezer and CLI.  This module has
no provider or filesystem access.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

POLICY_VERSION = "news_60d_v1"

#: Descriptive trend statistics use at most the latest 52 weeks (about 250
#: exchange sessions).  This is a display bound, not a forecast target.
HISTORY_CALENDAR_DAYS = 364

#: News collection window: (as_of - 60 days, as_of].
NEWS_WINDOW_DAYS = 60
NEWS_RECENT_WINDOW_DAYS = 30

#: The price basis retained for descriptive market data.
PRICE_BASIS = "split_adjusted"
PRICE_BASIS_SOURCE = "provider_declared_split_adjusted_close"
PRICE_BASIS_NOTE = (
    "provider close, split-adjusted, cash dividends not reinvested; "
    "fields that include dividend adjustment are not accepted as this basis"
)

#: Receipt stages required for a complete news analysis run.
REQUIRED_STAGES: tuple[str, ...] = (
    "request",
    "collection",
    "data",
    "news",
    "final",
)

ERROR_FORECAST_REMOVED = "FORECAST_REMOVED"
ERROR_FIXED_WINDOW_CONFLICT = "FIXED_WINDOW_CONFLICT"
ERROR_INSUFFICIENT_COVERAGE = "INSUFFICIENT_HISTORY_COVERAGE"
ERROR_CALENDAR_UNVERIFIED = "CALENDAR_UNVERIFIED"
ERROR_PRICE_BASIS_UNVERIFIED = "PRICE_BASIS_UNVERIFIED"
ERROR_POLICY_MISMATCH = "POLICY_VERSION_MISMATCH"


class PolicyError(ValueError):
    """Validation failure carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def policy_metadata() -> dict[str, Any]:
    """Serialisable policy block written into requests and snapshots."""

    return {
        "policy_version": POLICY_VERSION,
        "history_calendar_days": HISTORY_CALENDAR_DAYS,
        "news_window_days": NEWS_WINDOW_DAYS,
        "news_recent_window_days": NEWS_RECENT_WINDOW_DAYS,
        "price_basis": PRICE_BASIS,
        "price_basis_source": PRICE_BASIS_SOURCE,
        "target": "news_events_descriptive_only",
    }


def window_bounds(decision_session: date | str) -> tuple[str, str]:
    """Return ``(window_start_exclusive, window_end_inclusive)`` as ISO dates."""

    end = date.fromisoformat(decision_session) if isinstance(decision_session, str) else decision_session
    start = end - timedelta(days=HISTORY_CALENDAR_DAYS)
    return start.isoformat(), end.isoformat()


def window_contains(session: date | str, start_exclusive: str, end_inclusive: str) -> bool:
    value = session.isoformat() if isinstance(session, date) else str(session)
    return start_exclusive < value <= end_inclusive


def validate_declared_window(
    declared: dict[str, Any] | None,
    decision_session: date | str,
    *,
    allow_missing: bool = True,
) -> tuple[str, str]:
    """Reject user-supplied windows that do not match the shared boundary."""

    start, end = window_bounds(decision_session)
    if not declared:
        if allow_missing:
            return start, end
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, "history window is required")
    declared_start = declared.get("start")
    declared_end = declared.get("end")
    if declared_start and declared_start != start:
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, f"history start {declared_start} does not match boundary {start}")
    if declared_end and declared_end != end:
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, f"history end {declared_end} does not match boundary {end}")
    return start, end


def sessions_between(sessions: list[date], start_exclusive: str, end_inclusive: str) -> list[date]:
    """Sessions strictly after ``start_exclusive`` and at/before ``end_inclusive``."""

    return [item for item in sessions if start_exclusive < item.isoformat() <= end_inclusive]


def describe_window(decision_session: str) -> str:
    start, end = window_bounds(decision_session)
    return f"截至{end}的52周（{HISTORY_CALENDAR_DAYS}个自然日，窗口({start}, {end}]）"
