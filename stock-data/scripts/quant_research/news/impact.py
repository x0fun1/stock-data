"""Deterministic impact magnitude, pattern comparison, breadth scope and source tiering.

The taxonomy is adapted from the MIT-licensed market-news-analyst skill
(https://github.com/tradermonty/claude-trading-skills). Only the deterministic,
frozen-bar computable parts are reused. The upstream composite Impact Score
((price x breadth) x forward modifier) is deliberately NOT reproduced: it is an
uncalibrated pseudo-precise number, which this runtime's probability and
confidence contracts forbid. Nothing here reads Quant output, emits a
probability, or fetches anything.

Every field is a disclosed heuristic band, never a calibrated expectation.
"""

from __future__ import annotations

import math
from typing import Any


# Single-instrument bands. Cross-asset thresholds from the upstream rubric are
# intentionally absent: the News layer only ever reads the one frozen OHLCV
# series for the analysed ticker, so index/sector/yield/FX bands are unobservable.
MAGNITUDE_BANDS: tuple[tuple[str, float], ...] = (
    ("severe", 0.10),
    ("major", 0.05),
    ("moderate", 0.02),
    ("minor", 0.005),
)

# Ratio of |event-session return| to the symbol's own pre-event return sigma.
PATTERN_BANDS: tuple[tuple[str, float], ...] = (
    ("amplified", 1.0),
    ("consistent", 0.5),
)

SOURCE_TIERS: dict[str, str] = {
    "tier1_official_primary": "Official primary record: regulator, central bank, exchange or issuer filing.",
    "tier2_major_financial_news": "Major financial newsroom or wire service.",
    "tier3_specialized_real_time": "Specialized or real-time market coverage.",
    "tier4_analysis_and_aggregator": "Analysis, commentary, or an aggregator relaying another publisher.",
    "unclassified_publisher": "Publisher identity is absent or unrecognized; tier not asserted.",
}

# Publication-quality tiers are only asserted for names a reviewer can verify.
# Anything else stays unclassified rather than being silently promoted.
_TIER1_PUBLISHERS = frozenset({
    "federal reserve", "federalreserve.gov", "sec", "sec.gov", "edgar", "sec edgar",
    "bls", "treasury", "treasury.gov", "cftc", "federal trade commission", "ftc",
    "european central bank", "ecb", "bank of japan", "boj", "people's bank of china", "pboc",
    "company press release", "company filing", "press release", "investor relations",
})
_TIER2_PUBLISHERS = frozenset({
    "reuters", "bloomberg", "wall street journal", "wsj", "financial times", "ft",
    "associated press", "ap", "cnbc", "barron's", "marketwatch", "the economist",
})
_TIER3_PUBLISHERS = frozenset({
    "seeking alpha", "zacks", "investing.com", "barrons", "stocktwits", "benzinga",
    "chartmill", "tipranks", "morningstar", "quartz", "fortune", "forbes",
})
_TIER4_PUBLISHERS = frozenset({
    "yahoo finance", "yahoo", "msn", "aol", "yahoo news", "newsfile", "globenewswire",
    "business wire", "pr newswire", "accesswire", "ein presswire", "simply wall st",
})


def magnitude_band(observed_return: float | None) -> dict[str, Any]:
    """Grade the absolute event-session close-to-close move into a disclosed band."""
    if observed_return is None or not math.isfinite(observed_return):
        return {"band": "not_assessed", "observed_abs_return": None,
                "thresholds": "single_instrument_absolute_return_bands",
                "warning": "no mature frozen session follows this event; magnitude is not assessed"}
    magnitude = abs(float(observed_return))
    band = next((name for name, threshold in MAGNITUDE_BANDS if magnitude >= threshold), "negligible")
    return {
        "band": band,
        "observed_abs_return": round(magnitude, 6),
        "thresholds": "single_instrument_absolute_return_bands",
        "basis": "frozen_daily_close_to_close_return_only; not an index, sector or intraday move",
        "calibration_status": "heuristic_uncalibrated",
    }


# Ratios below this daily sigma are not a usable baseline: a near-constant
# return series makes every event look "amplified". 1e-4 is 1bp per session.
MINIMUM_USABLE_SIGMA = 1e-4


def _trailing_sigma(closes: list[float], index: int, window: int = 20) -> float | None:
    """Sample standard deviation of returns strictly before the event session."""
    start = max(1, index - window)
    segment = closes[start:index]
    if len(segment) < 5:
        return None
    returns = [segment[i] / segment[i - 1] - 1 for i in range(1, len(segment)) if segment[i - 1] > 0]
    if len(returns) < 5:
        return None
    center = sum(returns) / len(returns)
    variance = sum((value - center) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) if variance > 0 else None


