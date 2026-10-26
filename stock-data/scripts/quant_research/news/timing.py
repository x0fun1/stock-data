"""Non-directional News timing facts: publication bucket and priced-in state.

Two findings drive this module.

1. Publication timing carries most of the event's information.  On 318 real SEC
   8-K Item 2.02 events the absolute 5-day move after an after-close release was
   5.28%, versus 2.57% for pre-market releases: the overnight gap, not the
   direction, is what the release actually produces.  A single ``impact`` band
   that does not say *when* the release landed hides that difference.
2. The event session's own move does not predict the following 5 days
   (``corr = -0.003`` over the same 318 events).  What the News layer can
   honestly add is therefore not a direction but a priced-in judgement: a large
   event-session move with no continuation is evidence the information was
   absorbed immediately.

Neither output is a probability, neither has a direction, and neither ever enters
Quant ``P(up)``.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from ..contracts import parse_timestamp
from .impact import trailing_sigma

#: Regular US session length used to derive the open from the recorded close.
SESSION_HOURS = 6.5
PRICED_IN_SIGMA_MULTIPLE = 1.0
PRICED_IN_MIN_EVENTS = 3

BUCKET_PRE_MARKET = "pre_market"
BUCKET_INTRADAY = "intraday"
BUCKET_AFTER_CLOSE = "after_close"
BUCKET_UNKNOWN = "unknown"


def event_time_bucket(
    published_at: Any,
    session_date: Any,
    session_closes: dict[str, str] | None,
) -> dict[str, Any]:
    """Classify a publication time relative to its mapped trading session.

    The bucket is derived from the frozen session close time minus a 6.5-hour
    regular session, so it is reproducible offline and identical on every run.
    When the mapped session or its close time is unavailable the bucket is
    ``unknown`` rather than guessed.
    """

    basis = "session_close_at minus 6.5h regular session; frozen calendar only"
    if not published_at or not session_date or not session_closes:
        return {"bucket": BUCKET_UNKNOWN, "basis": basis, "reason": "publication time or session close unavailable"}
    close_at = session_closes.get(str(session_date)[:10])
    if not close_at:
        return {"bucket": BUCKET_UNKNOWN, "basis": basis, "reason": "mapped session has no recorded close time"}
    close = parse_timestamp(close_at)
    opened = close - timedelta(hours=SESSION_HOURS)
    published = parse_timestamp(published_at)
    if published < opened:
        bucket = BUCKET_PRE_MARKET
    elif published <= close:
        bucket = BUCKET_INTRADAY
    else:
        bucket = BUCKET_AFTER_CLOSE
    return {
        "bucket": bucket,
        "basis": basis,
        "session_date": str(session_date)[:10],
        "session_open_at": opened.isoformat().replace("+00:00", "Z"),
        "session_close_at": close.isoformat().replace("+00:00", "Z"),
        "published_at": published.isoformat().replace("+00:00", "Z"),
        "information_path": (
            "overnight_gap_into_next_session" if bucket == BUCKET_AFTER_CLOSE
            else "same_session" if bucket == BUCKET_INTRADAY
            else "pre_open_positioning"
        ),
        "direction_neutral": True,
    }


def priced_in_assessment(
    *,
    reaction: dict[str, Any],
    closes: list[float],
    session_index: int | None,
    event_direction: str,
    horizon: int = 5,
    sigma_multiple: float = PRICED_IN_SIGMA_MULTIPLE,
) -> dict[str, Any]:
    """Judge whether the event session already absorbed the news.

    ``likely_priced_in`` requires both a large event-session move
    (``|move| >= sigma_multiple * sigma_5d``) and no continuation in the event's
    direction over the next ``horizon`` sessions.  It is a descriptive state, not
    a forecast: it says the news is probably in the price, not what happens next.
    """

    base = {"display_only": True, "probability_effect": "none", "direction_neutral": True}
    status = reaction.get("status")
    if status not in {
        "confirmed_positive", "confirmed_negative",
        "event_price_divergence_positive", "event_price_divergence_negative",
    }:
        return {**base, "state": "not_assessable", "reason": "reaction is not classifiable for this event session"}
    if event_direction not in {"positive", "negative"}:
        return {**base, "state": "not_assessable", "reason": "event direction is not one-sided"}
    if session_index is None or session_index <= 0 or session_index + horizon >= len(closes):
        return {**base, "state": "not_assessable", "reason": "the 5-session continuation window has not matured in the frozen bars"}
    sigma, observations = trailing_sigma(closes, session_index)
    observed = reaction.get("daily_close_return")
    if sigma is None or sigma <= 0 or observed is None or not math.isfinite(float(observed)):
        return {**base, "state": "not_assessable", "reason": "no usable trailing volatility baseline"}
    sigma_horizon = sigma * math.sqrt(horizon)
    move_ratio = abs(float(observed)) / sigma_horizon
    forward = closes[session_index + horizon] / closes[session_index] - 1.0
    event_sign = 1 if event_direction == "positive" else -1
    continuation = (forward > 0) == (event_sign > 0)
    if move_ratio < sigma_multiple:
        state = "move_within_noise"
        reason = "the event-session move is smaller than one 5-session sigma; priced-in cannot be judged"
    elif continuation:
        state = "continued"
        reason = "a large event-session move was followed by a same-direction 5-session drift"
    else:
        state = "likely_priced_in"
        reason = "a large event-session move was not followed by same-direction drift"
    return {
        **base,
        "state": state,
        "reason": reason,
        "event_session_return": round(float(observed), 6),
        "sigma_5d": round(sigma_horizon, 6),
        "move_to_sigma_5d": round(move_ratio, 4),
        "forward_5d_return": round(forward, 6),
        "continuation": continuation,
        "trailing_sigma_observations": observations,
        "threshold_sigma_multiple": sigma_multiple,
        "calibration_status": "heuristic_uncalibrated",
    }


def aggregate_priced_in(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-event priced-in states into one disclosed, non-directional state."""

    assessable = [card for card in cards if (card.get("already_priced_in") or {}).get("state") in {"likely_priced_in", "continued"}]
    if len(assessable) < PRICED_IN_MIN_EVENTS:
        return {
            "state": "not_assessable",
            "assessable_events": len(assessable),
            "minimum_events": PRICED_IN_MIN_EVENTS,
            "reason": "too few matured event windows to judge absorption",
            "display_only": True,
            "probability_effect": "none",
        }
    priced_in = sum(1 for card in assessable if card["already_priced_in"]["state"] == "likely_priced_in")
    share_priced_in = priced_in / len(assessable)
    return {
        "state": "likely_priced_in" if share_priced_in >= 0.5 else "mixed_absorption",
        "assessable_events": len(assessable),
        "likely_priced_in_events": priced_in,
        "share_priced_in": round(share_priced_in, 4),
        "rule": "share of matured event windows with a >=1 sigma_5d move and no same-direction continuation",
        "display_only": True,
        "probability_effect": "none",
        "direction_neutral": True,
    }
