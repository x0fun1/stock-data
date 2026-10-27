"""Single-path production probability with isolated shadow candidates."""

from __future__ import annotations

import math
from typing import Any

from .forecast_policy import (
    DRIFT_SHRINKAGE_VERSION,
    FACTOR_BACKTEST_HOLDOUT_MIN,
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_REPLAY_EFFECTIVE_SAMPLES,
    MODEL_VERSIONS,
    POLICY_VERSION,
    PRICE_BASIS,
    SHADOW_PATHS,
    SHRINKAGE_MIN_MATCHES,
    dividend_label_disclosure,
    model_identity,
    policy_metadata,
)

ROUND_TRIP_COST_SCENARIOS_BPS = (5, 10, 20)

#: Compatibility shims retained for older hosts.  The live path uses
#: ``forecast_assessment`` and the ``evidence`` block instead.
LEGACY_UNUSED = ("oos_support_sessions",)


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
        "expected_return_definition": (
            "mean realised forward return of the matching-sample origins; not the probability-weighted "
            "expected return of the production estimator and not a tradable forecast"
        ),
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
        "OOS_SUPPORT_INSUFFICIENT": f"同版本有效OOS回放样本不足{MIN_REPLAY_EFFECTIVE_SAMPLES}个非重叠等价样本。",
        "NO_VALID_OOS_PROBABILITY": "没有有效的非空OOS概率。",
        "OOS_INTEGRITY_FAILED": "OOS概率存在完整性错误。",
        "PREDICTION_INTEGRITY_FAILED": "当前概率无法从冻结行情和固定参数重算。",
        "PRODUCTION_PATH_UNAVAILABLE": "生产路径A没有产出可用结果。",
        "PROSPECTIVE_SUPPORT_INSUFFICIENT": f"真实前向非重叠记录不足{MIN_PROSPECTIVE_NONOVERLAP}条。",
        "DIRECTIONAL_EDGE_UNPROVEN": "相对因果基率的方向优势尚无充分证据。",
        "INTERVAL_BELOW_ZERO": "相对因果基率的成对损失改善区间完全在零以下（劣于纯基率）。",
        "INTERVAL_CROSSES_ZERO": "成对损失改善的区间跨零。",
        "INTERVAL_UNESTIMABLE": "区间无法可靠估计。",
        "TARGET_SESSION_UNRESOLVED": "预测目标交易日未解析，不能发布正式概率。",
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
    if not policy.get("target_session"):
        reasons.append("TARGET_SESSION_UNRESOLVED")
    return reasons


