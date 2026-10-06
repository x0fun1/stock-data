"""Compare two completed, frozen evidence sets without numeric probability blending."""

from __future__ import annotations

from typing import Any


CONFIDENCE_ORDER = {"unavailable": -1, "very_low": 0, "low": 1, "medium": 2, "medium_high": 3, "high": 4}


def build_synthesis(quant_result: dict[str, Any], news_result: dict[str, Any]) -> dict[str, Any]:
    quant = quant_result.get("consensus", {})
    q_direction = str(quant.get("direction", "unavailable"))
    q_probability = quant.get("prob_up")
    n_direction = str(news_result.get("overall_direction", "unknown"))
    sentiment_direction = str(news_result.get("sentiment", {}).get("consensus_direction", "unknown"))
    alignment_basis = "event"
    if sentiment_direction == "mixed":
        evidence_direction = "mixed"
        alignment_basis = "sentiment_source_conflict"
    elif n_direction in {"positive", "negative"} and sentiment_direction in {"positive", "negative"} and n_direction != sentiment_direction:
        evidence_direction = "mixed"
        alignment_basis = "event_sentiment_conflict"
    elif n_direction in {"positive", "negative"}:
        evidence_direction = n_direction
        alignment_basis = "event_with_sentiment" if sentiment_direction in {"positive", "negative"} else "event_only"
    elif sentiment_direction in {"positive", "negative"}:
        evidence_direction = sentiment_direction
        alignment_basis = "sentiment_only"
    elif sentiment_direction == "mixed" or news_result.get("sentiment", {}).get("cross_source_divergence") == "high":
        evidence_direction = "mixed"
        alignment_basis = "sentiment_source_conflict"
    else:
        evidence_direction = n_direction
        alignment_basis = "event_or_sentiment_unavailable"
    n_confidence = str(news_result.get("confidence", "unavailable"))
    market_confirmation = str(news_result.get("market_confirmation", "not_assessed"))

    if q_direction not in {"bullish", "bearish", "neutral"} or evidence_direction not in {"positive", "negative", "neutral", "mixed"}:
        alignment = "NOT_ASSESSED"
    elif q_direction == "neutral" or evidence_direction in {"neutral", "mixed"}:
        alignment = "MIXED"
    elif (q_direction == "bullish" and evidence_direction == "positive") or (q_direction == "bearish" and evidence_direction == "negative"):
        market_matches = (q_direction == "bullish" and market_confirmation == "positive") or (q_direction == "bearish" and market_confirmation == "negative")
        alignment = "STRONG_ALIGNMENT" if market_matches and n_confidence == "medium" else "ALIGNMENT"
    else:
        news_confirmed_against_quant = (q_direction == "bullish" and evidence_direction == "negative" and market_confirmation == "negative") or (q_direction == "bearish" and evidence_direction == "positive" and market_confirmation == "positive")
        alignment = "STRONG_DIVERGENCE" if news_confirmed_against_quant and n_confidence == "medium" else "DIVERGENCE"

    q_confidence = str(quant.get("confidence", "unavailable"))
    if alignment in {"DIVERGENCE", "STRONG_DIVERGENCE"}:
        final_confidence = min(
            (q_confidence, n_confidence, "low"),
            key=lambda item: CONFIDENCE_ORDER.get(item, -1),
        )
    elif alignment == "NOT_ASSESSED":
        final_confidence = q_confidence
    else:
        final_confidence = min((q_confidence, n_confidence), key=lambda item: CONFIDENCE_ORDER.get(item, -1))
    return {
        "alignment": alignment,
        "quantitative_bias": q_direction,
        "quant_prob_up": q_probability,
        "news_event_bias": n_direction,
        "news_evidence_direction": evidence_direction,
        "alignment_basis": alignment_basis,
        "news_sentiment": news_result.get("sentiment", {}).get("label", "unknown"),
        "market_confirmation": market_confirmation,
        "quant_confidence": q_confidence,
        "news_confidence": n_confidence,
        "overall_confidence": final_confidence,
        "quant_probability_preserved": True,
        "probability_blending": "prohibited_in_v1",
        "key_risks": news_result.get("risks", [])[:5],
        "warnings": (
            ["Quant and News evidence diverge; lower the combined confidence and investigate the conflicting events."]
            if alignment in {"DIVERGENCE", "STRONG_DIVERGENCE"}
            else ["News event and opinion evidence conflict; do not reduce them to one directional story."]
            if alignment == "MIXED" and alignment_basis in {"event_sentiment_conflict", "sentiment_source_conflict"}
            else []
        ),
    }
