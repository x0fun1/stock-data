"""Semantic market gates using supplied source evidence, never guessed calendars."""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import parse_timestamp

EXCHANGE_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}
ADJUSTMENTS = {"adjusted", "unadjusted", "split_adjusted", "total_return"}


def calendar_sessions(block: dict[str, Any], market: str) -> list[dict[str, Any]]:
    calendar = block.get("session_calendar")
    if not isinstance(calendar, dict) or not calendar.get("source"):
        raise ValueError("source-backed session_calendar is unavailable")
    if calendar.get("timezone") != EXCHANGE_TIMEZONES[market]:
        raise ValueError("session_calendar timezone does not match the requested market")
    start, end = date.fromisoformat(calendar["coverage_start"]), date.fromisoformat(calendar["coverage_end"])
    if start > end:
        raise ValueError("session_calendar coverage is reversed")
    sessions = calendar.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("session_calendar.sessions must be non-empty")
    previous = None
    for row in sessions:
        day = date.fromisoformat(row["date"])
        close = parse_timestamp(row["close_at"])
        local = close.astimezone(ZoneInfo(EXCHANGE_TIMEZONES[market]))
        if local.date() != day or not start <= day <= end or previous is not None and day <= previous:
            raise ValueError("session_calendar dates/close timestamps must be ordered, unique and within coverage")
        previous = day
    return sessions


