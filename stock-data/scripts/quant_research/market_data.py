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


_MONDAY_EPOCH_ORDINAL = 719160  # date(1969, 12, 29) is a Monday; a non-Monday epoch
# would collide adjacent weekdays in the formula below.


def business_day_ordinal(day: str) -> int:
    """Deterministic weekday count from a fixed epoch, derived from the date alone.

    A sliding 52-week window cannot supply a global session counter, and anchoring the
    sampling phase on the window's first bar makes the phase move with the window. This
    ordinal depends only on the calendar date, so the same history keeps the same phase
    no matter where the window starts. It counts Monday-Friday days and therefore does
    not know about exchange holidays: two consecutive sessions normally differ by one,
    occasionally by two. Callers must still enforce the session gap themselves.
    """
    from datetime import date

    offset = date.fromisoformat(day).toordinal() - _MONDAY_EPOCH_ORDINAL
    weeks, remainder = divmod(offset, 7)
    return weeks * 5 + min(remainder, 5)


def anchored_non_overlapping(
    dates: list[str],
    indices: list[int],
    horizon: int,
    *,
    phase: int = 0,
) -> tuple[list[int], dict[str, Any]]:
    """Non-overlapping sample with a window-independent phase.

    ``non_overlapping`` starts from whatever index the caller passes, so a window that
    slides by one session replaces every sampled date. This selects candidates by an
    absolute weekday ordinal instead, then walks them in session order and drops any
    candidate that would overlap the previous kept label. Membership of a historical
    date therefore no longer depends on where the window happens to begin.

    The weekday step is ``horizon + 2`` rather than ``horizon + 1``: a step of
    ``horizon + 1`` weekdays collapses to only ``horizon`` sessions whenever a single
    exchange holiday falls inside the span, which forces those candidates to be dropped
    and roughly halves the surviving sample. One weekday of slack absorbs a holiday and
    keeps the session gap at ``horizon + 1`` or more, verified on real 250-bar data.
    """
    required_gap = max(1, int(horizon) + 1)
    weekday_step = max(2, int(horizon) + 2)
    usable = sorted(index for index in indices if 0 <= index < len(dates))
    candidates = [index for index in usable if business_day_ordinal(dates[index]) % weekday_step == phase]
    kept: list[int] = []
    dropped_for_overlap = 0
    last_end = -1
    for index in candidates:
        if index > last_end:
            kept.append(index)
            last_end = index + int(horizon)
        else:
            dropped_for_overlap += 1
    provenance = {
        "method": "absolute_weekday_phase_then_session_gap",
        "phase": phase,
        "weekday_step": weekday_step,
        "required_session_gap": required_gap,
        "candidates": len(candidates),
        "kept": len(kept),
        "dropped_for_overlap": dropped_for_overlap,
        "weekday_ordinal_ignores_exchange_holidays": True,
        "window_independent_phase": True,
    }
    if not kept:
        # Documented deterministic fallback: no candidate cleared the phase filter, so
        # the legacy spacing is used and the caller must lower its evidence grade.
        fallback = non_overlapping(usable, int(horizon))
        provenance.update(method="legacy_spacing_fallback", kept=len(fallback),
                          window_independent_phase=False, fallback_reason="no_phase_candidates")
        return fallback, provenance
    return kept, provenance
