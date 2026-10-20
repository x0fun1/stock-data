"""Probability estimates, validation metadata, and explanatory confidence scoring."""

from __future__ import annotations

import math
from typing import Any

from .math_utils import logistic, mean, sample_std
from .validation.probability_quality import (
    DEFAULT_BRIER_SKILL_EPSILON,
    MIN_BOOTSTRAP_INDEPENDENT_BLOCKS,
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
    support_sessions: int | None = None,
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
    # A path that declares itself abstained must not vote, and this is checked here rather
    # than trusted from the path: otherwise a path could report ``route: abstained`` and a
    # numeric ``prob_up`` at the same time, and consensus would silently count it.
    declared_route = (validation.get("qualification") or {}).get("route")
    if declared_route == "abstained" and isinstance(probability, (int, float)) and not isinstance(probability, bool):
        codes.append("QUALIFICATION_CONTRACT_VIOLATION")
        messages.append(
            "The path declared its qualification route 'abstained' but still reported a numeric "
            "probability; the probability is rejected."
        )
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
    thresholds = oos_support_thresholds(_horizon_sessions(manifest), support_sessions)
    if count >= thresholds["strong"]:
        oos_status = "strong"
    elif count >= thresholds["acceptable"]:
        oos_status = "acceptable"
    elif count >= thresholds["weak"]:
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
    fatal_codes = [code for code in codes if code in {
        "SNAPSHOT_IDENTITY_INVALID", "PREDICTION_FAILED", "LEAKAGE_DETECTED",
        "QUALIFICATION_CONTRACT_VIOLATION",
    }]
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


def _horizon_sessions(manifest: dict[str, Any]) -> int:
    digits = "".join(ch for ch in str(manifest.get("horizon", "5D")) if ch.isdigit())
    horizon = int(digits) if digits else 5
    return horizon if horizon >= 1 else 5


DEFAULT_SUPPORT_SESSIONS = 252
MIN_STRONG_SUPPORT = 20
SUPPORT_SCALING_FLOOR = DEFAULT_SUPPORT_SESSIONS * 3 // 4


def support_basis(support_sessions: int | None) -> dict[str, Any]:
    """Which ledger size the OOS tiers were derived from.

    Scaling keeps every tier attainable on a 52-week ledger, but it also means a
    ``strong`` label is relative to the attainable budget rather than an absolute
    sample count. Callers must publish the basis next to the thresholds.
    """
    usable = (isinstance(support_sessions, int) and not isinstance(support_sessions, bool)
              and support_sessions >= SUPPORT_SCALING_FLOOR)
    sessions_used = min(DEFAULT_SUPPORT_SESSIONS, support_sessions) if usable else DEFAULT_SUPPORT_SESSIONS
    return {
        "sessions_used": sessions_used,
        "nominal_sessions": DEFAULT_SUPPORT_SESSIONS,
        "scaled": usable and sessions_used < DEFAULT_SUPPORT_SESSIONS,
        "scaling_floor": SUPPORT_SCALING_FLOOR,
    }


def oos_support_thresholds(horizon_sessions: int,
                           support_sessions: int | None = None) -> dict[str, int]:
    """Non-overlapping OOS sample targets scaled to the research window actually available.

    The nominal one-year window (252 sessions) defines full support: `strong` is ~90% of its
    non-overlapping budget (floor 20 so short horizons still need a working sample), with
    `acceptable` at ~55% and `weak` at ~30% of that target. A 52-week window holds fewer
    sessions than the nominal count once holidays and the label tail are removed, so deriving
    the budget from 252 alone can push `strong` above the number of samples the window can ever
    produce. `support_sessions` is the widest non-overlapping ledger the run actually has; the
    targets are scaled to it and capped at its budget so every tier stays attainable. Longer
    windows are capped at the nominal window, so extra history never lowers the bar. Thresholds
    use non-overlapping counts.

    Scaling only applies once the ledger covers at least three quarters of the nominal window,
    which is the holiday and label-tail shortfall of a one-year study (a 52-week window yields
    about 220 label dates). A materially shorter study (for example a recent listing that cannot
    supply a year of history) keeps the nominal tiers and therefore reports insufficient support:
    borrowing the relative standard there would award `strong` on a handful of samples, which is
    the opposite of what the label is supposed to certify.
    """
    sessions = DEFAULT_SUPPORT_SESSIONS
    if (isinstance(support_sessions, int) and not isinstance(support_sessions, bool)
            and support_sessions >= SUPPORT_SCALING_FLOOR):
        sessions = min(DEFAULT_SUPPORT_SESSIONS, support_sessions)
    budget = max(1, sessions // (horizon_sessions + 1))
    strong = min(budget, max(MIN_STRONG_SUPPORT, budget - budget // 10))
    acceptable = min(strong, max(10, (strong * 55) // 100))
    weak = min(acceptable, max(5, (strong * 30) // 100))
    return {"strong": strong, "acceptable": acceptable, "weak": weak}


def oos_support_sessions(results: list[dict[str, Any]]) -> int | None:
    """Widest OOS label ledger across paths: the shared ceiling for non-overlapping support."""
    widest = 0
    for result in results:
        audit = result.get("oos_evidence_audit") or {}
        if not isinstance(audit, dict):
            continue
        for key in ("supplied_samples", "valid_samples"):
            value = audit.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > widest:
                widest = value
    return widest or None


def _quality_support_sessions(quality: dict[str, Any]) -> int | None:
    """Same ceiling read back from the aggregated OOS quality block."""
    paths = (quality or {}).get("paths") or {}
    widest = 0
    for row in paths.values():
        if not isinstance(row, dict):
            continue
        value = row.get("all_evaluable_oos_samples")
        if isinstance(value, int) and not isinstance(value, bool) and value > widest:
            widest = value
    return widest or None


def _predictive_evidence_grade(quality: dict[str, Any], manifest: dict[str, Any],
                               support_sessions: int | None = None) -> str:
    replay = quality.get("algorithm_policy_replay") or {}
    support = support_sessions if support_sessions is not None else _quality_support_sessions(quality)
    thresholds = oos_support_thresholds(_horizon_sessions(manifest), support)
    count = replay.get("non_overlapping_predicted_dates", replay.get("predicted_dates", 0))
    skill = replay.get("brier_skill_vs_expanding_base_rate")
    if (not isinstance(count, int) or isinstance(count, bool) or count < thresholds["weak"]
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
    if count < thresholds["strong"]:
        return "exploratory_positive_skill_unconfirmed"
    if not ci_valid:
        return "positive_skill_point_estimate_uncertainty_unestimated"
    if float(ci_low) > 0:
        return "positive_skill_with_estimated_interval_excluding_zero"
    return "positive_skill_with_estimated_interval_including_zero"


def _confidence_composite(*, base_score: float, path_assessments: list[dict[str, Any]], valid_count: int,
                          oos_probability_quality: dict[str, Any], agreement: str, diversity: str,
                          manifest: dict[str, Any], support_sessions: int | None = None) -> dict[str, Any]:
    """Blend evidence dimensions into the reported confidence score.

    Dimensions and weights (see references/reliability-gates.md): data integrity 0.20,
    OOS validation 0.25, policy replay skill 0.25, calibration 0.15, consensus robustness
    0.15. Each dimension scores 0-100, so closed gaps earn full credit and a fully
    validated study can reach the high band while thin or contradicted evidence cannot.
    """
    support = support_sessions if support_sessions is not None else _quality_support_sessions(oos_probability_quality)
    thresholds = oos_support_thresholds(_horizon_sessions(manifest), support)
    confidence_reasons: list[str] = []

    oos_status_points = {"strong": 100.0, "acceptable": 80.0, "weak": 45.0, "insufficient": 20.0, "invalid": 0.0}
    oos_statuses = [(item.get("oos_validation") or {}).get("status", "insufficient") for item in path_assessments]
    oos_component = (sum(oos_status_points.get(status, 20.0) for status in oos_statuses) / len(oos_statuses)) if oos_statuses else 0.0
    if not oos_statuses or all(status in {"weak", "insufficient"} for status in oos_statuses):
        confidence_reasons.append("oos_validation_insufficient")
    elif "weak" in oos_statuses or "insufficient" in oos_statuses:
        confidence_reasons.append("some_oos_validation_weak")

    calibration_statuses = [item.get("probability_calibration", "uncalibrated") for item in path_assessments]
    calibrated_fraction = (sum(1 for status in calibration_statuses if status in {"calibrated", "empirical_frequency"}) / len(calibration_statuses)) if calibration_statuses else 0.0
    ensemble_calibration = oos_probability_quality.get("ensemble_calibration") or {}
    # The calibrator is *selected* on a retrospective ledger check: the block that
    # decides whether to keep it is also the block that reports the improvement. That
    # is not independent validation, so the field name must not say "validated".
    ensemble_calibrator_selected = ensemble_calibration.get("status") == "improved_on_subsequent_ledger_block"
    calibration_component = min(100.0, 40.0 + 45.0 * calibrated_fraction + (15.0 if ensemble_calibrator_selected else 0.0))
    if "uncalibrated" in calibration_statuses:
        confidence_reasons.append("uncalibrated_raw_probability")
    if not ensemble_calibrator_selected:
        confidence_reasons.append("ensemble_calibration_unavailable")

    if any("LEAKAGE_AUDIT_INCOMPLETE" in (item.get("reason_codes") or []) for item in path_assessments):
        oos_component = max(0.0, oos_component - 15.0)
        confidence_reasons.append("leakage_audit_incomplete")
    if any("OOS_EVIDENCE_INVALID" in (item.get("reason_codes") or []) for item in path_assessments):
        oos_component = max(0.0, oos_component - 10.0)
        confidence_reasons.append("oos_evidence_invalid")

    agreement_points = {"high": 100.0, "medium": 80.0, "high_disagreement": 45.0, "extreme_disagreement": 20.0}
    diversity_points = {"high": 100.0, "medium": 85.0, "low": 60.0}
    robustness_component = 0.75 * agreement_points.get(agreement, 60.0) + 0.25 * diversity_points.get(diversity, 60.0)
    if agreement == "high_disagreement":
        confidence_reasons.append("high_path_disagreement")
    elif agreement == "extreme_disagreement":
        confidence_reasons.append("extreme_path_disagreement")
    if diversity == "low" and valid_count > 1:
        confidence_reasons.append("low_evidence_family_diversity")

    policy_replay = oos_probability_quality.get("algorithm_policy_replay") or {}
    replay_count = policy_replay.get("non_overlapping_predicted_dates", policy_replay.get("predicted_dates", 0))
    replay_skill = policy_replay.get("brier_skill_vs_expanding_base_rate")
    skill_uncertainty = policy_replay.get("skill_uncertainty") or {}
    ci_low = skill_uncertainty.get("ci_low") if isinstance(skill_uncertainty, dict) else None
    ci_high = skill_uncertainty.get("ci_high") if isinstance(skill_uncertainty, dict) else None
    independent_blocks = skill_uncertainty.get("independent_blocks") if isinstance(skill_uncertainty, dict) else None
    ci_estimated = (isinstance(ci_low, (int, float)) and not isinstance(ci_low, bool) and math.isfinite(float(ci_low))
                    and isinstance(ci_high, (int, float)) and not isinstance(ci_high, bool) and math.isfinite(float(ci_high))
                    and isinstance(independent_blocks, int) and not isinstance(independent_blocks, bool)
                    and independent_blocks >= MIN_BOOTSTRAP_INDEPENDENT_BLOCKS)
    replay_component = 20.0
    if (not isinstance(replay_count, int) or isinstance(replay_count, bool) or replay_count < thresholds["weak"]
            or isinstance(replay_skill, bool) or not isinstance(replay_skill, (int, float))
            or not math.isfinite(float(replay_skill))):
        replay_component = 20.0
        confidence_reasons.append("policy_oos_skill_evidence_insufficient")
    elif ci_estimated and float(ci_high) < 0:
        replay_component = 10.0
        confidence_reasons.append("policy_oos_skill_below_expanding_base_rate_interval")
    elif replay_skill <= 0:
        replay_component = 15.0
        confidence_reasons.append("policy_oos_not_better_than_expanding_base_rate")
    elif ci_estimated and float(ci_low) > 0:
        # Full credit is earned by a block-bootstrap interval that excludes zero; sample
        # breadth below the full-support target still shows as a caveat reason.
        replay_component = 100.0
        if replay_count < thresholds["strong"]:
            confidence_reasons.append("policy_oos_skill_is_exploratory")
    else:
        replay_component = 65.0
        if not ci_estimated:
            confidence_reasons.append("policy_oos_skill_uncertainty_unestimated")
        else:
            confidence_reasons.append("policy_oos_skill_uncertainty_includes_zero")

    component_weights = {"data_integrity": 0.20, "oos_validation": 0.25, "policy_replay": 0.25,
                         "calibration": 0.15, "consensus_robustness": 0.15}
    component_scores = {"data_integrity": max(0.0, min(100.0, float(base_score))), "oos_validation": oos_component,
                        "policy_replay": replay_component, "calibration": calibration_component,
                        "consensus_robustness": robustness_component}
    confidence_score = round(sum(component_weights[key] * component_scores[key] for key in component_weights))
    if valid_count < 2:
        confidence_score = min(confidence_score, 74)
        confidence_reasons.append("only_one_quant_path")
    if ci_estimated and float(ci_high) < 0:
        confidence_score = min(confidence_score, 64)
    elif (isinstance(replay_skill, (int, float)) and not isinstance(replay_skill, bool)
          and math.isfinite(float(replay_skill)) and replay_skill <= 0):
        confidence_score = min(confidence_score, 74)
    confidence_score = max(0, min(100, confidence_score))
    confidence_penalties = [{"points": round(component_weights[key] * (100.0 - component_scores[key])),
                             "reason": f"{key}_below_full_support"}
                            for key in component_weights if component_scores[key] < 100.0]
    return {"score": confidence_score, "label": _confidence_label(confidence_score),
            "reasons": list(dict.fromkeys(confidence_reasons)), "penalties": confidence_penalties,
            "thresholds": thresholds, "support_basis": support_basis(support),
            "component_scores": component_scores, "component_weights": component_weights,
            "calibration_statuses": calibration_statuses, "ensemble_calibration": ensemble_calibration,
            "ensemble_calibrator_selected_on_retrospective_check": ensemble_calibrator_selected,
            "independent_validation_status": "not_established",
            "ci_estimated": ci_estimated, "ci_low": ci_low, "ci_high": ci_high,
            "skill_uncertainty": skill_uncertainty}


def _ensemble_uncertainty(probabilities: list[float], path_dependency: dict[str, Any]) -> dict[str, Any]:
    """Say plainly that no probability interval is identified here.

    Three paths sharing the same label dates are not three independent draws, so a
    range over their point estimates is descriptive, not a confidence interval. The
    block exists to keep that absence machine-readable instead of letting a host
    invent an interval.
    """
    spread = [min(probabilities), max(probabilities)] if len(probabilities) >= 2 else None
    effective = path_dependency.get("effective_independent_paths")
    if len(probabilities) < 2:
        status, reason = "not_estimable", "single_path"
    elif path_dependency.get("status") != "assessed":
        status, reason = "not_estimable", "path_dependency_not_assessed"
    elif isinstance(effective, (int, float)) and not isinstance(effective, bool) and effective < 2:
        status, reason = "not_estimable", "effective_independent_paths_below_two"
    else:
        status, reason = "descriptive_range_only", "no_per_date_ensemble_ledger"
    return {
        "status": status,
        "method": "none",
        "interval": None,
        "path_probability_range": spread,
        "effective_independent_paths": effective,
        "reason": reason,
        "note": "A probability interval is not identified from a small set of correlated paths; the range is descriptive only.",
    }


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
        audits = {r["researcher_id"]: (audit_oos_result(r, manifest, bars) if bars is not None else None)
                  for r in researchers}
        support_sessions = oos_support_sessions(
            [{**r, "oos_evidence_audit": audits[r["researcher_id"]]} for r in researchers])
        assessments = {r["researcher_id"]: forecast_assessment(
            r, manifest, oos_evidence_audit=audits[r["researcher_id"]], support_sessions=support_sessions,
        ) for r in researchers}
    else:
        # Callers that supply assessments have already audited every forecast path, so the same
        # ledger ceiling is reused here. Per-path grading and the composite must never disagree
        # about how much evidence a run has.
        support_sessions = oos_support_sessions(researchers)
        supplied_assessments = assessments
        assessments = {}
        for result in researchers:
            name = result["researcher_id"]
            assessment = supplied_assessments.get(name)
            # Qualification is authoritative at this boundary too: a supplied/stale
            # assessment cannot make an explicitly abstained path vote or contribute
            # to eligible-path probability-quality diagnostics. Reassess it before
            # either consumer sees the supplied eligibility flag.
            if ((result.get("validation") or {}).get("qualification") or {}).get("route") == "abstained":
                assessments[name] = forecast_assessment(
                    result, manifest, oos_evidence_audit=result.get("oos_evidence_audit"),
                    support_sessions=support_sessions,
                )
            elif isinstance(assessment, dict):
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
            "predictive_evidence_grade": _predictive_evidence_grade(
                oos_probability_quality, manifest, support_sessions),
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
    path_assessments = [assessments.get(r["researcher_id"], {}) for r in valid]
    composite = _confidence_composite(base_score=float(base_score), path_assessments=path_assessments,
                                      valid_count=len(valid), oos_probability_quality=oos_probability_quality,
                                      agreement=agreement, diversity=diversity, manifest=manifest,
                                      support_sessions=support_sessions)
    confidence_score = composite["score"]
    confidence = composite["label"]
    confidence_reasons = list(composite["reasons"])
    confidence_penalties = composite["penalties"]
    thresholds = composite["thresholds"]
    component_scores = composite["component_scores"]
    component_weights = composite["component_weights"]
    calibration_statuses = composite["calibration_statuses"]
    ensemble_calibration = composite["ensemble_calibration"]
    ensemble_calibrator_selected = composite["ensemble_calibrator_selected_on_retrospective_check"]
    ci_estimated = composite["ci_estimated"]
    ci_low = composite["ci_low"]
    ci_high = composite["ci_high"]

    warnings: list[str] = (["Ambiguous path IDs or missing assessments were excluded; see path_input_rejections."] if input_rejections else [])
    all_codes = [code for item in path_assessments for code in (item.get("reason_codes") or [])]
    if "uncalibrated" in calibration_statuses:
        warnings.append("Consensus includes raw uncalibrated model probabilities; calibration cannot be inferred from the path mean.")
    if ensemble_calibrator_selected:
        warnings.append("The ensemble probability carries a Platt calibrator estimated on the earliest 60% of the walk-forward ledger and checked on later ledger dates; the check block overlaps windows already used by path-level evaluation, so the improvement is descriptive rather than an independent confirmation.")
    else:
        warnings.append("The equal-weight consensus has no ensemble calibrator that improved the subsequent ledger check; its probability reliability is not established by path-level calibration alone.")
    if len(valid) < 2:
        warnings.append("Only one research path produced a probability; this is a single-path result, not an ensemble consensus.")
    if diversity == "low":
        warnings.append("Research paths rely on too few distinct evidence families; agreement may reflect shared exposure.")
    if agreement in {"high_disagreement", "extreme_disagreement"}:
        warnings.append("Researcher probabilities disagree materially; the average must be interpreted with low confidence.")
    for item in path_assessments:
        warnings.extend(item.get("warnings", []))

    calibrated_probability = None
    if ensemble_calibrator_selected:
        calibrator = ensemble_calibration.get("calibrator") or {}
        slope = calibrator.get("slope")
        intercept = calibrator.get("intercept")
        if (isinstance(slope, (int, float)) and not isinstance(slope, bool)
                and isinstance(intercept, (int, float)) and not isinstance(intercept, bool)):
            clipped = max(1e-6, min(1 - 1e-6, float(probability)))
            calibrated_probability = logistic(intercept + slope * math.log(clipped / (1 - clipped)))
    reportable_probability = calibrated_probability if calibrated_probability is not None else probability
    path_dependency = (oos_probability_quality.get("path_dependency") or {})
    ensemble_uncertainty = _ensemble_uncertainty(probabilities, path_dependency)
    prediction = {
        "status": "available",
        "raw_probability": raw_probability,
        "calibrated_probability": calibrated_probability,
        "reportable_probability": reportable_probability,
        "probability_basis": ("ensemble_calibrated_equal_weight_mean_of_valid_path_estimates"
                              if calibrated_probability is not None else "equal_weight_mean_of_valid_path_estimates"),
        "calibration_status": ("ensemble_calibrated_on_retrospective_ledger_check"
                               if calibrated_probability is not None else "ensemble_calibration_unavailable"),
        "ensemble_uncertainty": ensemble_uncertainty,
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
        "calibration_status": ("ensemble_calibrated_on_retrospective_ledger_check"
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
        "ensemble_uncertainty": ensemble_uncertainty,
        "path_dependency": path_dependency,
        "direction_agreement_ratio": max(sum(p > 0.5 for p in probabilities), sum(p < 0.5 for p in probabilities), sum(p == 0.5 for p in probabilities)) / len(probabilities),
        "diversity": diversity,
        "evidence_families": sorted(union),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "confidence_score_kind": "heuristic_evidence_support",
        "predictive_evidence_grade": _predictive_evidence_grade(
            oos_probability_quality, manifest, support_sessions),
        "confidence_reasons": list(dict.fromkeys(confidence_reasons)),
        "confidence_penalties": confidence_penalties,
        "reason_codes": list(dict.fromkeys(all_codes)),
        "evidence_overlap": {"mean_pairwise_jaccard": (sum(overlaps) / len(overlaps)) if overlaps else None, "path_families": [sorted(items) for items in path_families]},
        "confidence_components": {"base_data_quality_score": round(float(base_score)),
                                  "data_quality_penalties": data_quality.get("confidence_penalties", []),
                                  "component_scores": {key: round(component_scores[key]) for key in component_scores},
                                  "component_weights": component_weights,
                                  "oos_support_thresholds": thresholds,
                                  "support_basis": support_basis(support_sessions),
                                  "final_score": confidence_score,
                                  "oos_validation": {key: assessments[key].get("oos_validation") for key in assessments},
                                  "calibration": {key: assessments[key].get("probability_calibration") for key in assessments},
                                  "ensemble_calibration": {key: ensemble_calibration.get(key) for key in ("status", "fit_dates", "validation_dates", "validation_improvement")},
                                  "ensemble_calibrator_selected_on_retrospective_check": ensemble_calibrator_selected,
                                  "independent_validation_status": "not_established",
                                  "policy_skill_uncertainty": {"estimated": ci_estimated,
                                                               "ci_low": float(ci_low) if ci_estimated else None,
                                                               "ci_high": (float(ci_high) if ci_estimated else None)},
                                  "agreement": agreement, "diversity": diversity},
        "warnings": list(dict.fromkeys(warnings)),
    }
