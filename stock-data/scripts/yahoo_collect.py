"""Explicit Yahoo market collection; the research pipeline remains offline.

Only normalized provider data is projected. Calendar evidence is optional, and
unknown provenance/closure is never replaced with a guessed symbol or clock.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_research.contracts import normalize_request, parse_timestamp, utc_now
from quant_research.data_checks import (
    EXCHANGE_TIMEZONES,
    _library_calendar,
    calendar_sessions,
)
from quant_research.forecast_policy import (
    HISTORY_CALENDAR_DAYS,
    POLICY_VERSION,
    PRICE_BASIS,
    PRICE_BASIS_NOTE,
    PRICE_BASIS_SOURCE,
    validate_declared_window,
    window_bounds,
)
from quant_research.security import provider_failed, sanitize_data

_METADATA = ("currency", "unit", "frequency", "source_symbol", "source_timestamp", "source_timestamp_kind",
             "fetched_at_utc", "adjustment", "adjustment_evidence", "provider_library",
             "provider_version", "fallback_attempted", "fallback_used", "fallback_reason", "fallback_route")

#: Extra calendar days fetched so that a decision session delayed by a long
#: exchange holiday still has a complete 364-day window.  Bars outside the
#: policy window are dropped by the collector and counted, never used.
FORECAST_FETCH_BUFFER_DAYS = 30


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


DEFAULT_HISTORY_WEEKS = 52


def _default_history_start(day: date, weeks: int = DEFAULT_HISTORY_WEEKS) -> date:
    """52 weeks is the research baseline, not a one-calendar-year approximation."""
    return day - timedelta(days=7 * weeks)


def _calendar(market: str, start: str, end: str) -> dict[str, Any] | None:
    try:
        return _library_calendar(market, start, end)
    except Exception:  # noqa: BLE001 - optional calendar failure is missing evidence, never synthetic closure
        return None


def _valid_calendar(value: Any, market: str) -> dict[str, Any] | None:
    try:
        calendar_sessions({"session_calendar": value}, market)
    except (KeyError, TypeError, ValueError):
        return None
    return value


def _call(provider: Any, name: str, **params: Any) -> dict[str, Any]:
    try:
        result = provider.call(name, **params)
    except Exception as exc:  # noqa: BLE001 - redact any provider failure; never expose strings that could contain a session credential
        category = getattr(exc, "code", None)
        if not isinstance(category, str) or not category:
            category = "NetworkError"
        return {"status": "error", "error": {"category": category, "exception_type": type(exc).__name__,
                                               "message": "provider call failed; details redacted"}}
    if isinstance(result, dict):
        return result
    return {"status": "error", "error": {"category": "InvalidResponse", "message": "provider returned a non-object"}}


def _block(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    provenance = envelope.get("provenance") if isinstance(envelope.get("provenance"), dict) else {}
    block = {}
    for key in _METADATA:
        if key in envelope:
            block[key] = envelope[key]
        elif key in data:
            block[key] = data[key]
        elif key in provenance:
            block[key] = provenance[key]
        elif key == "provider_library" and provenance.get("library"):
            block[key] = provenance["library"]
        elif key == "provider_version" and provenance.get("version"):
            block[key] = provenance["version"]
    block.update(actual_source="Yahoo Finance", gateway_envelope=envelope,
                 warnings=list(envelope.get("warnings") or []) if isinstance(envelope.get("warnings"), list) else [])
    if "fetched_at_utc" not in block:
        block["fetched_at_utc"] = utc_now()
    if provider_failed(envelope):
        block.update(status="failed", data=None)
    elif str(envelope.get("status", "")).lower() in {"empty", "unavailable"}:
        block.update(status="unavailable", data=None)
    else:
        block.update(status="partial" if envelope.get("status") == "partial" else "complete", data=data.copy())
    return block


def _closed_sessions(schedule: dict[str, Any], asof: datetime, local_day: str) -> list[str]:
    return [
        row["date"]
        for row in schedule["sessions"]
        if row["date"] <= local_day and parse_timestamp(row["close_at"]) <= asof
    ]


def _decision_session(
    bars: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
    asof: datetime,
    market: str,
) -> tuple[str | None, str]:
    """Latest completed exchange session at/before the as-of instant.

    With a verified calendar the answer depends only on the clock and the
    exchange close times.  Without one, the latest bar label is used and today's
    partial bar is excluded when the local wall clock has not reached the
    standard close; the result is explicitly marked unverified.
    """

    local = asof.astimezone(ZoneInfo(EXCHANGE_TIMEZONES[market]))
    local_day = local.date().isoformat()
    if schedule:
        closed = _closed_sessions(schedule, asof, local_day)
        return (closed[-1], "exchange_calendar") if closed else (None, "exchange_calendar")
    labels = sorted({row["date"] for row in bars if isinstance(row.get("date"), str) and row["date"] <= local_day})
    if not labels:
        return None, "bar_labels_only"
    latest = labels[-1]
    # Standard closes: 16:00 for both supported markets.  Only used on the
    # unverified path, which cannot publish a formal probability anyway.
    if latest == local_day and (local.hour, local.minute) < (16, 0):
        labels = labels[:-1]
        latest = labels[-1] if labels else None
    return latest, "bar_labels_only"


def _policy_window(
    request: dict[str, Any], bars: list[dict[str, Any]], schedule: dict[str, Any] | None,
    asof: datetime,
) -> dict[str, Any]:
    decision, basis = _decision_session(bars, schedule, asof, request["market"])
    if decision is None:
        return {
            "policy_version": POLICY_VERSION,
            "status": "no_completed_session",
            "decision_session": None,
            "calendar_verified": bool(schedule),
        }
    start, end = window_bounds(decision)
    declared = request.get("history_window") or {}
    if declared.get("start") or declared.get("end"):
        start, end = validate_declared_window(declared, decision)
    return {
        "policy_version": POLICY_VERSION,
        "status": "resolved",
        "decision_session": decision,
        "decision_session_basis": basis,
        "window_start_exclusive": start,
        "window_end_inclusive": end,
        "calendar_verified": bool(schedule),
        # Provenance, not just a boolean: an audit must be able to tell a real
        # exchange calendar from a weekday approximation.
        "calendar_source": ((schedule or {}).get("source") or "unverified_no_schedule"),
        "price_basis": PRICE_BASIS,
        "price_basis_source": PRICE_BASIS_SOURCE,
        "price_basis_note": PRICE_BASIS_NOTE,
    }


def _policy_coverage(policy: dict[str, Any], schedule: dict[str, Any] | None, kept: list[dict[str, Any]]) -> dict[str, Any]:
    observed = [row["date"] for row in kept]
    if policy.get("status") != "resolved":
        return {"coverage_status": "unavailable", "expected_sessions": None,
                "observed_sessions": len(observed), "missing_sessions": None,
                "missing_session_count": None, "unexpected_sessions": []}
    start, end, decision = policy["window_start_exclusive"], policy["window_end_inclusive"], policy["decision_session"]
    if not schedule:
        return {"coverage_status": "unverified", "expected_sessions": None,
                "observed_sessions": len(observed), "missing_sessions": None,
                "missing_session_count": None, "unexpected_sessions": [],
                "reason": "CALENDAR_UNVERIFIED"}
    expected = [row["date"] for row in schedule["sessions"] if start < row["date"] <= end]
    missing = [day for day in expected if day not in set(observed)]
    unexpected = [day for day in observed if day not in set(expected)]
    coverage_ok = (
        not missing
        and not unexpected
        and len(observed) == len(expected)
        and schedule["coverage_start"] <= start
        and schedule["coverage_end"] >= end
        and decision in set(observed)
    )
    return {
        "coverage_status": "complete" if coverage_ok else "incomplete",
        "expected_sessions": len(expected),
        "observed_sessions": len(observed),
        "missing_sessions": missing,
        "missing_session_count": len(missing),
        "unexpected_sessions": unexpected,
        "calendar_coverage_start": schedule.get("coverage_start"),
        "calendar_coverage_end": schedule.get("coverage_end"),
        "reason": None if coverage_ok else "INSUFFICIENT_HISTORY_COVERAGE",
    }


def _market(envelope: dict[str, Any], request: dict[str, Any], start: str, end: str,
            schedule: dict[str, Any] | None) -> dict[str, Any]:
    block = _block(envelope)
    if block["status"] in {"failed", "unavailable"}:
        return block
    data = block["data"]
    bars = data.get("bars")
    if not isinstance(bars, list):
        block.update(status="failed", data=None)
        block["warnings"].append("provider history lacks a normalized bars array")
        return block
    asof = parse_timestamp(request["asof"])
    schedule = schedule or _valid_calendar(envelope.get("session_calendar", data.get("session_calendar")), request["market"])
    sessions = {r["date"]: r for r in schedule["sessions"]} if schedule else {}
    if schedule:
        block["session_calendar"] = schedule
    if block.get("adjustment") == "provider":
        block["price_basis"] = PRICE_BASIS
        block["price_basis_source"] = PRICE_BASIS_SOURCE
        block["price_basis_note"] = PRICE_BASIS_NOTE
    elif block.get("adjustment") == "adjusted":
        block["price_basis"] = "split_and_dividend_adjusted"
        block["price_basis_source"] = "provider_adjusted_close_includes_dividends"
        block["price_basis_note"] = "not the policy basis; dividends are reinvested in this series"
    is_forecast = request.get("intent") == "forecast"
    policy: dict[str, Any] | None = None
    if is_forecast:
        policy = _policy_window(request, bars, schedule, asof)
        if policy.get("decision_session"):
            start, end = policy["window_start_exclusive"], policy["window_end_inclusive"]
        block["policy"] = {"policy_version": POLICY_VERSION, "decision_session": policy.get("decision_session"),
                           "window_start_exclusive": policy.get("window_start_exclusive"),
                           "window_end_inclusive": policy.get("window_end_inclusive"),
                           "calendar_verified": policy.get("calendar_verified")}
    kept, excluded = [], []
    invalid = False
    for row in bars:
        try:
            day = date.fromisoformat(row["date"]).isoformat()
        except (KeyError, TypeError, ValueError):
            invalid = True
            excluded.append({"reason": "invalid_session_date"})
            continue
        reason = None
        inside = (start < day <= end) if is_forecast else (start <= day <= end)
        if not inside:
            reason = "outside_requested_window"
        elif sessions.get(day) and parse_timestamp(sessions[day]["close_at"]) > asof:
            reason = "session_not_closed_at_asof"
        elif schedule and schedule["coverage_start"] <= day <= schedule["coverage_end"] and day not in sessions:
            reason = "not_an_exchange_session"
            invalid = True
        if reason:
            excluded.append({"date": day, "reason": reason})
        else:
            kept.append(dict(row))
    # Preserve order and duplicates: the existing freezer must reject invalid axes.
    data["bars"] = kept
    block["collection_provenance"] = {"requested_window": {"start": start, "end": end},
        "provider_end_exclusive": (date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        "excluded_bars": excluded, "asof": request["asof"],
        "archive_kind": "normalized provider result, not raw HTTP payload",
        "historical_vintage": "download-time revised series; not historical PIT vintage"}
    if not kept:
        block.update(status="unavailable", data=None, last_bar_closed=None)
        block["warnings"].append("no retained daily observations in requested window")
        if policy is not None:
            data["policy_window"] = {**policy, **_policy_coverage(policy, schedule, kept)}
            block["policy_status"] = "ineligible"
        return block
    first, last = kept[0]["date"], kept[-1]["date"]
    if policy is None:
        data["history_window"] = {"start": first, "end": last,
            "selection": "explicit user window" if request.get("history_window") else "one year from latest calendar-confirmed close; calendar uncertainty declared"}
    else:
        coverage = _policy_coverage(policy, schedule, kept)
        data["policy_window"] = {**policy, **coverage}
        data["history_window"] = {
            "start": policy.get("window_start_exclusive", start),
            "end": policy.get("window_end_inclusive", end),
            "selection": f"{POLICY_VERSION}: decision_session - {HISTORY_CALENDAR_DAYS} calendar days, left-open right-closed",
            "observed_first": first,
            "observed_last": last,
        }
        data["policy_window"]["dropped_out_of_window_count"] = sum(
            1 for item in excluded if item.get("reason") == "outside_requested_window"
        )
    data["frequency"] = block["frequency"] = data.get("frequency", block.get("frequency", "1d"))
    if block.get("source_symbol") is not None:
        data["symbol"] = block["source_symbol"]
    elif data.get("symbol") is not None:
        block["source_symbol"] = data["symbol"]
    block["last_bar_closed"] = (parse_timestamp(sessions[last]["close_at"]) <= asof) if last in sessions else None
    if policy is not None and not sessions:
        # Without a verified calendar the decision is taken from bar labels; the
        # clock rule already excluded an unclosed same-day bar, so the retained
        # last bar is the diagnostic decision close.  Formal probability is still
        # blocked by CALENDAR_UNVERIFIED downstream.
        block["last_bar_closed"] = last == policy.get("decision_session")
    # A bar label is genuine source time, not a quote update or a close timestamp.
    label = kept[-1].get("timestamp_utc", kept[-1].get("timestamp"))
    if label is not None:
        try:
            parse_timestamp(label)
            block["source_timestamp"] = label
            block["source_timestamp_kind"] = "bar_label_not_trade_time"
            block["collection_provenance"]["source_timestamp_kind"] = "bar_label_not_trade_time"
        except (TypeError, ValueError):
            block["warnings"].append("last bar source timestamp is invalid")
    elif excluded and block.get("source_timestamp"):
        # Do not reuse a source timestamp belonging to a removed/future bar.
        try:
            timestamp = parse_timestamp(block["source_timestamp"])
            local = timestamp.astimezone(ZoneInfo(EXCHANGE_TIMEZONES[request["market"]])).date().isoformat()
            if timestamp > asof or local != last:
                block.pop("source_timestamp", None)
        except (TypeError, ValueError):
            block.pop("source_timestamp", None)
    missing = [key for key in ("currency", "unit", "source_symbol", "source_timestamp", "adjustment") if not block.get(key)]
    if policy is None:
        if block["last_bar_closed"] is None:
            missing.append("calendar-backed closure")
        if schedule and (schedule["coverage_start"] > start or schedule["coverage_end"] < end):
            missing.append("calendar coverage of requested window")
        if sessions:
            expected = [day for day, row in sessions.items() if start <= day <= end and parse_timestamp(row["close_at"]) <= asof]
            if expected != [row["date"] for row in kept]:
                missing.append("requested exchange session coverage")
    else:
        coverage = data["policy_window"]
        if policy.get("status") != "resolved":
            missing.append("completed decision session")
        if coverage.get("coverage_status") != "complete":
            missing.append(f"complete 52-week session coverage ({coverage.get('reason') or 'incomplete'})")
        if block.get("price_basis") != PRICE_BASIS:
            missing.append("verified split-adjusted price basis")
        if policy.get("decision_session") and last != policy["decision_session"]:
            missing.append("decision session close bar")
        if block["last_bar_closed"] is not True:
            missing.append("calendar-backed closure")
        block["policy_status"] = "eligible" if not missing and not invalid else "ineligible"
    if missing or invalid:
        block["status"] = "partial"
        block["warnings"].append("Unverified/incomplete: " + ", ".join(missing or ["invalid session dates"]))
    return block


def _benchmark(provider: Any, symbol: str, start: str, end: str) -> dict[str, Any]:
    """Collect the explicitly declared comparison series.

    This never fails the target collection: a missing benchmark leaves the relative
    strength factor unavailable, which the momentum block reports as such. No fallback
    symbol is substituted, because a silently swapped benchmark would change the meaning
    of the relative-strength factor without disclosure.
    """
    block: dict[str, Any] = {
        "symbol": symbol,
        "selection": "declared",
        "status": "unavailable",
        "bars": [],
        "adjustment": None,
        "source": None,
    }
    envelope = _call(
        provider, "yahoo_history", symbol=symbol, start=start,
        end=(date.fromisoformat(end) + timedelta(days=1)).isoformat(), interval="1d", price_basis="provider",
    )
    if provider_failed(envelope):
        block.update(reason="provider_call_failed", error=envelope.get("error"))
        return block
    data = envelope.get("data") or {}
    raw = data.get("bars")
    if not isinstance(raw, list):
        block["reason"] = "missing_bars"
        return block
    bars = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row["date"])[:10]).isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        if start <= day <= end:
            bars.append(dict(row))
    if not bars:
        block["reason"] = "no_bars_in_window"
        return block
    block.update(
        bars=bars,
        adjustment=data.get("adjustment") or envelope.get("adjustment"),
        price_basis=(PRICE_BASIS if (data.get("adjustment") or envelope.get("adjustment")) == "provider"
                     else "split_and_dividend_adjusted"),
        source=envelope.get("source") or data.get("source"),
        status="partial" if envelope.get("status") == "partial" else "complete",
    )
    return block


def collect(request: dict[str, Any], *, domains: tuple[str, ...] | list[str] = ("market",),
            provider: Any = None, cache_dir: str | Path | None = None, timeout: float = 15,
            deadline: float = 120, request_budget: int = 30,
            now: datetime | None = None) -> dict[str, Any]:
    """Collect selected domains independently. Inject a provider for offline tests.

    News is intentionally absent: the Finnhub MCP tool is the only news source.
    Existing Finnhub successes are reused by the host.
    No provider-specific options are added to the research request.
    """
    normalized = normalize_request(request)
    if not domains or set(domains) - {"market"}:
        raise ValueError("domains must select market")
    if timeout <= 0 or deadline <= 0 or request_budget < 1:
        raise ValueError("network limits must be positive")
    normalized["asof"] = normalized["asof"] or (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    asof = parse_timestamp(normalized["asof"])
    local_day = asof.astimezone(ZoneInfo(EXCHANGE_TIMEZONES[normalized["market"]])).date()
    is_forecast = normalized["intent"] == "forecast"
    window = normalized.get("history_window") or {}
    end = min(date.fromisoformat(window.get("end", local_day.isoformat())), local_day)
    schedule = None
    if "market" in domains:
        if "start" in window:
            start_probe = window["start"]
        elif is_forecast:
            start_probe = (end - timedelta(days=HISTORY_CALENDAR_DAYS + FORECAST_FETCH_BUFFER_DAYS)).isoformat()
        else:
            start_probe = _default_history_start(end).isoformat()
        # Keep future sessions in the pinned schedule so the fifth-session target
        # can be resolved before its close exists.
        calendar_end = (local_day + timedelta(days=45)).isoformat()
        schedule = _valid_calendar(_calendar(normalized["market"], start_probe, calendar_end), normalized["market"])
        if "end" not in window and schedule:
            closed = [r["date"] for r in schedule["sessions"] if parse_timestamp(r["close_at"]) <= asof and r["date"] <= end.isoformat()]
            if closed:
                end = date.fromisoformat(closed[-1])
    if "start" in window:
        start = date.fromisoformat(window["start"])
    elif is_forecast:
        start = end - timedelta(days=HISTORY_CALENDAR_DAYS + FORECAST_FETCH_BUFFER_DAYS)
    else:
        start = date.fromisoformat(_default_history_start(end).isoformat())
    if start > end:
        raise ValueError("requested history starts after effective as-of end")
    # Library, cache and transport creation occur only on an actual collection call.
    owned_provider = provider is None
    if owned_provider:
        from yfinance_provider import YahooProvider
        provider = YahooProvider(cache_dir=cache_dir, timeout=timeout, deadline=deadline, max_requests=request_budget)
    aliases = (normalized.get("instrument") or {}).get("provider_symbols") or {}
    symbol = aliases.get("Yahoo Finance", normalized["ticker"])
    result = {}
    try:
        env = _call(provider, "yahoo_history", symbol=symbol, start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(), interval="1d", price_basis="provider")
        result["market"] = _market(env, normalized, start.isoformat(), end.isoformat(), schedule)
        benchmark_symbol = (normalized.get("instrument") or {}).get("benchmark")
        if benchmark_symbol:
            result["market"]["data"]["benchmark"] = _benchmark(
                provider, benchmark_symbol, start.isoformat(), end.isoformat()
            )
            policy_window = (result["market"].get("data") or {}).get("policy_window") or {}
            if normalized["intent"] == "forecast" and policy_window.get("status") == "resolved":
                low = policy_window["window_start_exclusive"]
                high = policy_window["window_end_inclusive"]
                benchmark = result["market"]["data"]["benchmark"]
                benchmark["bars"] = [row for row in benchmark.get("bars") or [] if low < row["date"] <= high]
                benchmark["window_start_exclusive"] = low
                benchmark["window_end_inclusive"] = high
    finally:
        if owned_provider:
            provider.close()
    return sanitize_data(_json_safe(result))


def write_responses(responses: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    # Serialize before opening, avoiding a half-written destination on invalid JSON.
    text = json.dumps(sanitize_data(_json_safe(responses)), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--domains", nargs="+", choices=("market",), default=["market"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--request-budget", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output already exists")
        request = json.loads(args.request.read_text(encoding="utf-8"))
        responses = collect(request, domains=args.domains, cache_dir=args.cache_dir, timeout=args.timeout,
                            deadline=args.deadline, request_budget=args.request_budget)
        write_responses(responses, args.output)
        return 1 if all(b["status"] == "failed" for b in responses.values()) or responses.get("market", {}).get("status") == "failed" else 0
    except (OSError, ValueError, ImportError) as exc:
        payload = {"status": "error", "error": {"category": "InvalidParameters" if isinstance(exc, ValueError) else "DependencyUnavailable" if isinstance(exc, ImportError) else "InvalidResponse", "exception_type": type(exc).__name__}}
        if isinstance(exc, ValueError) and getattr(exc, "code", None):
            payload["error"]["code"] = exc.code
        print(json.dumps(payload, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
