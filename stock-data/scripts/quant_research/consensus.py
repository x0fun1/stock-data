"""Probability estimates, validation metadata, and explanatory confidence scoring."""

from __future__ import annotations

import math
from typing import Any

from .math_utils import logistic, mean, sample_std
from .validation.probability_quality import (
    DEFAULT_BRIER_SKILL_EPSILON,
    audit_oos_result,
    build_probability_validation,
    path_has_detected_leakage,
    unique_research_paths,
)

ROUND_TRIP_COST_SCENARIOS_BPS = (5, 10, 20)


def _economic_viability(researchers: list[dict[str, Any]]) -> dict[str, Any]:
    available = [
        (str(result.get("researcher_id", "unknown")), float(result["expected_return"]))
        for result in researchers
        if isinstance(result.get("expected_return"), (int, float))
        and not isinstance(result.get("expected_return"), bool)
        and math.isfinite(float(result["expected_return"]))
    ]
    gross = mean(value for _, value in available)
    if gross is None:
        return {
            "status": "not_assessed",
            "gross_expected_return": None,
            "aggregation_method": "equal_mean_of_available_path_expected_returns",
            "contributing_paths": [],
            "expected_return_source_count": 0,
            "scenarios": [],
            "cost_model_status": "assumed_scenario_sensitivity_not_actual_cost_estimate",
            "execution_validation": "not_assessed",
        }
    return {
        "status": "scenario_sensitivity_only",
        "gross_expected_return": gross,
        "aggregation_method": "equal_mean_of_available_path_expected_returns",
        "contributing_paths": [name for name, _ in available],
        "expected_return_source_count": len(available),
        "scenarios": [
            {
                "round_trip_cost_bps": cost_bps,
                "net_expected_return": gross - cost_bps / 10000.0,
                "expected_return_below_cost": gross < cost_bps / 10000.0,
                "cost_model_status": "assumed_scenario_only_not_live_fee_slippage_or_impact",
            }
            for cost_bps in ROUND_TRIP_COST_SCENARIOS_BPS
        ],
        "cost_model_status": "assumed_scenario_sensitivity_not_actual_cost_estimate",
        "execution_validation": "not_assessed",
        "market_impact_estimate": "not_assessed",
        "strategy_net_return_backtest": "not_assessed",
    }


