"""Explicit Yahoo market/news collection; the research pipeline remains offline.

Only normalized provider data is projected. Calendar evidence is optional, and
unknown provenance/closure is never replaced with a guessed symbol or clock.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_research.contracts import normalize_request, parse_timestamp, utc_now
from quant_research.data_checks import EXCHANGE_TIMEZONES, _library_calendar, calendar_sessions
from quant_research.security import provider_failed, sanitize_data


_METADATA = ("currency", "unit", "frequency", "source_symbol", "source_timestamp", "source_timestamp_kind",
             "fetched_at_utc", "adjustment", "adjustment_evidence", "provider_library",
             "provider_version", "fallback_used", "fallback_reason")


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


def _year_back(day: date, years: int = 3) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _calendar(market: str, start: str, end: str) -> dict[str, Any] | None:
    try:
        return _library_calendar(market, start, end)
    except Exception:
        # Optional calendar failure is missing evidence, never synthetic closure.
        return None


def _valid_calendar(value: Any, market: str) -> dict[str, Any] | None:
    try:
        calendar_sessions({"session_calendar": value}, market)
        return value
    except (KeyError, TypeError, ValueError):
        return None


def _call(provider: Any, name: str, **params: Any) -> dict[str, Any]:
    try:
        result = provider.call(name, **params)
        if isinstance(result, dict):
            return result
        return {"status": "error", "error": {"category": "InvalidResponse", "message": "provider returned a non-object"}}
    except Exception as exc:
        # Never expose an exception string that could contain a session credential.
        category = getattr(exc, "code", None)
        if not isinstance(category, str) or not category:
            category = "NetworkError"
        return {"status": "error", "error": {"category": category, "exception_type": type(exc).__name__,
                                               "message": "provider call failed; details redacted"}}


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
        if not start <= day <= end:
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
        return block
    first, last = kept[0]["date"], kept[-1]["date"]
    data["history_window"] = {"start": first, "end": last,
        "selection": "explicit user window" if request.get("history_window") else "three years from latest calendar-confirmed close; calendar uncertainty declared"}
    data["frequency"] = block["frequency"] = data.get("frequency", block.get("frequency", "1d"))
    if block.get("source_symbol") is not None:
        data["symbol"] = block["source_symbol"]
    elif data.get("symbol") is not None:
        block["source_symbol"] = data["symbol"]
    block["last_bar_closed"] = (parse_timestamp(sessions[last]["close_at"]) <= asof) if last in sessions else None
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
    if block["last_bar_closed"] is None:
        missing.append("calendar-backed closure")
    if schedule and (schedule["coverage_start"] > start or schedule["coverage_end"] < end):
        missing.append("calendar coverage of requested window")
    if block.get("adjustment") != "adjusted":
        missing.append("verified adjusted price basis")
    if sessions:
        expected = [day for day, row in sessions.items() if start <= day <= end and parse_timestamp(row["close_at"]) <= asof]
        if expected != [row["date"] for row in kept]:
            missing.append("requested exchange session coverage")
    if missing or invalid:
        block["status"] = "partial"
        block["warnings"].append("Unverified/incomplete: " + ", ".join(missing or ["invalid session dates"]))
    return block


def _news(envelope: dict[str, Any]) -> dict[str, Any]:
    block = _block(envelope)
    if block["status"] in {"failed", "unavailable"}:
        return block
    articles = block["data"].get("articles")
    if not isinstance(articles, list):
        block.update(status="failed", data=None)
        block["warnings"].append("provider news lacks a normalized articles array")
    elif not articles:
        block.update(status="unavailable", data=None)
        block["warnings"].append("no articles returned; historical coverage is not established")
    else:
        block["status"] = "partial"
        block["warnings"].append("Yahoo short feed only; historical news coverage/full text not guaranteed. Existing NewsInput performs PIT and ticker filtering.")
    return block


def collect(request: dict[str, Any], *, domains: tuple[str, ...] | list[str] = ("market", "news"),
            provider: Any = None, cache_dir: str | Path | None = None, timeout: float = 15,
            deadline: float = 120, request_budget: int = 30, news_count: int = 10,
            news_tab: str = "news", now: datetime | None = None) -> dict[str, Any]:
    """Collect selected domains independently. Inject a provider for offline tests.

    Existing Finnhub successes are reused by the host: select only missing domains.
    No provider-specific options are added to the research request.
    """
    normalized = normalize_request(request)
    if not domains or set(domains) - {"market", "news"}:
        raise ValueError("domains must select market and/or news")
    if not 1 <= news_count <= 100 or news_tab not in {"news", "all", "press releases"}:
        raise ValueError("invalid bounded news count/tab")
    if timeout <= 0 or deadline <= 0 or request_budget < 1:
        raise ValueError("network limits must be positive")
    normalized["asof"] = normalized["asof"] or (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    asof = parse_timestamp(normalized["asof"])
    local_day = asof.astimezone(ZoneInfo(EXCHANGE_TIMEZONES[normalized["market"]])).date()
    window = normalized.get("history_window") or {}
    end = min(date.fromisoformat(window.get("end", local_day.isoformat())), local_day)
    schedule = None
    if "market" in domains:
        start_probe = window.get("start", _year_back(end).isoformat())
        schedule = _valid_calendar(_calendar(normalized["market"], start_probe, local_day.isoformat()), normalized["market"])
        if "end" not in window and schedule:
            closed = [r["date"] for r in schedule["sessions"] if parse_timestamp(r["close_at"]) <= asof and r["date"] <= end.isoformat()]
            if closed:
                end = date.fromisoformat(closed[-1])
    start = date.fromisoformat(window.get("start", _year_back(end).isoformat()))
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
        if "market" in domains:
            env = _call(provider, "yahoo_history", symbol=symbol, start=start.isoformat(),
                        end=(end + timedelta(days=1)).isoformat(), interval="1d", price_basis="adjusted")
            result["market"] = _market(env, normalized, start.isoformat(), end.isoformat(), schedule)
        if "news" in domains:
            result["news"] = _news(_call(provider, "yahoo_news", symbol=symbol, count=news_count, tab=news_tab))
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
    parser.add_argument("--domains", nargs="+", choices=("market", "news"), default=["market", "news"])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--request-budget", type=int, default=30)
    parser.add_argument("--news-count", type=int, default=10)
    parser.add_argument("--news-tab", choices=("news", "all", "press releases"), default="news")
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("output already exists")
        request = json.loads(args.request.read_text(encoding="utf-8"))
        responses = collect(request, domains=args.domains, cache_dir=args.cache_dir, timeout=args.timeout,
                            deadline=args.deadline, request_budget=args.request_budget, news_count=args.news_count, news_tab=args.news_tab)
        write_responses(responses, args.output)
        return 1 if all(b["status"] == "failed" for b in responses.values()) or responses.get("market", {}).get("status") == "failed" else 0
    except (OSError, ValueError, ImportError) as exc:
        print(json.dumps({"status": "error", "error": {"category": "InvalidParameters" if isinstance(exc, ValueError) else "DependencyUnavailable" if isinstance(exc, ImportError) else "InvalidResponse", "exception_type": type(exc).__name__}}, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
