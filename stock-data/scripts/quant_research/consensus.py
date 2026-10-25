"""Single-path production probability with isolated shadow candidates."""

from __future__ import annotations

import math
from typing import Any

from .forecast_policy import (
    FACTOR_BACKTEST_HOLDOUT_MIN,
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_VALID_OOS_NONOVERLAP,
    MODEL_VERSIONS,
    POLICY_VERSION,
    PRICE_BASIS,
    SHADOW_PATHS,
    SHRINKAGE_MIN_MATCHES,
    model_identity,
    policy_metadata,
)

ROUND_TRIP_COST_SCENARIOS_BPS = (5, 10, 20)


def _economic_viability(researchers: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [
        float(result["expected_return"])
        for result in researchers
        if isinstance(result.get("expected_return"), (int, float))
        and not isinstance(result.get("expected_return"), bool)
        and math.isfinite(float(result["expected_return"]))
    ]
    gross = sum(returns) / len(returns) if returns else None
    if gross is None:
        return {"status": "not_assessed", "gross_expected_return": None, "scenarios": [],
                "cost_model_status": "assumed_scenario_sensitivity_not_actual_cost_estimate",
                "execution_validation": "not_assessed"}
    return {
        "status": "scenario_sensitivity_only",
        "gross_expected_return": gross,
        "scenarios": [
            {"round_trip_cost_bps": cost, "net_expected_return": gross - cost / 10000.0,
             "expected_return_below_cost": gross < cost / 10000.0}
            for cost in ROUND_TRIP_COST_SCENARIOS_BPS
        ],
        "cost_model_status": "assumed_scenario_sensitivity_not_actual_cost_estimate",
        "execution_validation": "not_assessed",
    }


def _reason_text(codes: list[str]) -> list[str]:
    messages = {
        "POLICY_VERSION_MISMATCH": "冻结快照的政策版本不是当前固定窗口政策。",
        "INSUFFICIENT_HISTORY_COVERAGE": "52周窗口覆盖不完整，无法发布正式概率。",
        "CALENDAR_UNVERIFIED": "交易所日历无法验证，只能输出诊断。",
        "PRICE_BASIS_UNVERIFIED": "价格基础不是拆股调整收盘价，目标语义不一致。",
        "NO_CURRENT_PROBABILITY": "当前decision session没有可重算的概率。",
        "CURRENT_MATCHING_SAMPLES_INSUFFICIENT": f"当前匹配样本不足{SHRINKAGE_MIN_MATCHES}条。",
        "OOS_SUPPORT_INSUFFICIENT": f"同版本有效非重叠OOS预测不足{MIN_VALID_OOS_NONOVERLAP}条。",
        "NO_VALID_OOS_PROBABILITY": "没有有效的非空OOS概率。",
        "OOS_INTEGRITY_FAILED": "OOS概率存在完整性错误。",
        "PREDICTION_INTEGRITY_FAILED": "当前概率无法从冻结行情和固定参数重算。",
        "PRODUCTION_PATH_UNAVAILABLE": "生产路径A没有产出可用结果。",
        "PROSPECTIVE_SUPPORT_INSUFFICIENT": f"真实前向非重叠记录不足{MIN_PROSPECTIVE_NONOVERLAP}条。",
        "DIRECTIONAL_EDGE_UNPROVEN": "相对因果基率的方向优势尚无充分证据。",
        "INTERVAL_CROSSES_ZERO": "成对损失改善的区间跨零。",
        "INTERVAL_UNESTIMABLE": "区间无法可靠估计。",
    }
    return [messages.get(code, code) for code in codes]


def _manifest_policy_reasons(manifest: dict[str, Any]) -> list[str]:
    policy = manifest.get("policy") or {}
    reasons: list[str] = []
    if policy.get("policy_version") != POLICY_VERSION:
        reasons.append("POLICY_VERSION_MISMATCH")
    if policy.get("coverage_status") != "complete":
        reasons.append("INSUFFICIENT_HISTORY_COVERAGE")
    if not policy.get("calendar_verified"):
        reasons.append("CALENDAR_UNVERIFIED")
    validation = manifest.get("data_validation") or {}
    declared_basis = (validation.get("policy") or {}).get("price_basis") or policy.get("price_basis")
    if declared_basis != PRICE_BASIS:
        reasons.append("PRICE_BASIS_UNVERIFIED")
    return reasons


def oos_support_sessions(_researchers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compatibility shim: support is a fixed policy floor, not a scaled score."""

    return {"minimum_nonoverlap": MIN_VALID_OOS_NONOVERLAP, "policy_version": POLICY_VERSION, "scaling": "none"}


def forecast_assessment(
    result: dict[str, Any],
    manifest: dict[str, Any],
    *,
    oos_evidence_audit: dict[str, Any] | None = None,
    support_sessions: dict[str, Any] | None = None,
    bars: list[dict[str, Any]] | None = None,
    sessions: list[Any] | None = None,
    prospective_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade one researcher path against the fixed-policy publication rules."""

    del support_sessions
    from .validation.probability_quality import (
        audit_current_prediction,
        audit_oos_result,
        directional_edge_assessment,
    )

    path = str(result.get("researcher_id"))
    validation = result.get("validation") or {}
    current = audit_current_prediction(result, manifest, bars)
    oos = oos_evidence_audit or audit_oos_result(result, manifest, bars, sessions=sessions)
    reasons: list[str] = list(_manifest_policy_reasons(manifest))
    if result.get("status") not in {"success"}:
        reasons.append("PRODUCTION_PATH_UNAVAILABLE")
    if result.get("prob_up") is None:
        reasons.append("NO_CURRENT_PROBABILITY")
    if not current.get("pass"):
        reasons.extend(current.get("reasons") or ["PREDICTION_INTEGRITY_FAILED"])
    if not oos.get("integrity_pass"):
        reasons.extend(oos.get("reasons") or ["OOS_INTEGRITY_FAILED"])
    matching = validation.get("n_raw")
    if not isinstance(matching, int) or matching < SHRINKAGE_MIN_MATCHES:
        reasons.append("CURRENT_MATCHING_SAMPLES_INSUFFICIENT")
    if int(oos.get("nonoverlap_valid_count") or 0) < MIN_VALID_OOS_NONOVERLAP:
        for code in oos.get("reasons") or []:
            if code not in reasons:
                reasons.append(code)
    edge = directional_edge_assessment(prospective_records or [])
    eligible = not reasons
    return {
        "computable": result.get("prob_up") is not None,
        "current_prediction_integrity_pass": bool(current.get("pass")),
        "oos_integrity_pass": bool(oos.get("integrity_pass")),
        "production_probability_eligible": bool(eligible),
        "directional_edge_supported": bool(edge.get("supported")),
        "trading_signal_eligible": False,
        "reason_codes": sorted(set(reasons)),
        "reasons": _reason_text(sorted(set(reasons))),
        "warnings": [] if eligible else _reason_text(sorted(set(reasons))),
        "oos_evidence_audit": oos,
        "current_prediction_audit": current,
        "directional_edge": edge,
        "path": path,
        "model_identity": validation.get("model_identity") or model_identity(path if path in MODEL_VERSIONS else "quant"),
    }


def _model_lean(probability: float | None) -> str:
    if probability is None:
        return "unavailable"
    if probability > 0.5:
        return "up"
    if probability < 0.5:
        return "down"
    return "flat"


def build_consensus(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    assessments: dict[str, dict[str, Any]] | None = None,
    bars: list[dict[str, Any]] | None = None,
    sessions: list[Any] | None = None,
    prospective_records: list[dict[str, Any]] | None = None,
    skill_epsilon: float = 0.0,
) -> dict[str, Any]:
    """Single production path (A); B/C are shadow candidates; D is a diagnostic."""

    del skill_epsilon
    by_id = {str(item.get("researcher_id")): item for item in researchers}
    production = by_id.get("quant")
    assessment = (assessments or {}).get("quant")
    if assessment is None:
        assessment = forecast_assessment(
            production or {}, manifest, bars=bars, sessions=sessions,
            prospective_records=prospective_records,
        )
    eligible = bool(assessment.get("production_probability_eligible"))
    probability = production.get("prob_up") if production and eligible else None
    raw = production.get("raw_probability") if production and eligible else None
    validation = (production or {}).get("validation") or {}
    transform = validation.get("probability_transform") or {}
    lean = _model_lean(probability)
    direction = {"up": "bullish", "down": "bearish", "flat": "neutral"}.get(lean, "unavailable")

    shadow = []
    for path in SHADOW_PATHS:
        item = by_id.get(path)
        if not item:
            continue
        shadow.append({
            "path": path,
            "role": "shadow",
            "model_version": MODEL_VERSIONS[path],
            "status": item.get("status"),
            "probability": item.get("prob_up"),
            "raw_probability": item.get("raw_probability"),
            "note": "shadow research candidate; not part of the production probability and not a confidence vote",
            "production_probability": False,
        })
    diagnostic = by_id.get("factor_backtest")
    diagnostics = {}
    if diagnostic:
        dvalidation = diagnostic.get("validation") or {}
        diagnostics = {
            "path": "factor_backtest",
            "role": "diagnostic",
            "status": diagnostic.get("status"),
            "horizon": diagnostic.get("horizon"),
            "descriptive_only": True,
            "information_coefficient": dvalidation.get("information_coefficient"),
            "holdout_sample_count": dvalidation.get("holdout_sample_count"),
            "holdout_minimum": FACTOR_BACKTEST_HOLDOUT_MIN,
            "note": "descriptive 5D diagnostic; never participates in probability, eligibility or promotion",
        }

    oos = assessment.get("oos_evidence_audit") or {}
    prospective = [row for row in (prospective_records or [])
                   if row.get("sampled") is True and row.get("status") == "matured"]
    evidence = {
        "replay_type": "prefix_walk_forward",
        "replay_nonoverlap": oos.get("nonoverlap_valid_count"),
        "replay_valid": oos.get("valid_count"),
        "replay_total": oos.get("record_count"),
        "replay_abstained": oos.get("abstained_count"),
        "replay_coverage_ratio": oos.get("coverage_ratio"),
        "oos_support_required": MIN_VALID_OOS_NONOVERLAP,
        "prospective_nonoverlap": len(prospective),
        "prospective_required": MIN_PROSPECTIVE_NONOVERLAP,
        "directional_edge_supported": bool(assessment.get("directional_edge_supported")),
        "directional_edge_reasons": (assessment.get("directional_edge") or {}).get("reasons"),
        "calibration_status": "disabled_by_policy",
        "probability_label": "uncalibrated_model_estimate",
        "relative_baseline_skill": (assessment.get("directional_edge") or {}).get("intervals"),
    }

    reason_codes = list(assessment.get("reason_codes") or [])
    if not eligible and probability is None:
        reason_codes = sorted(set(reason_codes) | {"NO_CURRENT_PROBABILITY"})
    prediction = {
        "status": "available" if probability is not None else "unavailable",
        "prob_up": probability,
        "reportable_probability": probability,
        "raw_probability": raw,
        "calibrated_probability": None,
        "model_lean": lean,
        "probability_label": "uncalibrated_model_estimate",
    }
    return {
        "status": "available" if probability is not None else "unavailable",
        "policy": policy_metadata(),
        "production": {
            "path": "quant",
            "model_version": MODEL_VERSIONS["quant"],
            "prob_up": probability,
            "raw_probability": raw,
            "model_lean": lean,
            "probability_label": "uncalibrated_model_estimate",
            "current_prediction_integrity_pass": bool(assessment.get("current_prediction_integrity_pass")),
            "oos_integrity_pass": bool(assessment.get("oos_integrity_pass")),
            "production_probability_eligible": eligible,
            "directional_edge_supported": bool(assessment.get("directional_edge_supported")),
            "trading_signal_eligible": False,
            "matching_observations": validation.get("n_raw"),
            "causal_base_rate": transform.get("base_rate_at_origin"),
            "probability_transform": transform,
        },
        "computable": bool(assessment.get("computable")),
        "current_prediction_integrity_pass": bool(assessment.get("current_prediction_integrity_pass")),
        "oos_integrity_pass": bool(assessment.get("oos_integrity_pass")),
        "production_probability_eligible": eligible,
        "directional_edge_supported": bool(assessment.get("directional_edge_supported")),
        "trading_signal_eligible": False,
        "prob_up": probability,
        "raw_probability": raw,
        "reportable_probability": probability,
        "calibrated_probability": None,
        "probability_basis": "causal_empirical_shrinkage",
        "calibration_status": "disabled_by_policy",
        "confidence": None,
        "available_paths": 1 if probability is not None else 0,
        "path_ids": ["quant"],
        "probability_range": None,
        "direction": direction,
        "model_lean": lean,
        "confidence_score": None,
        "confidence_score_deprecated": True,
        "aggregation_method": "single_path",
        "production_path_count": 1,
        "shadow_predictions": shadow,
        "diagnostics": diagnostics,
        "paths": [
            {"path": "quant", "role": "production", "model_version": MODEL_VERSIONS["quant"],
             "status": (production or {}).get("status")},
            *[{"path": path, "role": "shadow", "model_version": MODEL_VERSIONS[path],
               "status": (by_id.get(path) or {}).get("status")} for path in SHADOW_PATHS],
            {"path": "factor_backtest", "role": "diagnostic",
             "model_version": MODEL_VERSIONS["factor_backtest"],
             "status": (diagnostic or {}).get("status")},
        ],
        "evidence": evidence,
        "reason_codes": reason_codes,
        "reasons": assessment.get("reasons") or [],
        "warnings": assessment.get("warnings") or [],
        "assessment": assessment,
        "prediction": prediction,
        "economic_viability": _economic_viability([production] if production else []),
    }
