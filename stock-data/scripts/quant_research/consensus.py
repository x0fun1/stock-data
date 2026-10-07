"""Probability-weighted cross-check and confidence calculation."""

from __future__ import annotations

import math
from typing import Any

from .math_utils import mean, sample_std


def forecast_assessment(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    probability = result.get("prob_up")
    if result.get("result_role", "forecast") != "forecast":
        return {"forecast_eligible": False, "forecast_exclusion_reasons": ["diagnostic-only path"], "probability_calibration": "not_applicable"}
    if not manifest.get("data_validation", {}).get("forecast_eligible", False):
        reasons.append("market data did not pass reporting gates")
    if any(result.get(key) != manifest.get(key) for key in ("ticker", "horizon", "snapshot_id")):
        reasons.append("research path identity differs from the snapshot")
    if result.get("status") not in {"success", "partial"} or isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1 or not result.get("probability_source"):
        reasons.append("no contract-valid quantitative probability")
    validation = result.get("validation") or {}
    name = result.get("researcher_id")
    calibration = "empirical_frequency" if name in {"quant", "factor"} else "calibrated" if validation.get("probability_calibration", {}).get("calibrated") else "uncalibrated"
    leakage = validation.get("leakage_audit") or {}
    if leakage.get("features_use_data_through_decision_close_only") is not True or leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features")) is not True:
        reasons.append("feature/label timing evidence missing")
    count = validation.get("oos_predictions", 0) if name == "quant" else validation.get("holdout_predictions", 0) if name == "factor" else validation.get("final_holdout", {}).get("observations", validation.get("final_holdout", {}).get("n", 0))
    minimum = 20 if name in {"quant", "ml"} else 10
    if manifest.get("request", {}).get("mode") == "strict":
        if not isinstance(count, int) or count < minimum:
            reasons.append(f"strict mode requires at least {minimum} chronological OOS predictions")
        if calibration == "uncalibrated":
            reasons.append("strict mode excludes raw uncalibrated model probabilities")
    return {"forecast_eligible": not reasons, "forecast_exclusion_reasons": reasons, "probability_calibration": calibration}


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


def _oos_validation_quality(result: dict[str, Any]) -> float:
    validation = result.get("validation") or {}
    candidates: list[tuple[float, float]] = []
    def collect(value: Any) -> None:
        if isinstance(value, dict):
            brier = value.get("brier", value.get("holdout_brier"))
            baseline = value.get("base_rate_brier", value.get("holdout_base_rate_brier"))
            if isinstance(brier, (int, float)) and isinstance(baseline, (int, float)) and math.isfinite(float(brier)) and math.isfinite(float(baseline)) and baseline > 0:
                candidates.append((float(brier), float(baseline)))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(validation)
    if not candidates:
        return 0.35
    quality = [0.85 if score < base else 0.45 for score, base in candidates]
    return sum(quality) / len(quality)


def build_consensus(researchers: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    assessments = {r["researcher_id"]: forecast_assessment(r, manifest) for r in researchers}
    valid = [
        r for r in researchers
        if r.get("result_role", "forecast") == "forecast"
        and r.get("status") in {"success", "partial"}
        and isinstance(r.get("prob_up"), (int, float))
        and r.get("probability_source")
        and assessments[r["researcher_id"]]["forecast_eligible"]
    ]
    probabilities = [float(r["prob_up"]) for r in valid]
    if not valid:
        return {"status": "unavailable", "weighting": "equal", "available_paths": 0, "prob_up": None, "direction": "unavailable", "agreement": "unavailable", "diversity": "unavailable", "confidence": "very_low", "path_assessments": assessments, "calibration_status": "not_assessed", "warnings": ["No researcher supplied a reporting-eligible quantitative probability."]}

    probability = mean(probabilities)
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
    if diversity_score >= 0.67:
        diversity = "high"
    elif diversity_score >= 0.4:
        diversity = "medium"
    else:
        diversity = "low"
    validation_quality = mean([_oos_validation_quality(result) for result in valid]) or 0.35
    data_quality = manifest.get("data_quality", {})
    data_score = data_quality.get("score")
    if not isinstance(data_score, (int, float)):
        data_score = {"high": 0.85, "medium": 0.65, "low": 0.35}.get(data_quality.get("overall"), 0.35)
    agreement_score = max(0.0, 1.0 - dispersion / 0.25)
    confidence_score = agreement_score * validation_quality * diversity_score * float(data_score)
    confidence = "high" if confidence_score >= 0.6 else "medium" if confidence_score >= 0.35 else "low" if confidence_score >= 0.15 else "very_low"
    if agreement in {"high_disagreement", "extreme_disagreement"}:
        confidence = "very_low" if confidence in {"low", "very_low"} else "low"
    warnings: list[str] = []
    if any(assessments[r["researcher_id"]]["probability_calibration"] == "uncalibrated" for r in valid):
        warnings.append("Consensus includes raw uncalibrated model probabilities; the equal-weight mean is not calibrated.")
    if len(valid) < 2:
        warnings.append("Only one research path produced a probability; this is a single-path result, not an ensemble consensus.")
    if diversity == "low":
        warnings.append("Research paths rely on too few distinct evidence families; agreement may reflect shared exposure.")
    if agreement in {"high_disagreement", "extreme_disagreement"}:
        warnings.append("Researcher probabilities disagree materially; the average must be interpreted with low confidence.")
    return {
        "status": "ensemble" if len(valid) >= 2 else "single_path",
        "weighting": "equal",
        "path_assessments": assessments,
        "calibration_status": "equal_weight_mean_not_ensemble_calibrated",
        "available_paths": len(valid),
        "path_ids": [r["researcher_id"] for r in valid],
        "prob_up": probability,
        "direction": "bullish" if probability is not None and probability > 0.5 else "bearish" if probability is not None and probability < 0.5 else "neutral" if probability is not None else "unavailable",
        "agreement": agreement,
        "probability_range": [min(probabilities), max(probabilities)],
        "probability_dispersion": dispersion,
        "direction_agreement_ratio": max(sum(p > 0.5 for p in probabilities), sum(p < 0.5 for p in probabilities), sum(p == 0.5 for p in probabilities)) / len(probabilities),
        "diversity": diversity,
        "evidence_families": sorted(union),
        "confidence": confidence,
        "evidence_overlap": {"mean_pairwise_jaccard": (sum(overlaps) / len(overlaps)) if overlaps else None, "path_families": [sorted(items) for items in path_families]},
        "confidence_components": {"agreement": round(agreement_score, 4), "historical_reliability": None, "diversity": round(diversity_score, 4), "data_quality": round(float(data_score), 4), "validation_quality": round(validation_quality, 4)},
        "warnings": warnings,
    }