def check_market(block: dict[str, Any], request: dict[str, Any], *, captured_at: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for field in ("actual_source", "currency", "unit"):
        if block.get(field) is not None and (not isinstance(block[field], str) or not 0 < len(block[field]) <= 200):
            errors.append(f"market {field} must be a bounded non-empty provenance string")
    data = block.get("data") or {}
    bars = data.get("bars") or []
    if not bars:
        return {"forecast_eligible": False, "errors": [], "blockers": ["no closed OHLCV bars"], "warnings": [], "latest_confirmed_close": None}
    asof = parse_timestamp(request["asof"])
    zone = ZoneInfo(EXCHANGE_TIMEZONES[request["market"]])
    local_day = asof.astimezone(zone).date()
    first, last = str(bars[0]["date"])[:10], str(bars[-1]["date"])[:10]
    symbol = data.get("symbol", block.get("source_symbol"))
    instrument = request.get("instrument") or {}
    expected = (instrument.get("provider_symbols") or {}).get(block.get("actual_source"), request["ticker"]) if isinstance(block.get("actual_source"), str) else request["ticker"]
    if symbol is None:
        blockers.append("market source symbol is missing")
    elif not isinstance(symbol, str) or symbol.upper() != str(expected).upper():
        errors.append("market source symbol does not match the resolved request instrument")
    frequency = data.get("frequency", block.get("frequency"))
    if frequency is None:
        blockers.append("market frequency is missing")
    elif frequency not in {"1d", "1D", "daily"}:
        errors.append("direction research requires daily frequency, not a compressed/weekly series")
    if not block.get("actual_source"):
        blockers.append("market actual_source is missing")
    if not block.get("currency") or not block.get("unit"):
        blockers.append("market currency/unit provenance is missing")
    for field in ("currency", "unit"):
        if data.get(field) and block.get(field) and data[field] != block[field]:
            errors.append(f"market {field} differs between metadata and payload")
        if instrument.get(field) and block.get(field) != instrument[field]:
            errors.append(f"market {field} differs from the resolved instrument")
    fetched = block.get("fetched_at_utc", block.get("fetched_at"))
    parsed_times = {}
    for field, value in (("source_timestamp", block.get("source_timestamp")), ("fetched_at", fetched)):
        if value is None:
            blockers.append(f"market {field} is missing")
        else:
            try:
                parsed_times[field] = parse_timestamp(value)
            except (TypeError, ValueError):
                errors.append(f"market {field} must be a timezone-aware timestamp")
    source_time = parsed_times.get("source_timestamp")
    fetch_time = parsed_times.get("fetched_at")
    if source_time and source_time > asof:
        errors.append("market source_timestamp occurs after the requested as-of")
    if source_time and fetch_time and source_time > fetch_time:
        errors.append("market source_timestamp occurs after fetched_at")
    if captured_at is None:
        blockers.append("collection capture timestamp is missing")
    else:
        capture_time = parse_timestamp(captured_at)
        if fetch_time and fetch_time > capture_time:
            errors.append("market fetched_at occurs after collection capture")

    window = data.get("history_window")
    if not isinstance(window, dict):
        blockers.append("market actual history_window is missing")
    else:
        try:
            if date.fromisoformat(window["start"]).isoformat() != first or date.fromisoformat(window["end"]).isoformat() != last:
                errors.append("market history_window must describe the actual normalized closed bars")
            if not window.get("selection"):
                blockers.append("market history_window selection is missing")
        except (KeyError, TypeError, ValueError):
            errors.append("market history_window has invalid start/end")
    requested = request.get("history_window") or {}
    if requested.get("start") and first < requested["start"] or requested.get("end") and last > requested["end"]:
        errors.append("market bars extend outside the user-requested history window")

    sessions: list[dict[str, Any]] = []
    latest_close = None
    if block.get("session_calendar") is None:
        blockers.append("source-backed exchange calendar is missing; session horizon and latest close are unverified")
    else:
        try:
            sessions = calendar_sessions(block, request["market"])
            calendar = block["session_calendar"]
            if calendar["coverage_start"] > first or calendar["coverage_end"] < local_day.isoformat():
                blockers.append("exchange calendar does not cover the history and requested as-of")
            closed = [row for row in sessions if parse_timestamp(row["close_at"]) <= asof]
            expected_latest = closed[-1]["date"] if closed else None
            if expected_latest != last:
                blockers.append(f"stale/unclosed history: latest bar {last}, expected latest closed session {expected_latest}")
            expected_axis = [row["date"] for row in closed if first <= row["date"] <= last]
            actual_axis = [str(row["date"])[:10] for row in bars]
            if expected_axis != actual_axis:
                blockers.append("daily bars do not match the supplied exchange session axis; missing/unclosed sessions cannot be compressed")
            close_row = next((row for row in closed if row["date"] == last), None)
            if close_row:
                close_time = parse_timestamp(close_row["close_at"])
                if fetch_time and fetch_time < close_time:
                    blockers.append("latest daily bar was fetched before its session close")
                if source_time and source_time < close_time:
                    blockers.append("market source_timestamp predates the latest daily session close")
                if fetch_time and source_time and fetch_time >= close_time and source_time >= close_time:
                    latest_close = {"price": bars[-1]["close"], "session_date": last, "close_at": close_row["close_at"], "currency": block.get("currency"), "kind": "confirmed_session_close", "source": block.get("actual_source")}
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid exchange calendar: {exc}")
    if (local_day - date.fromisoformat(last)).days > 7:
        blockers.append("market history is more than seven calendar days old relative to as-of")

    adjustment = block.get("adjustment")
    evidence = block.get("adjustment_evidence")
    if adjustment not in ADJUSTMENTS:
        blockers.append("OHLCV adjustment convention is unknown")
    if not isinstance(evidence, dict) or not evidence.get("source") or not isinstance(evidence.get("actions"), list):
        blockers.append("source-backed adjustment/company-action evidence is missing")
    else:
        if evidence.get("price_basis") != adjustment:
            errors.append("adjustment evidence does not match the OHLCV price basis")
        try:
            if date.fromisoformat(evidence["checked_through"]).isoformat() < last:
                blockers.append("company-action coverage ends before the latest market bar")
            for action in evidence["actions"]:
                action_day = date.fromisoformat(action["date"]).isoformat()
                if action.get("type") == "split" and first <= action_day <= last and adjustment == "unadjusted":
                    blockers.append("unadjusted split in the selected window invalidates return research")
        except (KeyError, TypeError, ValueError):
            errors.append("invalid adjustment/company-action evidence")
    if any(abs(float(bars[i]["close"]) / float(bars[i - 1]["close"]) - 1) > 0.5 for i in range(1, len(bars))):
        blockers.append("extreme close-to-close move requires source correction/company-action reconciliation")
    if request.get("asset_type") in {None, "unknown"}:
        warnings.append("asset type is unverified; ETF-specific exposure/NAV/leverage checks are not assessed")
    if request.get("asset_type") == "etf":
        warnings.append("ETF NAV, holdings-date, tracking error and leverage/path risks are not assessed by the OHLCV engine")
    if latest_close is None:
        latest_close = {"price": bars[-1]["close"], "session_date": last, "close_at": None, "currency": block.get("currency"), "kind": "historical_bar_close_unverified", "source": block.get("actual_source")}
    return {"forecast_eligible": not errors and not blockers, "errors": errors, "blockers": blockers, "warnings": warnings,
            "latest_confirmed_close": latest_close, "calendar_verification": "supplied_source_evidence" if sessions else "unverified",
            "source_authentication": "not_independently_verified", "session_horizon_verified": bool(sessions) and not any("session" in item for item in blockers)}
