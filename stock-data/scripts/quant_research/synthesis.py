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
    production = quant.get("production") or {}
    transform = production.get("probability_transform") or {}
    n_eff = transform.get("n_eff")
    prior_weight = (
        50 / (float(n_eff) + 50)
        if isinstance(n_eff, (int, float)) and not isinstance(n_eff, bool) and float(n_eff) + 50 > 0
        else None
    )
    fields = {
        "prob_up": quant.get("prob_up"),
        "raw_probability": None,
        "matching_hit_rate_disclosure_only": quant.get("matching_hit_rate_disclosure_only"),
        "causal_base_rate": production.get("causal_base_rate") or transform.get("base_rate_at_origin"),
        "p_vol": transform.get("p_vol") or production.get("p_vol"),
        "prior_weight": prior_weight,
        "direction": quant.get("direction"),
        "model_lean": quant.get("model_lean"),
        "probability_label": "uncalibrated_model_estimate",
        "calibrated_probability": None,
        "production_probability_eligible": bool(quant.get("production_probability_eligible")),
        "directional_edge_supported": bool(quant.get("directional_edge_supported")),
        "direction_claim_status": quant.get("direction_claim_status"),
        "direction_claimable": bool(quant.get("direction_claimable")),
        "evidence": quant.get("evidence") or {},
        "reason_codes": quant.get("reason_codes") or [],
        "confidence_score": None,
        "confidence_score_deprecated": True,
    }
    if not allowed:
        fields.update(prob_up=None, raw_probability=None, matching_hit_rate_disclosure_only=None,
                      direction="research_unavailable", direction_claim_status="unavailable",
                      direction_claimable=False, reporting_status="unavailable")
    else:
        fields["reporting_status"] = "reportable_uncalibrated"
    return fields


def build_risk_block(
    quant_result: dict[str, Any], news_result: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a deterministic, disclosure-only risk and uncertainty block."""

    consensus = quant_result.get("consensus") or {}
    snapshot = quant_result.get("snapshot") or {}
    evidence = consensus.get("evidence") or {}
    audit = quant_result.get("adversarial_audit") or {}
    window = snapshot.get("policy") or {}
    items: list[dict[str, str]] = []

    def add(source: str, severity: str, detail: str) -> None:
        items.append({"source": source, "severity": severity, "detail": bounded_text(detail, 500)})

    for message in (audit.get("vetoes") or []):
        add("audit_veto", "high", str(message))
    for message in (audit.get("warnings") or []):
        add("audit_warning", "medium", str(message))
    for message in (snapshot.get("data_validation") or {}).get("blockers") or []:
        add("data_blocker", "high", str(message))
    if window.get("coverage_status") != "complete":
        add("coverage", "high", f"52周覆盖状态为 {window.get('coverage_status') or 'unknown'}，正式概率不可用")
    elif int(window.get("missing_sessions") or 0):
        add("coverage", "medium", f"窗口缺失 {window.get('missing_sessions')} 个交易日")
    effective = evidence.get("oos_effective_samples")
    required = evidence.get("oos_support_required_effective_samples")
    if isinstance(effective, (int, float)) and isinstance(required, (int, float)) and effective < required:
        add("replay_support", "high", f"有效回放样本 {effective}/{required}，方向优势证据不足")
    state = evidence.get("directional_edge_state") or "insufficient_evidence"
    if state == "negative_skill_with_estimated_interval_excluding_zero":
        add("directional_edge", "high", "回放劣于纯基率，方向结论 abstain")
    elif not evidence.get("directional_edge_independent_supported"):
        add("directional_edge", "medium", "方向优势缺少独立前向证据，方向结论不可 claim")
    if not evidence.get("point_in_time_certified", False):
        add("point_in_time", "medium", "行情PIT未认证，provider 历史可能被修订")
    news_status = news_result.get("status") or "not_assessed"
    if news_status not in {"complete", "partial", "valid"}:
        add("news", "medium", "新闻未评估，消息面方向不可用")
    coverage = (news_result.get("coverage") or {})
    if coverage.get("coverage_short"):
        add("news_coverage", "medium",
            f"新闻观测跨度仅 {coverage.get('observed_publication_span_days')} 天，低于披露下限")
    label_basis = evidence.get("label_basis") or {}
    if label_basis.get("dividend_adjusted") is False:
        add("label_basis", "low", "5D 标签不含分红再投资，除息可能单向压低接近持平的标签")
    add("execution_cost", "medium", "未建模执行、价差、滑点、费用与冲击成本，收益为毛收益")
    add("calibration", "low", "政策禁用校准；上涨概率不是真实频率")
    unique = list({(row["source"], row["detail"]): row for row in items}.values())
    return {
        "status": "disclosed" if unique else "no_disclosed_risks",
        "disclosure_only": True,
        "items": unique,
        "count": len(unique),
        "probability_effect": "none",
        "note": "assembled deterministically from frozen eligibility, evidence, coverage and news state",
    }


def build_synthesis(
    quant_result: dict[str, Any],
    news_result: dict[str, Any],
    cross_validation: dict[str, Any] | None = None,
    risk_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "cross_validation": cross_validation or {
            "relation": "both_unavailable",
            "explanation_lines": ["量化与新闻均不可用，综合结论不可计算。"],
            "probability_blending": "prohibited_in_v1",
            "joint_probability": None,
        },
        "comparison_basis": "frozen_independent_sections_no_score",
        "risk_block": risk_block or build_risk_block(quant_result, news_result),
        "quantitative_bias": fields["direction"],
        "quant_direction_claim_status": fields.get("direction_claim_status"),
        "quant_prob_up": q_probability,
        "news_event_bias": None,
        "news_evidence_direction": None,
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
