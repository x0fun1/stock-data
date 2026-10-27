"""Present frozen Quant and News results as independent research sections."""

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
    viability = dict(quant.get("economic_viability") or {})
    viability.setdefault("status", "not_assessed")
    viability["source"] = "quant_result.json.consensus.economic_viability"
    viability["single_source"] = True
    q_probability = fields["prob_up"]
    ai = news_result.get("ai_lean") or {}

    risks = [
        {"source": "data_or_audit", "detail": bounded_text(message), "severity": "high"}
        for message in (quant_result.get("snapshot", {}).get("data_validation", {}).get("blockers") or [])
        + (quant_result.get("adversarial_audit", {}).get("vetoes") or [])
    ]
    risks.extend(
        {"source": "coverage_or_validation", "detail": bounded_text(message), "severity": "medium"}
        for message in (quant_result.get("adversarial_audit", {}).get("warnings") or [])
        + (snapshot.get("warnings") or [])
    )
    unique_risks = list({(row["source"], row["detail"]): row for row in risks}.values())
    return {
        "quant": fields,
        "quant_field_source": "quant_result.json.consensus",
        "economic_viability": viability,
        "alignment": "NOT_COMPUTED",
        "quantitative_bias": fields["direction"],
        "quant_prob_up": q_probability,
        "news_event_bias": None,
        "news_evidence_direction": None,
        "alignment_basis": "independent_sections",
        "news_ai_status": ai.get("status"),
        "news_direct_label_counts": (ai.get("coverage") or {}).get("direct_label_counts"),
        "news_sentiment": "headline_ai" if ai.get("status") == "assessed" else "not_assessed",
        "market_confirmation": str(news_result.get("market_confirmation", "not_assessed")),
        "overall_confidence": None,
        "overall_confidence_deprecated": True,
        "quant_probability_preserved": q_probability == quant.get("prob_up"),
        "probability_blending": "prohibited_in_v1",
        "key_risks": unique_risks,
        "news_major_bearish": [
            {"headline": row.get("headline"), "url": row.get("url"), "reason": row.get("reason")}
            for row in ai.get("major_direct_items") or [] if row.get("lean") == "bearish"
        ],
        "news_warnings": news_result.get("warnings") or [],
        "evidence_links": {
            "quant_paths": [row.get("path") for row in quant.get("shadow_predictions", [])],
            "news_urls": [row.get("url") for row in ai.get("major_direct_items") or []],
        },
        "warnings": [],
    }
