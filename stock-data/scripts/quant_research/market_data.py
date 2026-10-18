"""Neutral access to normalized OHLCV in a frozen snapshot."""

from __future__ import annotations

import math
from typing import Any


def market_bars(block: dict[str, Any]) -> list[dict[str, Any]]:
    if not block.get("_data_validation", {}).get("forecast_eligible", False):
        raise ValueError("prediction eligibility is blocked by fatal data gates; non-fatal calendar/adjustment/audit gaps are reported as degraded")
    bars = block.get("data", {}).get("bars")
    if not isinstance(bars, list):
        raise ValueError("market data must contain normalized daily bars")  # noqa: TRY004 - repo-wide validation contract: input errors raise ValueError
    result: list[dict[str, Any]] = []
    for bar in bars:
        row = {"date": str(bar["date"])[:10]}
        for key in ("open", "high", "low", "close", "volume"):
            value = float(bar[key])
            if not math.isfinite(value):
                raise ValueError(f"non-finite {key} in market bar")
            row[key] = value
        result.append(row)
    return result


def simple_return(start: float, end: float) -> float:
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0:
        raise ValueError("simple return requires finite positive start and finite end")
    return end / start - 1.0


def adjusted_close_series(block: dict[str, Any]) -> tuple[list[str], list[float], dict[str, Any]]:
    """Return ``(dates, prices, provenance)`` on a single, explicitly recorded basis.

    The composite momentum feature must compare like with like: a series that mixes
    adjusted and unadjusted prints, or that silently changes basis between the target
    and its benchmark, would corrupt every ratio it feeds. The price basis actually
    used is therefore returned instead of being assumed by the caller.
    """
    data = block.get("data") or {}
    bars = data.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("market data must contain normalized daily bars")
    declared = str(block.get("adjustment") or "").strip().lower()
    # Decide the basis over the whole series first: a single missing adj_close bar must
    # not silently switch the earlier bars onto a different basis mid-series.
    all_adjusted = all(
        isinstance(bar.get("adj_close"), (int, float))
        and math.isfinite(float(bar["adj_close"]))
        and float(bar["adj_close"]) > 0
        for bar in bars
    )
    dates = []
    values = []
    for bar in bars:
        date = str(bar["date"])[:10]
        close = bar.get("close")
        if not (isinstance(close, (int, float)) and math.isfinite(float(close))):
            raise ValueError(f"non-finite close on {date}")
        dates.append(date)
        values.append(float(bar["adj_close"]) if all_adjusted else float(close))
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate trading dates in market bars")
    if dates != sorted(dates):
        raise ValueError("market bars must be sorted ascending by date")
    if any(value <= 0 for value in values):
        raise ValueError("momentum requires strictly positive prices")
    if all_adjusted:
        basis_status = "verified_adjusted" if declared in {"adjusted", "adjust"} else "declared_adjusted"
        price_field = "adjusted_close"
    elif declared in {"adjusted", "adjust"}:
        # Legacy route: close is already split/dividend adjusted even though a separate
        # adj_close column is absent, so ratios stay internally consistent.
        basis_status = "declared_adjusted"
        price_field = "close_declared_adjusted"
    else:
        basis_status = "unverified"
        price_field = "close"
    provenance = {
        "price_field": price_field,
        "basis_status": basis_status,
        "declared_adjustment": declared or None,
        "sessions": len(dates),
        "first_date": dates[0],
        "last_date": dates[-1],
    }
    return dates, values, provenance


def forward_returns(closes: list[float], horizon: int) -> list[float | None]:
    """Map session t to close[t+h]/close[t]-1; the final horizon rows are unlabeled."""
    result: list[float | None] = [None] * len(closes)
    for index in range(max(0, len(closes) - horizon)):
        result[index] = simple_return(closes[index], closes[index + horizon])
    return result


def non_overlapping(indices: list[int], horizon: int) -> list[int]:
    kept: list[int] = []
    last_end = -1
    for index in sorted(indices):
        if index > last_end:
            kept.append(index)
            last_end = index + horizon
    return kept
