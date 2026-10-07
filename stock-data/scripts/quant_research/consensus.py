"""Probability estimates, validation metadata, and explanatory confidence scoring."""

from __future__ import annotations

import math
from typing import Any

from .math_utils import mean, sample_std


def _oos_count(result: dict[str, Any]) -> int:
    validation = result.get("validation") or {}
    name = result.get("researcher_id")
    value: Any = (validation.get("oos_predictions", 0) if name == "quant"
                  else validation.get("holdout_predictions", 0) if name == "factor"
                  else validation.get("final_holdout", {}).get("observations", validation.get("final_holdout", {}).get("n", 0)))
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def forecast_assessment(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
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

    count = _oos_count(result)
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
        messages.append(f"Chronological OOS support is {oos_status} (N={count}); this lowers confidence but does not erase the forecast.")
    if calibration == "uncalibrated":
        codes.append("CALIBRATION_MISSING")
        messages.append("Probability calibration is unavailable; the raw model probability is retained with lower confidence.")
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


def build_consensus(researchers: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    assessments = {r["researcher_id"]: forecast_assessment(r, manifest) for r in researchers}
    valid = [
        r for r in researchers
        if r.get("result_role", "forecast") == "forecast"
        and r.get("status") in {"success", "partial"}
        and isinstance(r.get("prob_up"), (int, float))
        and not isinstance(r.get("prob_up"), bool)
        and r.get("probability_source")
        and assessments[r["researcher_id"]]["forecast_eligible"]
    ]
    probabilities = [float(r["prob_up"]) for r in valid]
    if not valid:
        return {
            "status": "unavailable", "weighting": "equal", "available_paths": 0,
            "prob_up": None, "direction": "unavailable", "agreement": "unavailable",
            "diversity": "unavailable", "confidence": "very_low", "confidence_score": 0,
            "path_assessments": assessments, "calibration_status": "not_assessed",
            "prediction": {"status": "unavailable", "raw_probability": None,
                           "calibrated_probability": None, "reportable_probability": None,
                           "probability_basis": "no_valid_path", "calibration_status": "not_assessed"},
            "reason_codes": ["PREDICTION_FAILED"],
            "warnings": ["No researcher produced a reportable probability; the Quant prediction is unavailable."],
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
    path_assessments = [assessments[r["researcher_id"]] for r in valid]
    def deduct(points: int, reason: str) -> None:
        nonlocal confidence_score
        confidence_score -= points
        confidence_penalties.append({"points": points, "reason": reason})
        confidence_reasons.append(reason)
    if len(valid) < 2:
        deduct(10, "only_one_quant_path")
    calibration_statuses = [item["probability_calibration"] for item in path_assessments]
    if "uncalibrated" in calibration_statuses:
        deduct(20, "uncalibrated_raw_probability")
    oos_statuses = [item["oos_validation"]["status"] for item in path_assessments]
    if not oos_statuses or all(status in {"weak", "insufficient"} for status in oos_statuses):
        deduct(20, "oos_validation_insufficient")
    elif "weak" in oos_statuses or "insufficient" in oos_statuses:
        deduct(10, "some_oos_validation_weak")
    if any("LEAKAGE_AUDIT_INCOMPLETE" in item["reason_codes"] for item in path_assessments):
        deduct(15, "leakage_audit_incomplete")
    if agreement == "medium":
        deduct(5, "moderate_path_disagreement")
    elif agreement == "high_disagreement":
        deduct(20, "high_path_disagreement")
    elif agreement == "extreme_disagreement":
        deduct(30, "extreme_path_disagreement")
    if diversity == "low" and len(valid) > 1:
        deduct(10, "low_evidence_family_diversity")
    confidence_score = max(0, min(100, round(confidence_score)))
    confidence = _confidence_label(confidence_score)

    warnings: list[str] = []
    all_codes = [code for item in path_assessments for code in item["reason_codes"]]
    if any(item["probability_calibration"] == "uncalibrated" for item in path_assessments):
        warnings.append("Consensus includes raw uncalibrated model probabilities; calibration cannot be inferred from the path mean.")
    if len(valid) < 2:
        warnings.append("Only one research path produced a probability; this is a single-path result, not an ensemble consensus.")
    if diversity == "low":
        warnings.append("Research paths rely on too few distinct evidence families; agreement may reflect shared exposure.")
    if agreement in {"high_disagreement", "extreme_disagreement"}:
        warnings.append("Researcher probabilities disagree materially; the average must be interpreted with low confidence.")
    for item in path_assessments:
        warnings.extend(item.get("warnings", []))

    prediction = {
        "status": "available",
        "raw_probability": raw_probability,
        "calibrated_probability": None,
        "reportable_probability": probability,
        "probability_basis": "equal_weight_mean_of_valid_path_estimates",
        "calibration_status": "ensemble_calibration_unavailable",
    }
    return {
        "status": "ensemble" if len(valid) >= 2 else "single_path",
        "weighting": "equal",
        "path_assessments": assessments,
        "calibration_status": "equal_weight_mean_not_ensemble_calibrated",
        "available_paths": len(valid),
        "path_ids": [r["researcher_id"] for r in valid],
        "prob_up": probability,
        "probability": prediction,
        "prediction": prediction,
        "raw_probability": raw_probability,
        "calibrated_probability": None,
        "reportable_probability": probability,
        "direction": "bullish" if probability > 0.5 else "bearish" if probability < 0.5 else "neutral",
        "agreement": agreement,
        "probability_range": [min(probabilities), max(probabilities)],
        "probability_dispersion": dispersion,
        "direction_agreement_ratio": max(sum(p > 0.5 for p in probabilities), sum(p < 0.5 for p in probabilities), sum(p == 0.5 for p in probabilities)) / len(probabilities),
        "diversity": diversity,
        "evidence_families": sorted(union),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "confidence_reasons": list(dict.fromkeys(confidence_reasons)),
        "confidence_penalties": confidence_penalties,
        "reason_codes": list(dict.fromkeys(all_codes)),
        "evidence_overlap": {"mean_pairwise_jaccard": (sum(overlaps) / len(overlaps)) if overlaps else None, "path_families": [sorted(items) for items in path_families]},
        "confidence_components": {"base_data_quality_score": round(float(base_score)),
                                  "data_quality_penalties": manifest.get("data_quality", {}).get("confidence_penalties", []),
                                  "final_score": confidence_score,
                                  "oos_validation": {key: assessments[key].get("oos_validation") for key in assessments},
                                  "calibration": {key: assessments[key].get("probability_calibration") for key in assessments},
                                  "agreement": agreement, "diversity": diversity},
        "warnings": list(dict.fromkeys(warnings)),
    }