def oos_support_sessions(_researchers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compatibility shim: support is an effective-sample floor, not a score."""

    return {"minimum_effective_samples": MIN_REPLAY_EFFECTIVE_SAMPLES,
            "policy_version": POLICY_VERSION, "scaling": "none",
            "primary_evidence": "full_prefix_replay"}


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
    # The 12-match floor is disclosed context under v2: the matching hit rate no
    # longer carries probability weight, so it cannot gate publication.
    effective = int(oos.get("effective_valid_samples") or 0)
    if effective < MIN_REPLAY_EFFECTIVE_SAMPLES:
        for code in oos.get("reasons") or []:
            if code not in reasons:
                reasons.append(code)
    replay_rows = [
        row for row in ((result.get("validation") or {}).get("oos_forecasts") or [])
        if isinstance(row, dict) and row.get("probability") is not None and row.get("target") is not None
    ]
    edge = directional_edge_assessment(
        replay_records=replay_rows, prospective_records=prospective_records or [],
    )
    eligible = not reasons
    return {
        "computable": result.get("prob_up") is not None,
        "current_prediction_integrity_pass": bool(current.get("pass")),
        "oos_integrity_pass": bool(oos.get("integrity_pass")),
        "production_probability_eligible": bool(eligible),
        "directional_edge_supported": bool(edge.get("supported")),
        "directional_edge_state": edge.get("state"),
        "trading_signal_eligible": False,
        "reason_codes": sorted(set(reasons)),
        "reasons": _reason_text(sorted(set(reasons))),
        "warnings": [] if eligible else _reason_text(sorted(set(reasons))),
        "oos_evidence_audit": oos,
        "current_prediction_audit": current,
        "directional_edge": edge,
        "matching_floor_role": "disclosure_only_not_an_eligibility_gate",
        "minimum_matching_samples": SHRINKAGE_MIN_MATCHES,
        "label_basis": dividend_label_disclosure(),
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
        "oos_support_required_effective_samples": MIN_REPLAY_EFFECTIVE_SAMPLES,
        "oos_effective_samples": oos.get("effective_valid_samples"),
        "oos_nonoverlap": oos.get("nonoverlap_valid_count"),
        "oos_sampling_status": (oos.get("sampling") or {}).get("status"),
        "oos_sampling_role": oos.get("sampling_role"),
        "prospective_nonoverlap": len(prospective),
        "prospective_unanchored": sum(
            1 for row in (prospective_records or [])
            if row.get("status") == "matured" and row.get("sampling_status") == "unanchored_calendar"
        ),
        "prospective_counting_rule": (
            "anchored phase + prompt recording only; unanchored or late records are descriptive"
        ),
        "prospective_required": MIN_PROSPECTIVE_NONOVERLAP,
        "directional_edge_supported": bool(assessment.get("directional_edge_supported")),
        "directional_edge_evidence_scope": (assessment.get("directional_edge") or {}).get("evidence_scope"),
        "directional_edge_replay_interval_excludes_zero": (assessment.get("directional_edge") or {}).get("replay_interval_excludes_zero"),
        "directional_edge_independent_supported": bool((assessment.get("directional_edge") or {}).get("independent_supported")),
        "directional_edge_reasons": (assessment.get("directional_edge") or {}).get("reasons"),
        "directional_edge_state": (assessment.get("directional_edge") or {}).get("state"),
        "directional_edge_primary_evidence": (assessment.get("directional_edge") or {}).get("primary_evidence"),
        "calibration_status": "disabled_by_policy",
        "probability_label": "uncalibrated_model_estimate",
        "relative_baseline_skill": (assessment.get("directional_edge") or {}).get("intervals"),
        "baseline_comparison": (oos.get("effectiveness") or {}),
        "label_basis": dividend_label_disclosure(),
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
        "probability_basis": "drift_shrunk_causal_base_rate",
        "probability_basis_version": transform.get("version") or DRIFT_SHRINKAGE_VERSION,
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


_PUBLISHABLE_KEYS = (
    "prob_up",
    "reportable_probability",
    "raw_probability",
    "calibrated_probability",
    "probability_range",
    "model_lean",
    "direction",
)


def redact_publishable_prediction(consensus: dict[str, Any], *, audit: dict[str, Any]) -> dict[str, Any]:
    """Remove every publishable probability and direction after an audit veto.

    Clearing only the top-level fields left ``consensus.prediction.prob_up``,
    ``consensus.model_lean`` and ``production.probability_transform`` readable by
    any consumer that looked one level deeper.  The transform is preserved for
    audit under ``audit_only`` with an explicit non-publishable marker; every
    field a renderer or downstream tool may treat as a forecast is set to
    unavailable.
    """

    production = consensus.get("production") if isinstance(consensus.get("production"), dict) else {}
    transform = production.get("probability_transform")
    if transform is not None:
        consensus.setdefault("audit_only", {})["probability_transform"] = transform
        consensus["audit_only"]["publishable"] = False
        consensus["audit_only"]["note"] = (
            "frozen estimator inputs retained for audit only; the audit vetoed publication, "
            "so no probability derived from them may be shown or reused"
        )
    production.pop("probability_transform", None)
    prediction = consensus.get("prediction")
    for container in (consensus, production, prediction):
        if not isinstance(container, dict):
            continue
        for key in _PUBLISHABLE_KEYS:
            if key in container:
                container[key] = None
    consensus["model_lean"] = "unavailable"
    consensus["direction"] = "unavailable"
    consensus["status"] = "unavailable"
    consensus["available_paths"] = 0
    if isinstance(prediction, dict):
        prediction["status"] = "unavailable"
        prediction["probability_label"] = "unavailable_after_audit_veto"
        prediction["model_lean"] = "unavailable"
    production["prob_up"] = None
    production["raw_probability"] = None
    production["model_lean"] = "unavailable"
    consensus["redaction"] = {
        "publishable": False,
        "cleared_fields": list(_PUBLISHABLE_KEYS),
        "reason_codes": list(audit.get("reason_codes") or []),
        "rule": "an audit veto leaves no publishable probability or direction anywhere in the payload",
    }
    return consensus
