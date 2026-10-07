"""Compare two completed, frozen evidence sets without numeric probability blending."""

from __future__ import annotations

import math
from typing import Any
from .security import bounded_text


CONFIDENCE_ORDER = {"unavailable": -1, "very_low": 0, "low": 1, "medium": 2, "medium_high": 3, "high": 4}


def reportable_quant_fields(quant_result: dict[str, Any]) -> dict[str, Any]:
    """Copy Quant's output fields without consulting News or creating a score."""
    quant = quant_result.get("consensus", {})
    probability = quant.get("prob_up")
    direction = str(quant.get("direction", "unavailable"))
    allowed = (direction in {"bullish", "bearish", "neutral"}
               and isinstance(probability, (int, float)) and not isinstance(probability, bool)
               and math.isfinite(probability) and 0 <= probability <= 1
               and quant_result.get("adversarial_audit", {}).get("may_report_direction") is not False)
    fields = {key: quant.get(key) for key in ("prob_up", "direction", "confidence", "agreement", "raw_probability", "calibrated_probability", "reportable_probability", "reporting_status", "confidence_score", "confidence_reasons", "calibration_status", "probability_basis", "horizon_type") if key in quant}
    if "diversity" in quant:
        fields["diversity"] = quant["diversity"]
    if not allowed:
        fields.update(prob_up=None, raw_probability=None, calibrated_probability=None, reportable_probability=None, direction="research_invalid", reporting_status="veto")
    return fields


