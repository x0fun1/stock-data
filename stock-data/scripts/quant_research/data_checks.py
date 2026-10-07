"""Assess data integrity separately from forecast eligibility and report quality."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import parse_timestamp
from .gate_policy import classify_reasons

EXCHANGE_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}
ADJUSTMENTS = {"adjusted", "unadjusted", "split_adjusted", "total_return"}
MIN_OBSERVATION_BARS = 21  # Minimum input for the predeclared 20-observation baseline.
PRICE_MATCH_TOLERANCE = 0.003


def calendar_sessions(block: dict[str, Any], market: str) -> list[dict[str, Any]]:
    """Validate a supplied exchange schedule. This validates shape, not authenticity."""
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


def _library_calendar(market: str, start: str, end: str) -> dict[str, Any] | None:
    """Build a freeze-time exchange schedule if a supported optional library is installed."""
    calendar_code = {"US": "XNYS", "HK": "XHKG"}[market]
    try:
        import exchange_calendars  # type: ignore[import-not-found]
        import exchange_calendars as _exchange_calendars  # type: ignore[import-not-found]

        schedule = exchange_calendars.get_calendar(calendar_code).schedule.loc[start:end]
        sessions = [{"date": str(session)[:10], "close_at": close.isoformat()}
                    for session, close in zip(schedule.index, schedule["close"])]
        if sessions:
            return {"source": f"exchange_calendars:{getattr(_exchange_calendars, '__version__', 'unknown')}",
                    "timezone": EXCHANGE_TIMEZONES[market], "coverage_start": start,
                    "coverage_end": end, "sessions": sessions}
    except Exception:
        pass
    try:
        import pandas_market_calendars  # type: ignore[import-not-found]
        import pandas_market_calendars as _pandas_market_calendars  # type: ignore[import-not-found]

        calendar_name = {"US": "NYSE", "HK": "HKEX"}[market]
        schedule = pandas_market_calendars.get_calendar(calendar_name).schedule(start_date=start, end_date=end)
        sessions = [{"date": str(session)[:10], "close_at": close.isoformat()}
                    for session, close in zip(schedule.index, schedule["market_close"])]
        if sessions:
            return {"source": f"pandas_market_calendars:{getattr(_pandas_market_calendars, '__version__', 'unknown')}",
                    "timezone": EXCHANGE_TIMEZONES[market], "coverage_start": start,
                    "coverage_end": end, "sessions": sessions}
    except Exception:
        pass
    return None


def _check_price_observations(block: dict[str, Any], bars: list[dict[str, Any]], last: str) -> tuple[str, list[str], list[str], list[str]]:
    """Compare optional same-session daily closes to the normalized last bar."""
    codes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    observations = block.get("latest_price_observations", [])
    if observations is None:
        observations = []
    if not isinstance(observations, list):
        observations = []
        codes.append("LATEST_CLOSE_SINGLE_PROVIDER")
        warnings.append("Additional latest-price observations were malformed and not used for cross-source verification.")
    base = {"source": block.get("actual_source"), "session_date": last,
            "price": bars[-1].get("close"), "currency": block.get("currency"), "kind": "daily_close"}
    accepted: list[dict[str, Any]] = [base] if isinstance(base["source"], str) and base["source"] else []
    for item in observations:
        try:
            if not isinstance(item, dict) or item.get("kind", "daily_close") not in {"daily_close", "close"}:
                continue
            source = item.get("source")
            session_day = date.fromisoformat(item["session_date"]).isoformat()
            price = float(item["price"])
            currency = item.get("currency")
            if (not isinstance(source, str) or not source or not math.isfinite(price) or price <= 0
                    or not isinstance(currency, str) or not currency):
                continue
            accepted.append({"source": source, "session_date": session_day, "price": price,
                             "currency": currency, "kind": "daily_close"})
        except (KeyError, TypeError, ValueError):
            continue
    same_session = [row for row in accepted if row["session_date"] == last and row["currency"] == block.get("currency")]
    distinct = {row["source"] for row in same_session}
    for index, left in enumerate(same_session):
        for right in same_session[index + 1:]:
            scale = min(float(left["price"]), float(right["price"]))
            if abs(float(left["price"]) - float(right["price"])) / scale > PRICE_MATCH_TOLERANCE:
                codes.append("LATEST_PRICE_CONFLICT")
                errors.append(f"latest same-session closes from {left['source']} and {right['source']} differ by more than 0.3%")
    if len(distinct) >= 2 and "LATEST_PRICE_CONFLICT" not in codes:
        codes.append("LATEST_CLOSE_CROSS_VERIFIED")
        return "cross_source_verified", codes, warnings, errors
    if len(distinct) < 2:
        codes.append("LATEST_CLOSE_SINGLE_PROVIDER")
        warnings.append("Latest close has one usable provider; source-level timestamp checks passed, but cross-source matching is unavailable.")
    return "provider_verified", codes, warnings, errors


def _gate(reason_codes: list[str], errors: list[str], blockers: list[str], warnings: list[str], latest_close: dict[str, Any] | None,
          *, calendar_status: str, horizon_type: str, resolved_calendar_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    classification = classify_reasons(reason_codes)
    fatal = classification["blockers"]
    warning_codes = classification["warnings"]
    return {
        "forecast_eligible": classification["prediction_eligible"],  # Compatibility alias: computable, not high-confidence.
        "prediction_eligibility": {"status": "blocked" if fatal else "eligible", "blockers": fatal},
        "reporting_eligibility": {"status": classification["status"], "warnings": warning_codes,
                                  "informational": classification["informational"]},
        "reporting_status": classification["status"],
        "errors": errors,
        "blockers": blockers,
        "warnings": warnings,
        "reason_codes": reason_codes,
        "latest_confirmed_close": latest_close,
        "latest_close_status": latest_close.get("kind") if latest_close else "unavailable",
        "calendar_verification": calendar_status,
        "resolved_calendar_evidence": resolved_calendar_evidence,
        "horizon_type": horizon_type,
        "horizon_confidence": "high" if horizon_type == "exchange_sessions" else "low",
        "source_authentication": "not_independently_verified",
        "session_horizon_verified": horizon_type == "exchange_sessions",
    }


def check_market(block: dict[str, Any], request: dict[str, Any], *, captured_at: str | None = None,
                resolved_calendar_evidence: dict[str, Any] | None = None,
                allow_library_calendar: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    reason_codes: list[str] = []

    def fatal(code: str, message: str) -> None:
        reason_codes.append(code)
        blockers.append(message)

    def degraded(code: str, message: str) -> None:
        reason_codes.append(code)
        warnings.append(message)

    for field in ("actual_source", "currency", "unit"):
        if block.get(field) is not None and (not isinstance(block[field], str) or not 0 < len(block[field]) <= 200):
            errors.append(f"market {field} must be a bounded non-empty provenance string")
            reason_codes.append("SOURCE_IDENTITY_CONFLICT" if field == "actual_source" else "MARKET_UNITS_UNVERIFIED")
    data = block.get("data") or {}
    bars = data.get("bars") or []
    if not bars:
        fatal("INSUFFICIENT_OHLCV", "no closed OHLCV bars")
        return _gate(reason_codes, errors, blockers, warnings, None, calendar_status="unavailable", horizon_type="unavailable")

    asof = parse_timestamp(request["asof"])
    zone = ZoneInfo(EXCHANGE_TIMEZONES[request["market"]])
    local_day = asof.astimezone(zone).date()
    first, last = str(bars[0]["date"])[:10], str(bars[-1]["date"])[:10]
    if len(bars) < MIN_OBSERVATION_BARS:
        fatal("INSUFFICIENT_OHLCV", f"only {len(bars)} daily observations; at least {MIN_OBSERVATION_BARS} are required for the minimum 20-observation feature")

    symbol = data.get("symbol", block.get("source_symbol"))
    instrument = request.get("instrument") or {}
    expected = (instrument.get("provider_symbols") or {}).get(block.get("actual_source"), request["ticker"]) if isinstance(block.get("actual_source"), str) else request["ticker"]
    if symbol is None:
        fatal("SOURCE_IDENTITY_UNVERIFIED", "market source symbol is missing")
    elif not isinstance(symbol, str) or symbol.upper() != str(expected).upper():
        errors.append("market source symbol does not match the resolved request instrument")
        reason_codes.append("SOURCE_IDENTITY_CONFLICT")
    frequency = data.get("frequency", block.get("frequency"))
    if frequency is None:
        fatal("FREQUENCY_UNVERIFIED", "market frequency is missing")
    elif frequency not in {"1d", "1D", "daily"}:
        errors.append("direction research requires daily frequency, not a compressed/weekly series")
        reason_codes.append("FREQUENCY_UNVERIFIED")
    if not block.get("actual_source"):
        fatal("SOURCE_IDENTITY_UNVERIFIED", "market actual_source is missing")
    if not block.get("currency") or not block.get("unit"):
        fatal("MARKET_UNITS_UNVERIFIED", "market currency/unit provenance is missing")
    for field in ("currency", "unit"):
        if data.get(field) and block.get(field) and data[field] != block[field]:
            errors.append(f"market {field} differs between metadata and payload")
            reason_codes.append("MARKET_UNITS_UNVERIFIED")
        if instrument.get(field) and block.get(field) != instrument[field]:
            errors.append(f"market {field} differs from the resolved instrument")
            reason_codes.append("MARKET_UNITS_UNVERIFIED")

    fetched = block.get("fetched_at_utc", block.get("fetched_at"))
    parsed_times: dict[str, datetime] = {}
    for field, value in (("source_timestamp", block.get("source_timestamp")), ("fetched_at", fetched)):
        if value is None:
            fatal("TEMPORAL_PROVENANCE_MISSING", f"market {field} is missing")
        else:
            try:
                parsed_times[field] = parse_timestamp(value)
            except (TypeError, ValueError):
                errors.append(f"market {field} must be a timezone-aware timestamp")
                reason_codes.append("TEMPORAL_PROVENANCE_MISSING")
    source_time = parsed_times.get("source_timestamp")
    fetch_time = parsed_times.get("fetched_at")
    if source_time and source_time > asof:
        errors.append("market source_timestamp occurs after the requested as-of")
        reason_codes.append("TEMPORAL_PROVENANCE_MISSING")
    if source_time and fetch_time and source_time > fetch_time:
        errors.append("market source_timestamp occurs after fetched_at")
        reason_codes.append("TEMPORAL_PROVENANCE_MISSING")
    if source_time and source_time.astimezone(zone).date() < date.fromisoformat(last):
        fatal("LATEST_OBSERVATION_UNRESOLVED", "market source timestamp predates the latest OHLCV observation")
    if captured_at is None:
        fatal("TEMPORAL_PROVENANCE_MISSING", "collection capture timestamp is missing")
    else:
        try:
            capture_time = parse_timestamp(captured_at)
            if fetch_time and fetch_time > capture_time:
                errors.append("market fetched_at occurs after collection capture")
                reason_codes.append("TEMPORAL_PROVENANCE_MISSING")
        except (TypeError, ValueError):
            errors.append("collection capture timestamp must be timezone-aware")
            reason_codes.append("TEMPORAL_PROVENANCE_MISSING")

    window = data.get("history_window")
    if not isinstance(window, dict):
        fatal("SOURCE_IDENTITY_UNVERIFIED", "market actual history_window is missing")
    else:
        try:
            if date.fromisoformat(window["start"]).isoformat() != first or date.fromisoformat(window["end"]).isoformat() != last:
                errors.append("market history_window must describe the actual normalized closed bars")
                reason_codes.append("SOURCE_IDENTITY_CONFLICT")
            if not window.get("selection"):
                fatal("SOURCE_IDENTITY_UNVERIFIED", "market history_window selection is missing")
        except (KeyError, TypeError, ValueError):
            errors.append("market history_window has invalid start/end")
            reason_codes.append("SOURCE_IDENTITY_CONFLICT")
    requested = request.get("history_window") or {}
    if requested.get("start") and first < requested["start"] or requested.get("end") and last > requested["end"]:
        errors.append("market bars extend outside the user-requested history window")
        reason_codes.append("SOURCE_IDENTITY_CONFLICT")

    # Calendar resolution: supplied schedule -> pinned optional library schedule -> observed dates.
    # Optional schedules are pinned in the manifest at freeze time; reloading a snapshot never
    # depends on which calendar packages happen to be installed later.
    sessions: list[dict[str, Any]] = []
    calendar_status = "observation_fallback"
    resolved = resolved_calendar_evidence
    calendar = block.get("session_calendar") or resolved
    calendar_block = {**block, "session_calendar": calendar} if calendar is not None else block
    if calendar is not None:
        try:
            candidate = calendar_sessions(calendar_block, request["market"])
            if calendar["coverage_start"] <= first and calendar["coverage_end"] >= local_day.isoformat():
                sessions = candidate
                calendar_status = "library_verified" if resolved is not None and not block.get("session_calendar") else "source_supplied_schedule"
            else:
                degraded("CALENDAR_OFFICIAL_MISSING", "supplied exchange calendar does not cover the full history through as-of")
        except (KeyError, TypeError, ValueError) as exc:
            degraded("CALENDAR_SOURCE_INVALID", f"supplied exchange calendar could not be used; resolving a fallback ({type(exc).__name__})")
    if not sessions and allow_library_calendar:
        resolved = _library_calendar(request["market"], first, local_day.isoformat())
        if resolved:
            sessions = resolved["sessions"]
            calendar_status = "library_verified"
    if not sessions:
        if calendar is None:
            degraded("CALENDAR_OFFICIAL_MISSING", "no complete source-backed exchange calendar is available")
        degraded("CALENDAR_FALLBACK_USED", "session horizon uses ordered OHLCV observations; calendar holidays and missing sessions are not independently verified")
        for bar in bars:
            try:
                day = date.fromisoformat(str(bar["date"])[:10])
                if day.weekday() >= 5:
                    fatal("INVALID_TIME_ORDER", f"daily stock observation falls on a weekend: {day.isoformat()}")
            except (KeyError, TypeError, ValueError):
                fatal("INVALID_TIME_ORDER", "an OHLCV observation has an invalid session date")
        for left, right in zip(bars, bars[1:]):
            try:
                gap_days = (date.fromisoformat(str(right["date"])[:10]) - date.fromisoformat(str(left["date"])[:10])).days
                if gap_days > 14:
                    fatal("SEVERE_MISSING_BARS", f"OHLCV observations have an unexplained {gap_days}-calendar-day gap")
            except (KeyError, TypeError, ValueError):
                fatal("INVALID_TIME_ORDER", "OHLCV observation dates are not parseable")
        horizon_type = "estimated_observed_sessions"
    else:
        horizon_type = "exchange_sessions"
        closed = [row for row in sessions if parse_timestamp(row["close_at"]) <= asof]
        expected_latest = closed[-1]["date"] if closed else None
        if expected_latest != last:
            fatal("LATEST_OBSERVATION_UNRESOLVED", f"latest bar {last} does not match the latest closed exchange session {expected_latest}")
        expected_axis = [row["date"] for row in closed if first <= row["date"] <= last]
        actual_axis = [str(row["date"])[:10] for row in bars]
        if expected_axis != actual_axis:
            fatal("SEVERE_MISSING_BARS", "daily bars do not match the resolved exchange session axis; confirmed sessions are missing")

    if (local_day - date.fromisoformat(last)).days > 7:
        fatal("STALE_MARKET_DATA", "market history is more than seven calendar days old relative to as-of")

    latest_status, latest_codes, latest_warnings, latest_errors = _check_price_observations(block, bars, last)
    reason_codes.extend(latest_codes)
    warnings.extend(latest_warnings)
    if "LATEST_PRICE_CONFLICT" in latest_codes:
        blockers.extend(latest_errors)
    else:
        errors.extend(latest_errors)
    latest_close = {"price": bars[-1]["close"], "session_date": last, "close_at": None,
                    "currency": block.get("currency"), "kind": latest_status, "source": block.get("actual_source")}
    if block.get("last_bar_closed") is False:
        fatal("LATEST_OBSERVATION_UNRESOLVED", "provider marked the latest OHLCV bar as not closed")
        latest_close["kind"] = "unverified"
    if sessions:
        close_row = next((row for row in sessions if row.get("date") == last), None)
        if close_row:
            close_at = close_row.get("close_at")
            latest_close["close_at"] = close_at
            if fetch_time and source_time and parse_timestamp(close_at) <= asof:
                if fetch_time < parse_timestamp(close_at) or source_time < parse_timestamp(close_at):
                    fatal("LATEST_OBSERVATION_UNRESOLVED", "market source/fetch timestamp predates the latest session close")
                    latest_close["kind"] = "unverified"
                    reason_codes = [code for code in reason_codes if code != "LATEST_CLOSE_CROSS_VERIFIED"]
            if latest_close["kind"] == "provider_verified" and calendar_status in {"source_supplied_schedule", "library_verified"} and latest_close["kind"] != "unverified":
                latest_close["kind"] = "calendar_verified"

    adjustment = block.get("adjustment")
    evidence = block.get("adjustment_evidence")
    corporate_action_status = "unavailable"
    if adjustment not in ADJUSTMENTS:
        degraded("ADJUSTMENT_UNKNOWN", "OHLCV adjustment convention is unknown; short-horizon direction remains computable with lower confidence")
    if not isinstance(evidence, dict) or not evidence.get("source") or not isinstance(evidence.get("actions"), list):
        degraded("ADJUSTMENT_EVIDENCE_UNAVAILABLE", "source-backed adjustment/company-action coverage is unavailable; unknown does not mean contaminated")
        degraded("CORPORATE_ACTION_UNKNOWN", "recent corporate actions could not be independently assessed")
    else:
        if evidence.get("price_basis") != adjustment:
            degraded("ADJUSTMENT_EVIDENCE_CONFLICT", "adjustment evidence does not establish the OHLCV price basis")
        try:
            checked = date.fromisoformat(evidence["checked_through"]).isoformat()
            evidence_covered = checked >= last
            if not evidence_covered:
                degraded("CORPORATE_ACTION_UNKNOWN", "company-action evidence does not cover the latest market bar")
            else:
                corporate_action_status = "clear"
            for action in evidence["actions"]:
                action_day = date.fromisoformat(action["date"]).isoformat()
                if first <= action_day <= last:
                    corporate_action_status = "recent_action_detected"
                action_type = str(action.get("type", "")).lower().replace("-", "_")
                contaminating_type = action_type in {"split", "reverse_split", "reverse split", "spinoff", "spin_off", "spin off", "major_adjustment_discontinuity", "adjustment_discontinuity"}
                prices_unadjusted = adjustment == "unadjusted" or evidence.get("price_basis") == "unadjusted"
                if contaminating_type and first <= action_day <= last and prices_unadjusted:
                    fatal("CORPORATE_ACTION_CONTAMINATION", "confirmed corporate action falls in the selected window while OHLCV is evidenced as unadjusted")
        except (KeyError, TypeError, ValueError):
            corporate_action_status = "unavailable"
            degraded("CORPORATE_ACTION_UNKNOWN", "adjustment/company-action evidence is malformed and was not relied on")
    try:
        if any(abs(float(bars[i]["close"]) / float(bars[i - 1]["close"]) - 1) > 0.5 for i in range(1, len(bars))):
            degraded("EXTREME_PRICE_MOVE_UNRECONCILED", "an extreme close-to-close move needs review; it was not automatically treated as a confirmed corporate action")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        fatal("INVALID_PRICE_VALUES", "OHLCV price values cannot be evaluated")
    if request.get("asset_type") in {None, "unknown"}:
        degraded("ASSET_TYPE_UNVERIFIED", "asset type is unverified; ETF-specific exposure/NAV/leverage checks are not assessed")
    if request.get("asset_type") == "etf":
        degraded("ASSET_TYPE_UNVERIFIED", "ETF NAV, holdings-date, tracking error and leverage/path risks are not assessed by the OHLCV engine")
    result = _gate(reason_codes, errors, blockers, warnings, latest_close,
                   calendar_status=calendar_status, horizon_type=horizon_type,
                   resolved_calendar_evidence=resolved if calendar_status == "library_verified" else None)
    result["adjustment_status"] = "provider_declared" if adjustment in ADJUSTMENTS else "unknown"
    result["corporate_action_status"] = corporate_action_status
    return result