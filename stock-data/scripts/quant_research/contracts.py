"""Request contracts. This module intentionally has no provider access."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from .forecast_policy import PolicyError, policy_metadata

#: News windows supported by the runtime.  The old forecast horizons are
#: intentionally absent: they are rejected with ``FORECAST_REMOVED``.
HORIZONS = {"30D": 30, "60D": 60}
LEGACY_FORECAST_HORIZONS = frozenset({"1D", "5D", "20D"})
INTENTS = frozenset({"news", "descriptive"})


def _forecast_removed() -> PolicyError:
    return PolicyError(
        "FORECAST_REMOVED",
        "Direction prediction was removed in v4. Use the news-analysis workflow "
        "(references/news-analysis.md); this runtime reports news events only and "
        "never emits an up/down probability or directional conclusion.",
    )


def normalize_request(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")  # noqa: TRY004 - repo-wide validation contract: input errors raise ValueError
    allowed = {"ticker", "market", "horizon", "asof", "mode", "intent", "asset_type", "instrument", "history_window", "history_start", "history_end", "policy"}
    if set(value) - allowed:
        raise ValueError("unsupported request fields: " + ", ".join(sorted(set(value) - allowed)))
    if not isinstance(value.get("ticker"), str):
        raise ValueError("request.ticker must be a string")  # noqa: TRY004 - repo-wide validation contract: input errors raise ValueError
    ticker = value["ticker"].strip().upper()
    market = str(value.get("market", "US")).strip().upper()
    horizon = str(value.get("horizon", "60D")).strip().upper()
    mode = str(value.get("mode", "standard")).strip().lower()
    if not re.fullmatch(r"[A-Z0-9.^][A-Z0-9._^-]{0,31}", ticker):
        raise ValueError("request.ticker must use supported stock/index symbol characters")
    if market not in {"US", "HK"}:
        raise ValueError("request.market must be US or HK")
    if "intent" in value and value.get("intent") == "forecast":
        raise _forecast_removed()
    if "horizon" in value and horizon in LEGACY_FORECAST_HORIZONS:
        raise _forecast_removed()
    if horizon not in HORIZONS:
        raise ValueError("request.horizon must be one of 30D, 60D")
    if mode not in {"standard", "strict"}:
        raise ValueError("request.mode must be standard or strict")
    asof = value.get("asof")
    if asof not in (None, ""):
        parsed = parse_timestamp(asof)
        asof = parsed.isoformat().replace("+00:00", "Z")
    else:
        asof = None
    result = {"ticker": ticker, "market": market, "horizon": horizon, "asof": asof, "mode": mode}
    for field, default, choices in (("intent", "news", INTENTS), ("asset_type", "unknown", {"stock", "etf", "index", "unknown"})):
        item = value.get(field, default)
        if not isinstance(item, str) or item not in choices:
            raise ValueError(f"request.{field} must be one of {sorted(choices)}")
        result[field] = item
    result["policy"] = policy_metadata()
    window = value.get("history_window", {})
    if not isinstance(window, dict) or set(window) - {"start", "end", "selection"}:
        raise ValueError("request.history_window must contain only start/end/selection")
    window = dict(window)
    for field in ("start", "end"):
        item = value.get("history_" + field, window.get(field))
        if item is not None:
            if field in window and window[field] != item:
                raise ValueError("conflicting request history window")
            window[field] = date.fromisoformat(item).isoformat()
    if window.get("start") and window.get("end") and window["start"] > window["end"]:
        raise ValueError("request history window is reversed")
    if window:
        result["history_window"] = window
    if "instrument" in value:
        instrument = value["instrument"]
        if not isinstance(instrument, dict) or set(instrument) - {"resolved_ticker", "provider_symbols", "currency", "unit", "source", "benchmark"}:
            raise ValueError("request.instrument has unsupported fields")
        if instrument.get("resolved_ticker", ticker) != ticker:
            raise ValueError("resolved instrument differs from request ticker")
        benchmark = instrument.get("benchmark")
        if benchmark is not None:
            if not isinstance(benchmark, str) or not re.fullmatch(r"[A-Za-z0-9.^][A-Za-z0-9._^-]{0,31}", benchmark.strip()):
                raise ValueError("instrument.benchmark must use supported symbol characters")
            if benchmark.strip().upper() == ticker:
                raise ValueError("instrument.benchmark must differ from the request ticker")
            instrument = {**instrument, "benchmark": benchmark.strip().upper()}
        aliases = instrument.get("provider_symbols", {})
        if not isinstance(aliases, dict) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9.^][A-Za-z0-9._^-]{0,31}", v) for v in aliases.values()):
            raise ValueError("instrument provider_symbols must be validated symbol strings")
        if aliases and not instrument.get("source"):
            raise ValueError("instrument aliases require source provenance")
        result["instrument"] = dict(instrument)
    return result


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
