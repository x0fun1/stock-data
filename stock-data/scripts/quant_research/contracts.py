"""Request and researcher contracts. This module intentionally has no provider access."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

HORIZONS = {"1D": 1, "5D": 5, "20D": 20}
RESEARCHERS = ("quant", "factor", "ml", "factor_backtest")
RESEARCHER_STATUSES = {"success", "partial", "failed", "insufficient_data", "invalid"}


def normalize_request(value: dict[str, Any]) -> dict[str, Any]:
    ticker = str(value.get("ticker", "")).strip().upper()
    market = str(value.get("market", "US")).strip().upper()
    horizon = str(value.get("horizon", "5D")).strip().upper()
    mode = str(value.get("mode", "standard")).strip().lower()
    if not ticker or any(ch.isspace() for ch in ticker):
        raise ValueError("request.ticker must be a non-empty ticker without spaces")
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
    return {"ticker": ticker, "market": market, "horizon": horizon, "asof": asof, "mode": mode}


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
        "expected_return": expected_return,
        "confidence": confidence,
        "probability_source": probability_source,
        "evidence": evidence or {"positive": [], "negative": []},
        "validation": validation or {},
        "data_used": data_used or [],
        "warnings": warnings or [],
        "status": status,
    }
