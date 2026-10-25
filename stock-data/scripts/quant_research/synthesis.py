"""Compare Quant and News without blending, rescoring or confidence arithmetic."""

from __future__ import annotations

import math
from typing import Any

from .security import bounded_text


def reportable_quant_fields(quant_result: dict[str, Any]) -> dict[str, Any]:
    """Copy Quant's production fields; News never edits them."""

    quant = quant_result.get("consensus", {})
    probability = quant.get("prob_up")
    direction = str(quant.get("direction", "unavailable"))
    allowed = (
        direction in {"bullish", "bearish", "neutral"}
        and isinstance(probability, (int, float)) and not isinstance(probability, bool)
        and math.isfinite(probability) and 0 <= probability <= 1
        and quant_result.get("adversarial_audit", {}).get("may_report_direction") is True
    )
    fields = {
        "prob_up": quant.get("prob_up"),
        "raw_probability": quant.get("raw_probability"),
        "direction": quant.get("direction"),
        "model_lean": quant.get("model_lean"),
        "probability_label": "uncalibrated_model_estimate",
        "calibrated_probability": None,
        "production_probability_eligible": bool(quant.get("production_probability_eligible")),
        "directional_edge_supported": bool(quant.get("directional_edge_supported")),
        "evidence": quant.get("evidence") or {},
        "reason_codes": quant.get("reason_codes") or [],
        "confidence_score": None,
        "confidence_score_deprecated": True,
    }
    if not allowed:
        fields.update(prob_up=None, raw_probability=None, direction="research_unavailable",
                      reporting_status="unavailable")
    else:
        fields["reporting_status"] = "reportable_uncalibrated"
    return fields


def build_synthesis(quant_result: dict[str, Any], news_result: dict[str, Any]) -> dict[str, Any]:
    quant = quant_result.get("consensus", {})
    snapshot = quant_result.get("snapshot", {})
    if snapshot and any(news_result.get(key) != snapshot.get(key) for key in ("ticker", "market", "horizon", "snapshot_id", "asof_timestamp")):
        raise ValueError("Quant/News identity or as-of mismatch")
    fields = reportable_quant_fields(quant_result)
    q_direction = str(fields["direction"])
    q_probability = fields["prob_up"]
    q_reportable = q_probability is not None
    news_status = str(news_result.get("status", "not_assessed"))
    n_direction = str(news_result.get("overall_direction", "unknown"))
    sentiment_direction = str((news_result.get("sentiment") or {}).get("consensus_direction", "unknown"))
    alignment_basis = "event"
    if news_status in {"failed", "invalid", "not_assessed", "unavailable"}:
        n_direction = sentiment_direction = "unknown"
    if n_direction == "mixed":
        evidence_direction = "mixed"
        alignment_basis = "event_conflict"
    elif sentiment_direction in {"positive", "negative"} and n_direction in {"positive", "negative"} and n_direction != sentiment_direction:
        evidence_direction = "mixed"
        alignment_basis = "event_sentiment_conflict"
    elif n_direction in {"positive", "negative"}:
        evidence_direction = n_direction
        alignment_basis = "event_with_sentiment" if sentiment_direction in {"positive", "negative"} else "event_only"
    elif sentiment_direction in {"positive", "negative"}:
        evidence_direction = sentiment_direction
        alignment_basis = "sentiment_only"
    else:
        evidence_direction = "unknown"
        alignment_basis = "event_or_sentiment_unavailable"

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
        alignment = "ALIGNMENT"
    else:
        alignment = "DIVERGENCE"

    risks = [
        {"source": "data_or_audit", "detail": bounded_text(message), "severity": "high"}
        for message in (quant_result.get("snapshot", {}).get("data_validation", {}).get("blockers") or [])
        + (quant_result.get("adversarial_audit", {}).get("vetoes") or [])
    ]
    risks.extend(
        {"source": "news", "detail": bounded_text(item.get("headline")), "event_id": item.get("event_id"), "severity": "medium"}
        for item in news_result.get("risks", [])
    )
    risks.extend(
        {"source": "coverage_or_validation", "detail": bounded_text(message), "severity": "medium"}
        for message in (quant_result.get("adversarial_audit", {}).get("warnings") or [])
        + (snapshot.get("warnings") or []) + (news_result.get("warnings") or [])
    )
    unique_risks = list({(row["source"], row["detail"]): row for row in risks}.values())
    warnings = []
    if alignment in {"DIVERGENCE", "MIXED"}:
        warnings.append("Conflicting/neutral evidence is retained; do not force a single directional story.")
    if alignment == "OPINION_ONLY_CROSS_CHECK":
        warnings.append("Only opinion/provider tone is available; it does not verify event facts or a future direction.")
    if alignment == "ABSTAIN":
        warnings.append("Quant probability is unavailable or not eligible; News cannot restore it.")
    if alignment == "NOT_ASSESSED":
        warnings.append("News cross-validation is not assessed; the complete evidence chain is unavailable.")
    return {
        "quant": fields,
        "quant_field_source": "quant_result.json.consensus",
        "economic_viability": {"status": "not_assessed", "cost_model_status": "not_assessed",
                              "execution_validation": "not_assessed", "scenarios": []},
        "alignment": alignment,
        "quantitative_bias": q_direction,
        "quant_prob_up": q_probability,
        "news_event_bias": n_direction,
        "news_evidence_direction": evidence_direction,
        "alignment_basis": alignment_basis,
        "news_sentiment": (news_result.get("sentiment") or {}).get("label", "unknown"),
        "market_confirmation": str(news_result.get("market_confirmation", "not_assessed")),
        "overall_confidence": None,
        "overall_confidence_deprecated": True,
        "quant_probability_preserved": q_probability == quant.get("prob_up"),
        "probability_blending": "prohibited_in_v1",
        "key_risks": unique_risks,
        "evidence_links": {
            "quant_paths": [row.get("path") for row in quant.get("shadow_predictions", [])],
            "news_events": [row.get("event_id") for row in news_result.get("major_events", [])],
            "comparison": "market behavior vs reported catalysts; causality not established",
        },
        "warnings": warnings,
    }
