"""Out-of-sample probability diagnostics and experimental temporal weight validation."""

from __future__ import annotations

import math
from typing import Any

from ..math_utils import mean


MINIMUM_WEIGHT_WINDOW_SAMPLES = 20
DEFAULT_BRIER_SKILL_EPSILON = 0.05


def _valid_probability(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0 <= float(value) <= 1)


def _validated_oos_rows(researchers: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    latest = (manifest.get("latest_confirmed_close") or {}).get("session_date")
    if not isinstance(latest, str):
        latest = str(manifest.get("asof_timestamp", ""))[:10]
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for path in researchers:
        if path.get("result_role", "forecast") != "forecast":
            continue
        if any(path.get(key) != manifest.get(key) for key in ("ticker", "horizon", "snapshot_id")):
            continue
        validation = path.get("validation") or {}
        source_rows = validation.get("oos_forecasts")
        if not isinstance(source_rows, list):
            continue
        rows: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            decision, label_end, target = row.get("decision_session"), row.get("label_end_session"), row.get("target")
            if (not isinstance(decision, str) or not isinstance(label_end, str)
                    or not isinstance(target, int) or isinstance(target, bool) or target not in (0, 1)
                    or decision > latest or label_end > latest or label_end < decision):
                continue
            if decision in rows:
                # Duplicate decision keys make common-window comparisons ambiguous.
                rows = {}
                break
            rows[decision] = {**row, "decision_session": decision, "label_end_session": label_end,
                              "target": target,
                              "target_definition": row.get("target_definition") or validation.get("target_definition")}
        if rows:
            result[str(path.get("researcher_id", "unknown"))] = rows
    return result


def _probability_metrics(rows: list[dict[str, Any]], *, denominator: int | None = None) -> dict[str, Any]:
    denominator_count = len(rows) if denominator is None else denominator
    valid = [row for row in rows if _valid_probability(row.get("probability"))]
    count = len(valid)
    brier = mean((float(row["probability"]) - int(row["target"])) ** 2 for row in valid)
    eps = 1e-15
    log_loss = mean(
        -(int(row["target"]) * math.log(max(eps, min(1 - eps, float(row["probability"]))))
          + (1 - int(row["target"])) * math.log(max(eps, min(1 - eps, 1 - float(row["probability"]))))
          ) for row in valid
    )
    bins = []
    ece_terms = []
    sparse_bins = []
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        group = [row for row in valid if lower <= float(row["probability"]) < upper
                 or index == 9 and float(row["probability"]) == 1]
        mean_probability = mean(float(row["probability"]) for row in group)
        observed_rate = mean(int(row["target"]) for row in group)
        bins.append({"lower_bound": lower, "upper_bound": upper, "count": len(group),
                     "mean_probability": mean_probability, "observed_positive_rate": observed_rate})
        if group:
            ece_terms.append(len(group) / count * abs(float(mean_probability) - float(observed_rate)))
        if len(group) < 10:
            sparse_bins.append(index)
    coverage = count / denominator_count if denominator_count else 0.0
    return {
        "all_evaluable_oos_samples": denominator_count,
        "valid_probability_predictions": count,
        "oos_coverage": coverage,
        "abstain_rate": 1 - coverage if denominator_count else None,
        "brier_score": brier,
        "log_loss": log_loss,
        "reliability_diagram": bins,
        "expected_calibration_error": sum(ece_terms) if ece_terms else None,
        "calibration_sample_count": count,
        "calibration_uncertainty": {
            "status": "insufficient_samples" if count < 20 else "descriptive_no_interval",
            "sparse_bins": sparse_bins,
            "interval_method": "not_computed",
        },
    }


def build_oos_probability_quality(researchers: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    rows_by_path = _validated_oos_rows(researchers, manifest)
    path_metrics = {
        name: _probability_metrics(list(rows.values()))
        for name, rows in rows_by_path.items()
    }
    names = sorted(rows_by_path)
    if len(names) < 2:
        return {
            "status": "not_assessed" if not names else "partial",
            "method": "Brier, log loss, ten equal-width reliability bins, ECE, coverage and abstain rate",
            "paths": path_metrics,
            "common_oos": {"common_oos_samples": 0, "reason": "fewer than two dated forecast paths supplied OOS records"},
        }
    common_dates = set.intersection(*(set(rows_by_path[name]) for name in names))
    compatible: list[str] = []
    for decision in sorted(common_dates):
        row_set = [rows_by_path[name][decision] for name in names]
        if (len({row["target"] for row in row_set}) == 1
                and len({row["label_end_session"] for row in row_set}) == 1
                and len({row.get("target_definition") for row in row_set}) == 1):
            compatible.append(decision)
    common_rows = {name: [rows_by_path[name][date] for date in compatible] for name in names}
    complete_dates = [date for date in compatible
                      if all(_valid_probability(rows_by_path[name][date].get("probability")) for name in names)]
    complete_rows = {name: [rows_by_path[name][date] for date in complete_dates] for name in names}
    return {
        "status": "available" if compatible else "not_assessed",
        "method": "Brier, log loss, ten equal-width reliability bins, ECE, coverage and abstain rate",
        "target_definition": rows_by_path[names[0]][compatible[0]].get("target_definition") if compatible else None,
        "paths": path_metrics,
        "common_oos": {
            "common_oos_samples": len(compatible),
            "common_oos_start": compatible[0] if compatible else None,
            "common_oos_end": compatible[-1] if compatible else None,
            "same_ticker_horizon_target_and_label_end": bool(compatible),
            "path_metrics_on_common_oos_dates": {
                name: _probability_metrics(common_rows[name], denominator=len(compatible)) for name in names
            },
            "all_paths_valid_prediction_samples": len(complete_dates),
            "complete_sample_start": complete_dates[0] if complete_dates else None,
            "complete_sample_end": complete_dates[-1] if complete_dates else None,
            "path_metrics_on_same_complete_samples": {
                name: _probability_metrics(complete_rows[name], denominator=len(complete_dates)) for name in names
            },
            "all_available_label_samples": len(compatible),
        },
    }


def build_brier_skill_weight_diagnostic(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
    minimum_per_window: int = MINIMUM_WEIGHT_WINDOW_SAMPLES,
) -> dict[str, Any]:
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("Brier skill epsilon must be finite and non-negative")
    if isinstance(minimum_per_window, bool) or not isinstance(minimum_per_window, int) or minimum_per_window < 2:
        raise ValueError("minimum_per_window must be an integer of at least 2")
    rows_by_path = _validated_oos_rows(researchers, manifest)
    names = sorted(rows_by_path)
    all_dates = set.intersection(*(set(rows_by_path[name]) for name in names)) if names else set()
    common_dates = []
    for decision in sorted(all_dates):
        rows = [rows_by_path[name][decision] for name in names]
        if (len({row["target"] for row in rows}) == 1
                and len({row["label_end_session"] for row in rows}) == 1
                and len({row.get("target_definition") for row in rows}) == 1):
            common_dates.append(decision)
    complete_dates = [decision for decision in common_dates
                      if names and all(_valid_probability(rows_by_path[name][decision].get("probability")) for name in names)]
    base = {
        "status": "fallback_equal_weight",
        "weighting": "equal",
        "current_weighting_enabled": False,
        "deployment_status": "disabled_pending_independent_real_snapshot_oos_and_performance_acceptance",
        "method": "Brier Skill weights estimated on earlier common OOS rows and evaluated on later common OOS rows",
        "skill_formula": "max(0, (baseline_brier - path_brier) / baseline_brier)",
        "epsilon": float(epsilon),
        "minimum_samples_per_window": minimum_per_window,
        "common_oos_samples": len(common_dates),
        "common_complete_prediction_samples": len(complete_dates),
        "path_ids": names,
        "weight_estimation_start": None,
        "weight_estimation_end": None,
        "weight_estimation_label_end": None,
        "evaluation_start": None,
        "evaluation_end": None,
        "baseline_brier": None,
        "path_brier": {},
        "skill": {},
        "candidate_weights": {},
        "evaluation": {"status": "not_assessed", "weighted_brier": None, "equal_weight_brier": None, "baseline_brier": None},
        "fallback_reason": None,
    }
    if len(names) < 2:
        base["fallback_reason"] = "fewer_than_two_forecast_paths_with_dated_oos_records"
        return base
    if len(complete_dates) < 2 * minimum_per_window:
        base["fallback_reason"] = "insufficient_complete_common_oos_samples_for_separate_estimation_and_evaluation_windows"
        return base

    split = len(complete_dates) // 2
    estimation_dates, evaluation_dates = complete_dates[:split], complete_dates[split:]
    if len(estimation_dates) < minimum_per_window or len(evaluation_dates) < minimum_per_window:
        base["fallback_reason"] = "one_temporal_window_has_too_few_common_oos_samples"
        return base
    latest_estimation_label = max(rows_by_path[names[0]][date]["label_end_session"] for date in estimation_dates)
    first_evaluation_decision = evaluation_dates[0]
    if latest_estimation_label >= first_evaluation_decision:
        base["fallback_reason"] = "estimation_labels_not_observable_before_independent_evaluation_window"
        return base

    estimation_targets = [rows_by_path[names[0]][date]["target"] for date in estimation_dates]
    base_rate = mean(estimation_targets)
    baseline_brier = mean((float(base_rate) - target) ** 2 for target in estimation_targets) if base_rate is not None else None
    if baseline_brier is None or baseline_brier <= 0:
        base["fallback_reason"] = "estimation_window_base_rate_brier_is_invalid_or_zero"
        return base
    path_brier: dict[str, float] = {}
    for name in names:
        score = mean((float(rows_by_path[name][date]["probability"]) - rows_by_path[name][date]["target"]) ** 2
                     for date in estimation_dates)
        if score is None or not math.isfinite(score):
            base["fallback_reason"] = f"invalid_estimation_brier_for_path:{name}"
            return base
        path_brier[name] = score
    skills = {name: max(0.0, (baseline_brier - path_brier[name]) / baseline_brier) for name in names}
    numerators = {name: skills[name] + float(epsilon) for name in names}
    denominator = sum(numerators.values())
    if denominator <= 0 or not math.isfinite(denominator):
        base["fallback_reason"] = "all_candidate_weights_are_zero"
        return base
    weights = {name: value / denominator for name, value in numerators.items()}

    evaluation_targets = [rows_by_path[names[0]][date]["target"] for date in evaluation_dates]
    weighted_probs = [sum(weights[name] * float(rows_by_path[name][date]["probability"]) for name in names)
                      for date in evaluation_dates]
    equal_probs = [sum(float(rows_by_path[name][date]["probability"]) for name in names) / len(names)
                   for date in evaluation_dates]
    evaluation_base_brier = mean((float(base_rate) - target) ** 2 for target in evaluation_targets)
    evaluation_path_brier = {
        name: mean((float(rows_by_path[name][date]["probability"]) - rows_by_path[name][date]["target"]) ** 2
                   for date in evaluation_dates)
        for name in names
    }
    weighted_brier = mean((probability - target) ** 2 for probability, target in zip(weighted_probs, evaluation_targets))
    equal_brier = mean((probability - target) ** 2 for probability, target in zip(equal_probs, evaluation_targets))
    base.update({
        "status": "exploratory_temporal_validation",
        "weight_estimation_start": estimation_dates[0],
        "weight_estimation_end": estimation_dates[-1],
        "weight_estimation_label_end": latest_estimation_label,
        "evaluation_start": evaluation_dates[0],
        "evaluation_end": evaluation_dates[-1],
        "estimation_samples": len(estimation_dates),
        "evaluation_samples": len(evaluation_dates),
        "estimation_base_rate": base_rate,
        "baseline_brier": baseline_brier,
        "path_brier": path_brier,
        "skill": skills,
        "candidate_weights": weights,
        "maximum_candidate_weight": max(weights.values()),
        "concentration_risk": "high" if max(weights.values()) >= .8 else "not_flagged_by_heuristic",
        "fallback_reason": None,
        "evaluation": {
            "status": "available",
            "weighted_brier": weighted_brier,
            "equal_weight_brier": equal_brier,
            "baseline_brier": evaluation_base_brier,
            "path_brier": evaluation_path_brier,
            "weighted_beats_equal_weight": weighted_brier < equal_brier if weighted_brier is not None and equal_brier is not None else None,
            "weighted_beats_estimation_base_rate": weighted_brier < evaluation_base_brier if weighted_brier is not None and evaluation_base_brier is not None else None,
            "evaluation_samples": len(evaluation_dates),
        },
    })
    return base


def build_probability_validation(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    skill_epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
    minimum_weight_window_samples: int = MINIMUM_WEIGHT_WINDOW_SAMPLES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_oos_probability_quality(researchers, manifest),
        build_brier_skill_weight_diagnostic(
            researchers, manifest, epsilon=skill_epsilon,
            minimum_per_window=minimum_weight_window_samples,
        ),
    )