def pattern_comparison(
    *, event_direction: str, reaction: dict[str, Any], closes: list[float], session_index: int | None,
) -> dict[str, Any]:
    """Compare the observed move to this symbol's own recent volatility.

    Uses the symbol's trailing sigma rather than a cross-asset historical
    pattern library, so the result is reproducible offline from frozen bars.
    """
    status = reaction.get("status")
    if status not in {"confirmed_positive", "confirmed_negative", "event_price_divergence_positive",
                      "event_price_divergence_negative", "intraday_daily_association"}:
        return {"pattern": "not_assessed", "reason": "no usable frozen market reaction",
                "calibration_status": "heuristic_uncalibrated"}
    if session_index is None or not closes or session_index <= 0 or session_index > len(closes) - 1:
        return {"pattern": "not_assessed", "reason": "event session index unavailable in frozen bars",
                "calibration_status": "heuristic_uncalibrated"}
    sigma = _trailing_sigma(closes, session_index)
    observed = reaction.get("daily_close_return")
    if (sigma is None or sigma <= 0 or sigma < MINIMUM_USABLE_SIGMA
            or observed is None or not math.isfinite(float(observed))):
        return {"pattern": "not_assessed",
                "reason": "insufficient trailing returns or a degenerate volatility baseline for a volatility ratio",
                "calibration_status": "heuristic_uncalibrated"}
    ratio = abs(float(observed)) / sigma
    if status in {"event_price_divergence_positive", "event_price_divergence_negative"}:
        pattern, reason = "inverse", "observed price direction opposes the event direction"
    elif status == "intraday_daily_association":
        pattern, reason = "not_assessed", "whole-session window includes pre-publication trading"
    else:
        pattern = next((name for name, threshold in PATTERN_BANDS if ratio >= threshold), "dampened")
        reason = "observed move relative to this symbol's own trailing return sigma"
    return {
        "pattern": pattern,
        "reason": reason,
        "observed_abs_return": round(abs(float(observed)), 6),
        "trailing_sigma": round(sigma, 6),
        "ratio_to_trailing_sigma": round(ratio, 4),
        "baseline": "own_trailing_20_session_return_sigma",
        "calibration_status": "heuristic_uncalibrated",
    }


def breadth_scope(event_type: str | None) -> dict[str, Any]:
    """Declare the event's potential transmission scope.

    This is a declared scope derived from the event taxonomy, NOT an observed
    cross-asset measurement. The News layer cannot observe index, sector, rate,
    FX or volatility breadth from a single frozen ticker series.
    """
    kind = (event_type or "").upper()
    if kind == "MACRO":
        scope, note = "cross_asset_candidate", "macro or policy event may transmit beyond this ticker; not measured here"
    elif kind in {"EARNINGS", "GUIDANCE", "ANALYST_REVISION"}:
        scope, note = "company_and_sector_candidate", "earnings, guidance or revision may transmit to a sector; not measured here"
    elif kind in {"SUPPLY_CHAIN", "COMPETITION", "REGULATORY"}:
        scope, note = "sector_candidate", "industry or regulatory transmission is possible; not measured here"
    else:
        scope, note = "company_specific_candidate", "no declared transmission beyond the analysed ticker"
    return {"declared_scope": scope, "note": note,
            "observed_breadth": "not_assessed",
            "observation_limit": "single_frozen_ticker_ohlcv_only"}


def _classify_publisher(name: str) -> str:
    folded = name.casefold()
    for tier, known in (("tier1_official_primary", _TIER1_PUBLISHERS),
                        ("tier2_major_financial_news", _TIER2_PUBLISHERS),
                        ("tier3_specialized_real_time", _TIER3_PUBLISHERS),
                        ("tier4_analysis_and_aggregator", _TIER4_PUBLISHERS)):
        if any(folded == item or item in folded for item in known):
            return tier
    return "unclassified_publisher"


def source_tier(publishers: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Assign the weakest tier present across a cluster's publishers.

    Weakest-link is deliberate: one unrecognized publisher must not upgrade the
    credibility of a cluster that also contains it. Ordering runs from the
    strongest tier to ``unclassified_publisher``.
    """
    names = sorted({str(name).strip() for name in publishers if name and str(name).strip()})
    if not names:
        return {"tier": "unclassified_publisher", "tier_meaning": SOURCE_TIERS["unclassified_publisher"],
                "publishers": [], "per_publisher": {}, "calibration_status": "heuristic_uncalibrated"}
    per_publisher = {name: _classify_publisher(name) for name in names}
    order = ("tier1_official_primary", "tier2_major_financial_news",
             "tier3_specialized_real_time", "tier4_analysis_and_aggregator", "unclassified_publisher")
    weakest = max(per_publisher.values(), key=order.index)
    return {"tier": weakest, "tier_meaning": SOURCE_TIERS[weakest], "publishers": names,
            "per_publisher": per_publisher, "selection_rule": "weakest_publisher_in_cluster",
            "calibration_status": "heuristic_uncalibrated"}


ATTRIBUTION_CAVEATS: tuple[str, ...] = (
    "Daily-bar association is temporal only; it does not establish that the headline caused the move.",
    "Absence of magnitude is not evidence that the event was unimportant; it may be priced in, offset, or observed on the wrong session.",
    "Conflicting same-session events cannot be separated in a single close-to-close return; do not attribute the whole move to one headline.",
    "Recency is not importance; a low-magnitude event can still matter for a longer horizon than the observed session.",
    "Multiple concurrent events are not separable here; treat any single-event attribution as one candidate explanation.",
    "Publisher tier reflects an editorial label, not authentication, independence or factual accuracy of a specific claim.",
)


def assess(
    *, event_type: str | None, event_direction: str, reaction: dict[str, Any],
    publishers: list[str] | tuple[str, ...], closes: list[float], session_index: int | None,
) -> dict[str, Any]:
    """Bundle every deterministic News-side impact judgement for one event card."""
    return {
        "magnitude": magnitude_band(reaction.get("daily_close_return")),
        "pattern_comparison": pattern_comparison(
            event_direction=event_direction, reaction=reaction, closes=closes, session_index=session_index),
        "breadth_scope": breadth_scope(event_type),
        "source_tier": source_tier(publishers),
        "attribution_caveats": list(ATTRIBUTION_CAVEATS),
        "composite_impact_score": "not_implemented",
        "composite_impact_score_reason": "an uncalibrated pseudo-precise score is not produced by this runtime",
    }