def build_synthesis(quant_result: dict[str, Any], news_result: dict[str, Any]) -> dict[str, Any]:
    quant = quant_result.get("consensus", {})
    snapshot = quant_result.get("snapshot", {})
    if snapshot and any(news_result.get(key) != snapshot.get(key) for key in ("ticker", "market", "horizon", "snapshot_id", "asof_timestamp")):
        raise ValueError("Quant/News identity or as-of mismatch")
    quant_fields = reportable_quant_fields(quant_result)
    q_direction = str(quant_fields["direction"])
    q_probability = quant_fields["prob_up"]
    q_reportable = q_probability is not None
    n_direction = str(news_result.get("overall_direction", "unknown"))
    sentiment_direction = str(news_result.get("sentiment", {}).get("consensus_direction", "unknown"))
    alignment_basis = "event"
    if news_result.get("status") in {"failed", "invalid", "not_assessed", "unavailable"}:
        n_direction = sentiment_direction = "unknown"
    if n_direction == "mixed":
        evidence_direction = "mixed"
        alignment_basis = "event_conflict"
    elif sentiment_direction == "mixed":
        evidence_direction = "mixed"
        alignment_basis = "sentiment_source_conflict"
    elif n_direction in {"positive", "negative"} and sentiment_direction in {"positive", "negative"} and n_direction != sentiment_direction:
        evidence_direction = "mixed"
        alignment_basis = "event_sentiment_conflict"
    elif n_direction in {"positive", "negative"}:
        evidence_direction = n_direction
        alignment_basis = "event_with_sentiment" if sentiment_direction in {"positive", "negative"} else "event_only"
    elif n_direction in {"unknown", "unavailable"} and sentiment_direction in {"positive", "negative"}:
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

    if not q_reportable:
        alignment = "ABSTAIN"
    elif evidence_direction not in {"positive", "negative", "neutral", "mixed"}:
        alignment = "NOT_ASSESSED"
    elif evidence_direction == "mixed":
        alignment = "MIXED"
    elif alignment_basis == "sentiment_only":
        alignment = "OPINION_ONLY_CROSS_CHECK"
    elif q_direction == "neutral" and news_result.get("catalysts"):
        alignment = "CATALYST_WITH_NEUTRAL_QUANT"
    elif q_direction == "neutral" or evidence_direction == "neutral":
        alignment = "MIXED"
    elif (q_direction == "bullish" and evidence_direction == "positive") or (q_direction == "bearish" and evidence_direction == "negative"):
        market_matches = (q_direction == "bullish" and market_confirmation == "positive") or (q_direction == "bearish" and market_confirmation == "negative")
        alignment = "STRONG_ALIGNMENT" if market_matches and n_confidence == "medium" else "ALIGNMENT"
    else:
        news_confirmed_against_quant = (q_direction == "bullish" and evidence_direction == "negative" and market_confirmation == "negative") or (q_direction == "bearish" and evidence_direction == "positive" and market_confirmation == "positive")
        alignment = "STRONG_DIVERGENCE" if news_confirmed_against_quant and n_confidence == "medium" else "DIVERGENCE"

    q_confidence = str(quant.get("confidence", "unavailable"))
    if q_confidence not in CONFIDENCE_ORDER:
        # Keep the original field for display; an unknown scale cannot be ranked.
        q_confidence = "unavailable"
    if alignment in {"DIVERGENCE", "STRONG_DIVERGENCE", "MIXED", "OPINION_ONLY_CROSS_CHECK", "CATALYST_WITH_NEUTRAL_QUANT"}:
        final_confidence = min(
            (q_confidence, n_confidence, "low"),
            key=lambda item: CONFIDENCE_ORDER.get(item, -1),
        )
    elif alignment == "ABSTAIN":
        final_confidence = "unavailable"
    elif alignment == "NOT_ASSESSED":
        final_confidence = q_confidence
    else:
        final_confidence = min((q_confidence, n_confidence), key=lambda item: CONFIDENCE_ORDER.get(item, -1))
    risks = []
    for message in snapshot.get("data_validation", {}).get("blockers", []) + quant_result.get("adversarial_audit", {}).get("vetoes", []):
        risks.append({"source": "data_or_audit", "detail": bounded_text(message), "severity": "high"})
    for item in news_result.get("risks", []):
        risks.append({"source": "news", "detail": bounded_text(item.get("headline")), "event_id": item.get("event_id"), "severity": "medium"})
    diagnostics = quant_result.get("researchers", {}).get("factor_backtest", {})
    validation = diagnostics.get("validation") or {}
    horizon = validation.get("requested_horizon")
    for name, factor in validation.get("factor_metrics", {}).items():
        metric = factor.get("horizons", {}).get(horizon, {}).get("holdout", {})
        if metric.get("status") not in {"available", "success"}:
            risks.append({"source": "factor_backtest", "detail": f"{name}: diagnostic holdout is {metric.get('status', 'not_assessed')}; not a forecast", "severity": "medium"})
        elif isinstance(metric.get("mean_forward_return"), (int, float)) and metric["mean_forward_return"] < 0:
            risks.append({"source": "factor_backtest", "detail": f"{name}: negative historical holdout mean return; no causal/directional implication", "severity": "medium"})
    for message in quant_result.get("adversarial_audit", {}).get("warnings", []) + snapshot.get("warnings", []) + news_result.get("warnings", []):
        risks.append({"source": "coverage_or_validation", "detail": bounded_text(message), "severity": "medium"})
    unique_risks = list({(row["source"], row["detail"]): row for row in risks}.values())
    warnings = []
    if alignment in {"DIVERGENCE", "STRONG_DIVERGENCE", "MIXED"}:
        warnings.append("Conflicting/neutral evidence is retained; do not force a single directional story.")
    if alignment == "OPINION_ONLY_CROSS_CHECK":
        warnings.append("Only opinion/provider tone is available; it does not verify event facts or a future direction.")
    if alignment == "ABSTAIN":
        warnings.append("Reporting eligibility is unavailable or vetoed; News cannot restore a Quant probability.")
    if alignment == "NOT_ASSESSED":
        warnings.append("News cross-validation is not assessed; the complete evidence chain is unavailable.")
    return {
        "quant": quant_fields,
        "quant_field_source": "quant_result.json.consensus",
        "alignment": alignment,
        "quantitative_bias": q_direction,
        "quant_prob_up": q_probability,
        "news_event_bias": n_direction,
        "news_evidence_direction": evidence_direction,
        "alignment_basis": alignment_basis,
        "news_sentiment": news_result.get("sentiment", {}).get("label", "unknown"),
        "market_confirmation": market_confirmation,
        "quant_confidence": quant_fields["confidence"],
        "news_confidence": n_confidence,
        "overall_confidence": final_confidence,
        "quant_probability_preserved": q_probability == quant.get("prob_up"),
        "probability_blending": "prohibited_in_v1",
        "key_risks": unique_risks,
        "evidence_links": {"quant_paths": quant.get("path_ids", []), "news_events": [row.get("event_id") for row in news_result.get("major_events", [])], "comparison": "market behavior vs reported catalysts; causality not established"},
        "warnings": warnings,
    }
