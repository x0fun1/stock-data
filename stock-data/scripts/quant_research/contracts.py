"""Request and researcher contracts. This module intentionally has no provider access."""

from __future__ import annotations

from datetime import date, datetime, timezone
import math
import re
from typing import Any

HORIZONS = {"1D": 1, "5D": 5, "20D": 20}
RESEARCHERS = ("quant", "factor", "ml", "factor_backtest")
RESEARCHER_STATUSES = {"success", "partial", "failed", "insufficient_data", "invalid"}


def normalize_request(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    allowed = {"ticker", "market", "horizon", "asof", "mode", "intent", "asset_type", "instrument", "history_window", "history_start", "history_end"}
    if set(value) - allowed:
        raise ValueError("unsupported request fields: " + ", ".join(sorted(set(value) - allowed)))
    if not isinstance(value.get("ticker"), str):
        raise ValueError("request.ticker must be a string")
    ticker = value["ticker"].strip().upper()
    market = str(value.get("market", "US")).strip().upper()
    horizon = str(value.get("horizon", "5D")).strip().upper()
    mode = str(value.get("mode", "standard")).strip().lower()
    if not re.fullmatch(r"[A-Z0-9.^][A-Z0-9._^-]{0,31}", ticker):
        raise ValueError("request.ticker must use supported stock/index symbol characters")
    if market not in {"US", "HK"}:
        raise ValueError("request.market must be US or HK")
    if horizon not in HORIZONS:
        raise ValueError("request.horizon must be one of 1D, 5D, 20D")
    if mode not in {"standard", "strict"}:
        raise ValueError("request.mode must be standard or strict")
    asof = value.get("asof")
    if asof not in (None, ""):
        parsed = parse_timestamp(asof)
        asof = parsed.isoformat().replace("+00:00", "Z")
    else:
        asof = None
    result = {"ticker": ticker, "market": market, "horizon": horizon, "asof": asof, "mode": mode}
    for field, default, choices in (("intent", "forecast", {"forecast", "descriptive", "factor_research"}), ("asset_type", "unknown", {"stock", "etf", "index", "unknown"})):
        item = value.get(field, default)
        if not isinstance(item, str) or item not in choices:
            raise ValueError(f"request.{field} must be one of {sorted(choices)}")
        result[field] = item
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
        if not isinstance(instrument, dict) or set(instrument) - {"resolved_ticker", "provider_symbols", "currency", "unit", "source"}:
            raise ValueError("request.instrument has unsupported fields")
        if instrument.get("resolved_ticker", ticker) != ticker:
            raise ValueError("resolved instrument differs from request ticker")
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


def researcher_result(
    researcher_id: str,
    ticker: str,
    horizon: str,
    snapshot_id: str,
    *,
    status: str,
    result_role: str = "forecast",
    prob_up: float | None = None,
    expected_return: float | None = None,
    confidence: float | None = None,
    evidence: dict[str, list[dict[str, Any]]] | None = None,
    validation: dict[str, Any] | None = None,
    data_used: list[str] | None = None,
    warnings: list[str] | None = None,
    probability_source: str | None = None,
    raw_probability: float | None = None,
    calibrated_probability: float | None = None,
) -> dict[str, Any]:
    if researcher_id not in RESEARCHERS:
        raise ValueError(f"unknown researcher_id: {researcher_id}")
    if status not in RESEARCHER_STATUSES:
        raise ValueError(f"unknown researcher status: {status}")
    if result_role not in {"forecast", "diagnostic"}:
        raise ValueError(f"unknown result_role: {result_role}")
    if prob_up is not None and not 0 <= prob_up <= 1:
        raise ValueError("prob_up must be between 0 and 1")
    if prob_up is None and probability_source is not None:
        raise ValueError("probability_source requires a numeric probability")
    raw_probability = prob_up if raw_probability is None else raw_probability
    for name, value in (("raw_probability", raw_probability), ("calibrated_probability", calibrated_probability)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError(f"{name} must be a finite probability between 0 and 1")
    if prob_up is None and (raw_probability is not None or calibrated_probability is not None):
        raise ValueError("raw/calibrated probabilities require a reportable path probability")
    direction = "unavailable"
    if prob_up is not None:
        direction = "bullish" if prob_up > 0.5 else "bearish" if prob_up < 0.5 else "neutral"
    return {
        "researcher_id": researcher_id,
        "result_role": result_role,
        "ticker": ticker,
        "horizon": horizon,
        "snapshot_id": snapshot_id,
        "direction": direction,
        "prob_up": prob_up,
        "prob_down": (1 - prob_up) if prob_up is not None else None,
        "raw_probability": raw_probability,
        "calibrated_probability": calibrated_probability,
        "reportable_probability": prob_up,
        "expected_return": expected_return,
        "confidence": confidence,
        "probability_source": probability_source,
        "evidence": evidence or {"positive": [], "negative": []},
        "validation": validation or {},
        "data_used": data_used or [],
        "warnings": warnings or [],
        "status": status,
    }
