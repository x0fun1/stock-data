"""Neutral access to normalized OHLCV in a frozen snapshot."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date
from typing import Any

from .forecast_policy import (
    FORECAST_HORIZON_SESSIONS,
    PRICE_BASIS,
    PRICE_BASIS_SOURCE,
    SAMPLING_ANCHOR_SESSION,
    SAMPLING_MODULUS,
    TARGET_VERSION,
    is_sampled_ordinal,
    session_ordinal,
)


def market_bars(block: dict[str, Any], *, diagnostic: bool = False) -> list[dict[str, Any]]:
    """Normalize OHLCV, refusing a blocked snapshot unless it is read for diagnosis.

    ``blocked`` means no probability may be produced, not that the frozen bars are
    unreadable.  A diagnostic reader that has already decided not to compute a
    forecast can pass ``diagnostic=True`` and still inspect the series; the
    flag is never used on a path that publishes a probability.
    """

    if not block.get("_data_validation", {}).get("forecast_eligible", False) and not diagnostic:
        raise ValueError(
            "prediction eligibility is blocked by fatal data gates; pass diagnostic=True only for "
            "read-only diagnostics that will not produce a probability"
        )
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


def prediction_price_series(block: dict[str, Any]) -> tuple[list[str], list[float], dict[str, Any]]:
    """Canonical price series for every production, shadow and audit prediction.

    The policy target is the provider's split-adjusted close without cash-dividend
    reinvestment.  We therefore use ``close`` directly and refuse to substitute a
    dividend-adjusted ``adj_close`` column: doing so would silently change the
    target definition while keeping the same ledger group.
    """

    data = block.get("data") or {}
    bars = data.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("market data must contain normalized daily bars")
    declared = block.get("price_basis")
    if declared != PRICE_BASIS:
        raise ValueError(
            f"price basis {declared!r} is not the policy basis {PRICE_BASIS!r}; "
            "a dividend-reinvested series cannot serve the 5D price-direction target"
        )
    dates: list[str] = []
    values: list[float] = []
    for bar in bars:
        day = str(bar["date"])[:10]
        close = bar.get("close")
        if not (isinstance(close, (int, float)) and math.isfinite(float(close)) and float(close) > 0):
            raise ValueError(f"non-finite or non-positive close on {day}")
        dates.append(day)
        values.append(float(close))
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate trading dates in market bars")
    if dates != sorted(dates):
        raise ValueError("market bars must be sorted ascending by date")
    provenance = {
        "price_field": "close",
        "price_basis": PRICE_BASIS,
        "price_basis_source": block.get("price_basis_source") or PRICE_BASIS_SOURCE,
        # A provider declaration is not independent evidence: the snapshot records
        # the claim and its source, and the audit reports it as declared, never as
        # verified against a corporate-action ledger.
        "price_basis_evidence": "declared_by_provider",
        "price_basis_evidence_status": "declared_not_independently_verified",
        "dividends_reinvested": False,
        "target_version": TARGET_VERSION,
        "horizon_sessions": FORECAST_HORIZON_SESSIONS,
        "sessions": len(dates),
        "first_date": dates[0],
        "last_date": dates[-1],
    }
    return dates, values, provenance


def calendar_session_list(block: dict[str, Any]) -> list[date] | None:
    calendar = (block.get("data") or {}).get("session_calendar") or block.get("session_calendar")
    if not isinstance(calendar, dict) or not isinstance(calendar.get("sessions"), list):
        return None
    return [date.fromisoformat(row["date"]) for row in calendar["sessions"]]


def fixed_phase_sample(dates: list[str], sessions: list[date], *, modulus: int = SAMPLING_MODULUS) -> tuple[list[str], dict[str, Any]]:
    """Select the fixed exchange-session phase (anchor 2000-01-03, ordinal % 6 == 0).

    The phase is defined on the exchange calendar, not on the snapshot's left
    edge, so the same historical date is sampled regardless of where a rolling
    52-week window starts.  Sampling is a policy, not a claim that every 6th
    session is an independent draw.
    """

    ordinal_map = {}
    for day in dates:
        try:
            value = session_ordinal(sessions, date.fromisoformat(day))
        except ValueError:
            value = None
        ordinal_map[day] = value
    anchored = any(value is not None for value in ordinal_map.values())
    sampled = [day for day in dates if is_sampled_ordinal(ordinal_map.get(day))]
    provenance = {
        "method": "fixed_exchange_session_phase",
        "status": "anchored" if anchored else "unanchored",
        "anchor_session": SAMPLING_ANCHOR_SESSION.isoformat(),
        "modulus": modulus,
        "phase": 0,
        "session_gap": modulus,
        "sample_size": len(sampled),
        "dates_without_session_ordinal": sum(1 for value in ordinal_map.values() if value is None),
        "phase_window_independent": anchored,
        "role": "throttle_only_not_qualification",
        "session_gap_is_sampling_policy_not_label_independence": True,
    }
    if not anchored:
        # A rolling 52-week calendar cannot supply a global session counter.  Use
        # every row instead of inventing a phase from the window's left edge.
        provenance["fallback"] = "all_rows_when_phase_unanchored"
        return list(dates), provenance
    return sampled, provenance


def evaluation_sample(predictions: Iterable[dict[str, Any]], sessions: list[date] | None) -> dict[str, Any]:
    """Report total/matured/non-overlapping counts and coverage for evaluation."""

    rows = list(predictions)
    matured = [row for row in rows if row.get("status") == "matured" and isinstance(row.get("probability"), (int, float))]
    if not sessions:
        return {"sampled_predictions": [], "total_predictions": len(rows), "matured_predictions": len(matured),
                "nonoverlap_predictions": 0, "coverage_ratio": None, "sampling": {"method": "unavailable"}}
    ordinals = {day.isoformat(): session_ordinal(sessions, day) for day in sessions}
    decision_dates = sorted({str(row["decision_session"]) for row in rows if row.get("decision_session") in ordinals})
    if not decision_dates:
        return {"sampled_predictions": [], "total_predictions": len(rows), "matured_predictions": len(matured),
                "nonoverlap_predictions": 0, "coverage_ratio": None, "sampling": {"method": "unavailable"}}
    first, last = decision_dates[0], decision_dates[-1]
    anchored = any(value is not None for value in ordinals.values())
    expected = [
        day for day in sessions
        if first <= day.isoformat() <= last and is_sampled_ordinal(ordinals[day.isoformat()])
    ]
    if anchored:
        by_date = {str(row["decision_session"]): row for row in matured}
        sampled_rows = [by_date[day.isoformat()] for day in expected if day.isoformat() in by_date]
    else:
        # Phase is a throttle, not a claim: without the anchor every matured row
        # is evaluated and the provenance says so.
        sampled_rows = list(matured)
    nonoverlap = len(non_overlapping(
        [int(row["decision_index"]) for row in matured if isinstance(row.get("decision_index"), int)],
        FORECAST_HORIZON_SESSIONS,
    ))
    return {
        "sampled_predictions": sampled_rows,
        "total_predictions": len(rows),
        "matured_predictions": len(matured),
        "nonoverlap_predictions": nonoverlap,
        "expected_sampled_sessions": len(expected),
        "coverage_ratio": (round(len(sampled_rows) / len(expected), 4) if expected else None) if anchored else None,
        "sampling": {
            "method": "fixed_exchange_session_phase",
            "status": "anchored" if anchored else "unanchored",
            "anchor_session": SAMPLING_ANCHOR_SESSION.isoformat(),
            "modulus": SAMPLING_MODULUS,
            "phase": 0,
            "session_gap": SAMPLING_MODULUS,
            "session_gap_is_sampling_policy_not_label_independence": True,
            "phase_window_independent": anchored,
            "role": "throttle_only_not_qualification",
        },
    }


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
    """Non-overlapping sample with a window-independent calendar candidate phase.

    ``non_overlapping`` starts from whatever index the caller passes, so a window that
    slides by one session replaces every sampled date. This selects candidates by an
    absolute weekday ordinal instead, then walks them in session order and drops any
    candidate that would overlap the previous kept label. Only the candidate phase is
    window-independent, not the final kept membership: the greedy gap filter restarts
    at the left boundary. Around holiday collisions, removing an earlier candidate can
    admit a later historical candidate that was previously rejected.

    The weekday step is ``horizon + 2`` rather than ``horizon + 1``: a step of
    ``horizon + 1`` weekdays collapses to only ``horizon`` sessions whenever a single
    exchange holiday falls inside the span, which forces those candidates to be dropped
    and roughly halves the surviving sample. One weekday of slack absorbs a holiday and
    usually avoids that collision. Multiple holidays can still cause collisions; the
    greedy gap filter always enforces a session gap of at least ``horizon + 1``.
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
        "candidate_phase_window_independent": True,
        # False means no general invariance guarantee, not that this call necessarily
        # encountered a collision or that every boundary shift changes membership.
        "final_membership_window_independent": False,
    }
    if not kept:
        # Documented deterministic fallback: no candidate cleared the phase filter, so
        # the legacy spacing is used and the caller must lower its evidence grade.
        fallback = non_overlapping(usable, int(horizon))
        provenance.update(method="legacy_spacing_fallback", kept=len(fallback),
                          candidate_phase_window_independent=False, fallback_reason="no_phase_candidates")
        return fallback, provenance
    return kept, provenance