def _oos_count(result: dict[str, Any], oos_evidence_audit: dict[str, Any] | None = None) -> int:
    if isinstance(oos_evidence_audit, dict):
        # Prefer the conservative non-overlapping count: the walk-forward ledger records daily
        # predictions whose horizon labels overlap, so repeated labels are not counted as equal
        # independent samples.
        value = oos_evidence_audit.get("non_overlapping_prediction_count",
                                       oos_evidence_audit.get("valid_prediction_count", 0))
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
    validation = result.get("validation") or {}
    name = result.get("researcher_id")
    if name in {"quant", "ml"}:
        value: Any = validation.get("non_overlapping_prediction_count")
        if not isinstance(value, int) or isinstance(value, bool):
            value = validation.get("oos_predictions", 0)
    elif name == "factor":
        value = validation.get("holdout_predictions", 0)
    else:
        value = validation.get("final_holdout", {}).get("observations", validation.get("final_holdout", {}).get("n", 0))
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def forecast_assessment(
    result: dict[str, Any], manifest: dict[str, Any],
    *, oos_evidence_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    codes: list[str] = []
    messages: list[str] = []
    probability = result.get("prob_up")
    if result.get("result_role", "forecast") != "forecast":
        return {"forecast_eligible": False, "prediction_eligibility": {"status": "not_applicable"},
                "forecast_exclusion_reasons": ["diagnostic-only path"], "probability_calibration": "not_applicable",
                "oos_validation": {"status": "not_applicable", "sample_count": 0}, "reason_codes": []}
    data_gate = manifest.get("data_validation", {})
    eligible = data_gate.get("prediction_eligibility", {}).get("status")
    if eligible is None:  # Legacy manifest compatibility.
        eligible = "eligible" if data_gate.get("forecast_eligible", False) else "blocked"
    if eligible != "eligible":
        codes.extend(data_gate.get("prediction_eligibility", {}).get("blockers", []) or ["PREDICTION_FAILED"])
        messages.append("Prediction eligibility is blocked by the market-data gate.")
    if any(result.get(key) != manifest.get(key) for key in ("ticker", "horizon", "snapshot_id")):
        codes.append("SNAPSHOT_IDENTITY_INVALID")
        messages.append("Research path identity differs from the frozen snapshot.")
    if (result.get("status") not in {"success", "partial"}
            or isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not math.isfinite(probability) or not 0 <= probability <= 1
            or not result.get("probability_source")):
        codes.append("PREDICTION_FAILED")
        messages.append("This path did not produce a contract-valid quantitative probability.")

    validation = result.get("validation") or {}
    name = result.get("researcher_id")
    calibration = ("empirical_frequency" if name in {"quant", "factor"}
                   else "calibrated" if validation.get("probability_calibration", {}).get("calibrated")
                   else "uncalibrated")
    leakage = validation.get("leakage_audit") or {}
    feature_check = leakage.get("features_use_data_through_decision_close_only")
    label_check = leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features"))
    if feature_check is False or label_check is False:
        codes.append("LEAKAGE_DETECTED")
        messages.append("A declared feature/label timing check detected leakage; this path is excluded.")
    elif feature_check is not True or label_check is not True:
        codes.append("LEAKAGE_AUDIT_INCOMPLETE")
        messages.append("Leakage audit evidence is incomplete; the probability is retained with lower confidence.")
    purged = validation.get("purged_kfold") or {}
    if isinstance(purged, dict) and purged.get("retained_interval_overlaps", 0) not in (None, 0):
        codes.append("LEAKAGE_DETECTED")
        messages.append("Purged validation reports overlapping train/test information intervals.")

    count = _oos_count(result, oos_evidence_audit)
    if count >= 100:
        oos_status = "strong"
    elif count >= 40:
        oos_status = "acceptable"
    elif count >= 20:
        oos_status = "weak"
    else:
        oos_status = "insufficient"
    if "LEAKAGE_DETECTED" in codes:
        oos_status = "invalid"
    if oos_status in {"weak", "insufficient"}:
        codes.append("OOS_INSUFFICIENT")
        detail = f"N={count}"
        if isinstance(oos_evidence_audit, dict) and isinstance(oos_evidence_audit.get("prediction_days"), int):
            detail = f"N={count} non-overlapping of {oos_evidence_audit['prediction_days']} prediction days"
        messages.append(f"Chronological OOS support is {oos_status} ({detail}); this lowers confidence but does not erase the forecast.")
    if calibration == "uncalibrated":
        codes.append("CALIBRATION_MISSING")
        messages.append("Probability calibration is unavailable; the raw model probability is retained with lower confidence.")
    if isinstance(oos_evidence_audit, dict) and oos_evidence_audit.get("rejected_samples", 0):
        codes.append("OOS_EVIDENCE_INVALID")
        messages.append("Some OOS records failed frozen-session, target, or matured-training-label verification; rejected records are excluded from validation metrics.")
    if result.get("status") not in {"success", "partial"}:
        codes.append("PATH_FAILED")
    fatal_codes = [code for code in codes if code in {"SNAPSHOT_IDENTITY_INVALID", "PREDICTION_FAILED", "LEAKAGE_DETECTED"}]
    eligible_path = not fatal_codes

    return {
        "forecast_eligible": eligible_path,
        "prediction_eligibility": {"status": "eligible" if eligible_path else "blocked", "blockers": fatal_codes},
        "reporting_status": "degraded" if messages and eligible_path else "veto" if not eligible_path else "pass",
        "forecast_exclusion_reasons": messages if not eligible_path else [],
        "warnings": messages if eligible_path else [],
        "reason_codes": list(dict.fromkeys(codes)),
        "probability_calibration": calibration,
        "oos_validation": {"status": oos_status, "sample_count": count},
        "calibration_available": calibration in {"calibrated", "empirical_frequency"},
    }


def _families(result: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    evidence = result.get("evidence") or {}
    for polarity in ("positive", "negative"):
        for item in evidence.get(polarity, []):
            if item.get("family"):
                found.add(str(item["family"]))
            for nested in item.get("factors", []):
                if nested.get("family"):
                    found.add(str(nested["family"]))
    return found


def _confidence_label(score: int) -> str:
    return "high" if score >= 80 else "medium" if score >= 60 else "low" if score >= 40 else "very_low"


def _predictive_evidence_grade(quality: dict[str, Any]) -> str:
    replay = quality.get("algorithm_policy_replay") or {}
    count = replay.get("non_overlapping_predicted_dates", replay.get("predicted_dates", 0))
    skill = replay.get("brier_skill_vs_expanding_base_rate")
    if (not isinstance(count, int) or isinstance(count, bool) or count < 20
            or isinstance(skill, bool) or not isinstance(skill, (int, float))
            or not math.isfinite(float(skill))):
        return "insufficient_oos_evidence"
    uncertainty = replay.get("skill_uncertainty") or {}
    ci_low = uncertainty.get("ci_low") if isinstance(uncertainty, dict) else None
    ci_high = uncertainty.get("ci_high") if isinstance(uncertainty, dict) else None
    ci_valid = (isinstance(ci_low, (int, float)) and not isinstance(ci_low, bool) and math.isfinite(float(ci_low))
                and isinstance(ci_high, (int, float)) and not isinstance(ci_high, bool) and math.isfinite(float(ci_high)))
    if ci_valid and float(ci_high) < 0:
        return "observed_skill_below_expanding_base_rate"
    if skill <= 0:
        return "no_observed_skill_vs_expanding_base_rate"
    if count < 100:
        return "exploratory_positive_skill_unconfirmed"
    if not ci_valid:
        return "positive_skill_point_estimate_uncertainty_unestimated"
    if float(ci_low) > 0:
        return "positive_skill_with_estimated_interval_excluding_zero"
    return "positive_skill_with_estimated_interval_including_zero"


def build_consensus(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    assessments: dict[str, dict[str, Any]] | None = None,
    bars: list[dict[str, Any]] | None = None,
    skill_epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
) -> dict[str, Any]:
    researchers, input_rejections = unique_research_paths(researchers)
    if assessments is None:
        assessments = {r["researcher_id"]: forecast_assessment(
            r, manifest, oos_evidence_audit=audit_oos_result(r, manifest, bars) if bars is not None else None,
        ) for r in researchers}
    else:
        supplied_assessments = assessments
        assessments = {}
        for result in researchers:
            name = result["researcher_id"]
            assessment = supplied_assessments.get(name)
            if isinstance(assessment, dict):
                assessments[name] = assessment
            else:
                input_rejections.append({"researcher_id": name, "reason": "path_assessment_missing_or_invalid"})
    oos_probability_quality, skill_weight_validation = build_probability_validation(
        researchers, manifest, bars=bars, assessments=assessments, skill_epsilon=skill_epsilon,
    )
    valid = [
        r for r in researchers
        if r.get("result_role", "forecast") == "forecast"
        and r.get("status") in {"success", "partial"}
        and isinstance(r.get("prob_up"), (int, float))
        and not isinstance(r.get("prob_up"), bool)
        and math.isfinite(float(r["prob_up"])) and 0 <= r["prob_up"] <= 1
        and r.get("probability_source")
        and assessments.get(r["researcher_id"], {}).get("forecast_eligible") is True
        and not path_has_detected_leakage(r, assessments)
    ]
    probabilities = [float(r["prob_up"]) for r in valid]
    if not valid:
        return {
            "status": "unavailable", "weighting": "equal", "available_paths": 0,
            "economic_viability": _economic_viability([]),
            "oos_probability_quality": oos_probability_quality,
            "predictive_evidence_grade": _predictive_evidence_grade(oos_probability_quality),
            "confidence_score_kind": "heuristic_evidence_support",
            "skill_weight_validation": skill_weight_validation,
            "prob_up": None, "direction": "unavailable", "agreement": "unavailable",
            "diversity": "unavailable", "confidence": "very_low", "confidence_score": 0,
            "path_assessments": assessments, "calibration_status": "not_assessed",
            "path_input_rejections": input_rejections,
            "prediction": {"status": "unavailable", "raw_probability": None,
                           "calibrated_probability": None, "reportable_probability": None,
                           "probability_basis": "no_valid_path", "calibration_status": "not_assessed"},
            "reason_codes": ["PREDICTION_FAILED"],
            "warnings": ["No researcher produced a reportable probability; the Quant prediction is unavailable."]
                        + (["Ambiguous path IDs or missing assessments were excluded; see path_input_rejections."] if input_rejections else []),
        }

    probability = mean(probabilities)
    raw_values = [float(r.get("raw_probability", r["prob_up"])) for r in valid
                  if isinstance(r.get("raw_probability", r["prob_up"]), (int, float)) and not isinstance(r.get("raw_probability", r["prob_up"]), bool)]
    raw_probability = mean(raw_values) if len(raw_values) == len(valid) else probability
    spread = max(probabilities) - min(probabilities)
    dispersion = sample_std(probabilities) or 0.0
    if spread <= 0.05:
        agreement = "high"
    elif spread <= 0.15:
        agreement = "medium"
    elif spread <= 0.30:
        agreement = "high_disagreement"
    else:
        agreement = "extreme_disagreement"

    path_families = [_families(result) for result in valid]
    union = set().union(*path_families) if path_families else set()
    overlaps: list[float] = []
    for left_index in range(len(path_families)):
        for right_index in range(left_index + 1, len(path_families)):
            left, right = path_families[left_index], path_families[right_index]
            union_pair = left | right
            overlaps.append(len(left & right) / len(union_pair) if union_pair else 1.0)
    diversity_score = max(0.0, 1.0 - (sum(overlaps) / len(overlaps) if overlaps else 1.0))
    diversity = "high" if diversity_score >= 0.67 else "medium" if diversity_score >= 0.4 else "low"

    data_quality = manifest.get("data_quality", {})
    base_score = data_quality.get("confidence_score")
    if not isinstance(base_score, (int, float)):
        raw_score = data_quality.get("score")
        base_score = float(raw_score) * 100 if isinstance(raw_score, (int, float)) else {"high": 85, "medium": 65, "low": 35}.get(data_quality.get("overall"), 35)
    confidence_score = float(base_score)
    confidence_reasons: list[str] = []
    confidence_penalties: list[dict[str, Any]] = []
    path_assessments = [assessments.get(r["researcher_id"], {}) for r in valid]
    def deduct(points: int, reason: str) -> None:
        nonlocal confidence_score
        confidence_score -= points
        confidence_penalties.append({"points": points, "reason": reason})
        confidence_reasons.append(reason)
    if len(valid) < 2:
        deduct(10, "only_one_quant_path")
    calibration_statuses = [item.get("probability_calibration", "uncalibrated") for item in path_assessments]
    if "uncalibrated" in calibration_statuses:
        deduct(20, "uncalibrated_raw_probability")
    ensemble_calibration = oos_probability_quality.get("ensemble_calibration") or {}
    ensemble_calibrator_validated = ensemble_calibration.get("status") == "validated_on_chronological_holdout"
    if not ensemble_calibrator_validated:
        deduct(10, "ensemble_calibration_unavailable")
    oos_statuses = [(item.get("oos_validation") or {}).get("status", "insufficient") for item in path_assessments]
    if not oos_statuses or all(status in {"weak", "insufficient"} for status in oos_statuses):
        deduct(20, "oos_validation_insufficient")
    elif "weak" in oos_statuses or "insufficient" in oos_statuses:
        deduct(10, "some_oos_validation_weak")
    if any("LEAKAGE_AUDIT_INCOMPLETE" in (item.get("reason_codes") or []) for item in path_assessments):
        deduct(15, "leakage_audit_incomplete")
    if any("OOS_EVIDENCE_INVALID" in (item.get("reason_codes") or []) for item in path_assessments):
        deduct(20, "oos_evidence_invalid")
    if agreement == "medium":
        deduct(5, "moderate_path_disagreement")
    elif agreement == "high_disagreement":
        deduct(20, "high_path_disagreement")
    elif agreement == "extreme_disagreement":
        deduct(30, "extreme_path_disagreement")
    if diversity == "low" and len(valid) > 1:
        deduct(10, "low_evidence_family_diversity")
    policy_replay = oos_probability_quality.get("algorithm_policy_replay") or {}
    replay_count = policy_replay.get("non_overlapping_predicted_dates", policy_replay.get("predicted_dates", 0))
    replay_skill = policy_replay.get("brier_skill_vs_expanding_base_rate")
    skill_uncertainty = policy_replay.get("skill_uncertainty") or {}
    ci_low = skill_uncertainty.get("ci_low") if isinstance(skill_uncertainty, dict) else None
    ci_high = skill_uncertainty.get("ci_high") if isinstance(skill_uncertainty, dict) else None
    ci_estimated = (isinstance(ci_low, (int, float)) and not isinstance(ci_low, bool) and math.isfinite(float(ci_low))
                    and isinstance(ci_high, (int, float)) and not isinstance(ci_high, bool) and math.isfinite(float(ci_high)))
    if (not isinstance(replay_count, int) or isinstance(replay_count, bool) or replay_count < 20
            or isinstance(replay_skill, bool) or not isinstance(replay_skill, (int, float))
            or not math.isfinite(float(replay_skill))):
        deduct(10, "policy_oos_skill_evidence_insufficient")
    elif ci_estimated and float(ci_high) < 0:
        deduct(20, "policy_oos_skill_below_expanding_base_rate_interval")
    elif not ci_estimated and replay_skill <= 0:
        deduct(20, "policy_oos_not_better_than_expanding_base_rate")
    elif replay_count < 100:
        deduct(10, "policy_oos_skill_is_exploratory")
    elif not ci_estimated:
        deduct(10, "policy_oos_skill_uncertainty_unestimated")
    elif not (float(ci_low) > 0):
        deduct(10, "policy_oos_skill_uncertainty_includes_zero")
    confidence_score = max(0, min(100, round(confidence_score)))
    confidence = _confidence_label(confidence_score)

    warnings: list[str] = (["Ambiguous path IDs or missing assessments were excluded; see path_input_rejections."] if input_rejections else [])
    all_codes = [code for item in path_assessments for code in (item.get("reason_codes") or [])]
    if "uncalibrated" in calibration_statuses:
        warnings.append("Consensus includes raw uncalibrated model probabilities; calibration cannot be inferred from the path mean.")
    if ensemble_calibrator_validated:
        warnings.append("The ensemble probability carries a Platt calibrator validated on a chronological holdout of the walk-forward ledger; the holdout is retrospective and not fully independent of label autocorrelation.")
    else:
        warnings.append("The equal-weight consensus has no independently validated ensemble calibrator; its probability reliability is not established by path-level calibration alone.")
    if len(valid) < 2:
        warnings.append("Only one research path produced a probability; this is a single-path result, not an ensemble consensus.")
    if diversity == "low":
        warnings.append("Research paths rely on too few distinct evidence families; agreement may reflect shared exposure.")
    if agreement in {"high_disagreement", "extreme_disagreement"}:
        warnings.append("Researcher probabilities disagree materially; the average must be interpreted with low confidence.")
    for item in path_assessments:
        warnings.extend(item.get("warnings", []))

    calibrated_probability = None
    if ensemble_calibrator_validated:
        calibrator = ensemble_calibration.get("calibrator") or {}
        slope = calibrator.get("slope")
        intercept = calibrator.get("intercept")
        if (isinstance(slope, (int, float)) and not isinstance(slope, bool)
                and isinstance(intercept, (int, float)) and not isinstance(intercept, bool)):
            clipped = max(1e-6, min(1 - 1e-6, float(probability)))
            calibrated_probability = logistic(intercept + slope * math.log(clipped / (1 - clipped)))
    reportable_probability = calibrated_probability if calibrated_probability is not None else probability
    prediction = {
        "status": "available",
        "raw_probability": raw_probability,
        "calibrated_probability": calibrated_probability,
        "reportable_probability": reportable_probability,
        "probability_basis": ("ensemble_calibrated_equal_weight_mean_of_valid_path_estimates"
                              if calibrated_probability is not None else "equal_weight_mean_of_valid_path_estimates"),
        "calibration_status": ("ensemble_calibrated_on_chronological_holdout"
                               if calibrated_probability is not None else "ensemble_calibration_unavailable"),
    }
    economic = _economic_viability(valid)
    return {
        "status": "ensemble" if len(valid) >= 2 else "single_path",
        "weighting": "equal",
        "economic_viability": economic,
        "oos_probability_quality": oos_probability_quality,
        "skill_weight_validation": skill_weight_validation,
        "path_assessments": assessments,
        "path_input_rejections": input_rejections,
        "calibration_status": ("ensemble_calibrated_on_chronological_holdout"
                               if calibrated_probability is not None else "equal_weight_mean_not_ensemble_calibrated"),
        "available_paths": len(valid),
        "path_ids": [r["researcher_id"] for r in valid],
        "prob_up": reportable_probability,
        "probability": prediction,
        "prediction": prediction,
        "raw_probability": raw_probability,
        "calibrated_probability": calibrated_probability,
        "reportable_probability": reportable_probability,
        "direction": "bullish" if reportable_probability > 0.5 else "bearish" if reportable_probability < 0.5 else "neutral",
        "agreement": agreement,
        "probability_range": [min(probabilities), max(probabilities)],
        "probability_dispersion": dispersion,
        "direction_agreement_ratio": max(sum(p > 0.5 for p in probabilities), sum(p < 0.5 for p in probabilities), sum(p == 0.5 for p in probabilities)) / len(probabilities),
        "diversity": diversity,
        "evidence_families": sorted(union),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "confidence_score_kind": "heuristic_evidence_support",
        "predictive_evidence_grade": _predictive_evidence_grade(oos_probability_quality),
        "confidence_reasons": list(dict.fromkeys(confidence_reasons)),
        "confidence_penalties": confidence_penalties,
        "reason_codes": list(dict.fromkeys(all_codes)),
        "evidence_overlap": {"mean_pairwise_jaccard": (sum(overlaps) / len(overlaps)) if overlaps else None, "path_families": [sorted(items) for items in path_families]},
        "confidence_components": {"base_data_quality_score": round(float(base_score)),
                                  "data_quality_penalties": manifest.get("data_quality", {}).get("confidence_penalties", []),
                                  "final_score": confidence_score,
                                  "oos_validation": {key: assessments[key].get("oos_validation") for key in assessments},
                                  "calibration": {key: assessments[key].get("probability_calibration") for key in assessments},
                                  "ensemble_calibration": {key: ensemble_calibration.get(key) for key in ("status", "fit_dates", "validation_dates", "validation_improvement")},
                                  "policy_skill_uncertainty": {"estimated": ci_estimated,
                                                               "ci_low": float(ci_low) if ci_estimated else None,
                                                               "ci_high": (skill_uncertainty.get("ci_high") if ci_estimated else None)},
                                  "agreement": agreement, "diversity": diversity},
        "warnings": list(dict.fromkeys(warnings)),
    }
