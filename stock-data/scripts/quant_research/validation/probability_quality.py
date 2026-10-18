"""Out-of-sample probability diagnostics and experimental temporal weight validation."""

from __future__ import annotations

import math
import random
from datetime import date
from typing import Any

from ..contracts import HORIZONS
from ..market_data import forward_returns, non_overlapping
from ..math_utils import logistic, mean

MINIMUM_WEIGHT_WINDOW_SAMPLES = 20
DEFAULT_BRIER_SKILL_EPSILON = 0.05

# A moving-block bootstrap needs several independent blocks before its percentile interval
# can drive grading: with fewer, the resampling collapses onto a handful of arrangements
# (or a single deterministic draw) and can report a misleading zero-width interval.
MIN_BOOTSTRAP_INDEPENDENT_BLOCKS = 4


def unique_research_paths(
    researchers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exclude ambiguous duplicate IDs rather than choosing a first or last vote."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    for index, path in enumerate(researchers):
        name = path.get("researcher_id") if isinstance(path, dict) else None
        if not isinstance(name, str) or not name:
            rejected.append({"record_index": index, "reason": "researcher_id_missing_or_invalid"})
            continue
        grouped.setdefault(name, []).append(path)
    accepted = []
    for name, paths in grouped.items():
        if len(paths) != 1:
            rejected.append({"researcher_id": name, "record_count": len(paths),
                             "reason": "duplicate_researcher_id"})
        else:
            accepted.append(paths[0])
    return accepted, rejected


def _session_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    session = value[:10]
    try:
        return session if date.fromisoformat(session).isoformat() == session else None
    except ValueError:
        return None


def _path_assessment(path: dict[str, Any], assessments: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    if assessments is None:
        return path  # Legacy standalone diagnostics consume optional annotations.
    value = assessments.get(path.get("researcher_id"))
    return value if isinstance(value, dict) else {}


def path_has_detected_leakage(path: dict[str, Any], assessments: dict[str, dict[str, Any]] | None = None) -> bool:
    """An explicit assessment cannot erase a path's declared timing violation."""
    for metadata in (path, _path_assessment(path, assessments)):
        if "LEAKAGE_DETECTED" in (metadata.get("reason_codes") or []):
            return True
    validation = path.get("validation")
    if not isinstance(validation, dict):
        return False
    leakage = validation.get("leakage_audit")
    if isinstance(leakage, dict) and (
        leakage.get("features_use_data_through_decision_close_only") is False
        or leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features")) is False
    ):
        return True
    purged = validation.get("purged_kfold")
    return isinstance(purged, dict) and purged.get("retained_interval_overlaps", 0) not in (None, 0)


def _valid_probability(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0 <= float(value) <= 1)


def _audit_oos_path(
    path: dict[str, Any], manifest: dict[str, Any], bars: list[dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    summary = {"status": "not_assessed", "supplied_samples": 0, "valid_samples": 0,
               "valid_predictions": 0, "rejected_samples": 0, "rejection_reasons": {},
               "rejected_sample_rate": None, "rejected_examples": []}
    validation = path.get("validation") or {}
    if not isinstance(validation, dict):
        summary["reason"] = "validation_metadata_not_object"
        return {}, summary
    source_rows = validation.get("oos_forecasts")
    if not isinstance(source_rows, list):
        summary["reason"] = "dated_oos_forecasts_not_supplied"
        return {}, summary
    summary["supplied_samples"] = len(source_rows)
    horizon_name = manifest.get("horizon")
    horizon = HORIZONS.get(horizon_name)
    target_definition = f"close[t+{horizon}] / close[t] - 1 > 0" if horizon else None
    session_index: dict[str, int] = {}
    closes: list[float] = []
    forward: list[float | None] = []
    bars_error = None
    if bars is not None:
        try:
            sessions = [str(bar["date"])[:10] for bar in bars]
            closes = [float(bar["close"]) for bar in bars]
            if (any(len(session) != 10 for session in sessions)
                    or len(set(sessions)) != len(sessions)
                    or any(close <= 0 or not math.isfinite(close) for close in closes)):
                raise ValueError("invalid frozen sessions or close values")
            session_index = {session: i for i, session in enumerate(sessions)}
            forward = forward_returns(closes, horizon) if horizon else []
        except (KeyError, TypeError, ValueError, OverflowError):
            session_index, closes, forward = {}, [], []
            bars_error = "frozen_market_bars_invalid"
    latest_close = manifest.get("latest_confirmed_close")
    latest = _session_date(latest_close.get("session_date")) if isinstance(latest_close, dict) else None
    if latest is None and bars and not bars_error:
        latest = _session_date(bars[-1].get("date"))
    if latest is None:
        latest = _session_date(manifest.get("asof_timestamp"))
    latest_index = session_index.get(latest) if session_index else None
    duplicates: set[str] = set()
    seen: set[str] = set()
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        decision = row.get("decision_session")
        if isinstance(decision, str):
            if decision in seen:
                duplicates.add(decision)
            seen.add(decision)

    rows: dict[str, dict[str, Any]] = {}
    for row_number, record in enumerate(source_rows):
        # Keep the loop variable intact: `row` is the object under test, and a
        # non-object record is analysed as an empty one with an explicit reason.
        reasons: list[str] = [] if isinstance(record, dict) else ["record_not_object"]
        row = record if isinstance(record, dict) else {}
        decision = row.get("decision_session")
        label_end = row.get("label_end_session")
        target = row.get("target")
        probability = row.get("probability")
        training_end = row.get("training_label_end_session")
        recorded_target = row.get("target_definition") or validation.get("target_definition")
        decision_index = session_index.get(decision) if isinstance(decision, str) else None
        label_index = session_index.get(label_end) if isinstance(label_end, str) else None
        if not isinstance(decision, str) or not isinstance(label_end, str):
            reasons.append("decision_or_label_end_missing")
        elif bars is not None:
            if bars_error:
                reasons.append(bars_error)
            elif decision_index is None or label_index is None:
                reasons.append("session_missing_from_frozen_market_bars")
            elif latest_index is None:
                reasons.append("snapshot_asof_session_missing_from_frozen_market_bars")
            elif horizon is None or label_index != decision_index + horizon:
                reasons.append("label_horizon_does_not_match_frozen_sessions")
            elif latest_index is not None and label_index > latest_index:
                reasons.append("label_not_matured_by_snapshot_asof")
            elif forward[decision_index] is None:
                reasons.append("forward_return_unavailable_for_oos_label")
            else:
                expected_target = int(float(forward[decision_index]) > 0)
                if target != expected_target:
                    reasons.append("target_does_not_match_frozen_close_prices")
        elif not isinstance(latest, str):
            reasons.append("snapshot_asof_session_missing_or_invalid")
        elif not (decision <= label_end <= latest):
            reasons.append("label_outside_snapshot_time_boundary")
        if not isinstance(target, int) or isinstance(target, bool) or target not in (0, 1):
            reasons.append("invalid_binary_target")
        if recorded_target != target_definition:
            reasons.append("target_definition_does_not_match_requested_horizon")
        if probability is not None and not _valid_probability(probability):
            reasons.append("invalid_probability_value")
        if training_end is not None:
            if not isinstance(training_end, str):
                reasons.append("invalid_training_label_boundary")
            elif bars is not None and training_end not in session_index:
                reasons.append("training_label_end_missing_from_frozen_market_bars")
            elif (bars is not None and decision_index is not None and session_index[training_end] > decision_index) or (bars is None and isinstance(decision, str) and training_end > decision):
                reasons.append("training_label_not_matured_by_decision")
        elif _valid_probability(probability) and bars is not None:
            reasons.append("training_label_boundary_missing_for_prediction")
        calibration_applied = row.get("calibration_applied")
        calibration_end = row.get("calibration_label_end_session")
        calibration_count = row.get("calibration_fit_observations")
        raw_probability = row.get("raw_probability")
        if calibration_applied is True:
            if not _valid_probability(raw_probability):
                reasons.append("raw_probability_missing_for_calibrated_record")
            if isinstance(calibration_count, bool) or not isinstance(calibration_count, int) or calibration_count < 40:
                reasons.append("calibration_fit_set_insufficient_or_invalid")
            slope, intercept = row.get("calibration_slope"), row.get("calibration_intercept")
            if not (isinstance(slope, (int, float)) and not isinstance(slope, bool)
                    and isinstance(intercept, (int, float)) and not isinstance(intercept, bool)):
                reasons.append("calibration_coefficients_missing")
            elif _valid_probability(probability) and _valid_probability(raw_probability) and 1e-9 < float(raw_probability) < 1 - 1e-9:
                raw_score = math.log(float(raw_probability) / (1 - float(raw_probability)))
                mapped = 1 / (1 + math.exp(-max(-60.0, min(60.0, intercept + slope * raw_score))))
                if abs(float(probability) - mapped) > 1e-6:
                    reasons.append("calibrated_probability_mismatch")
            if not isinstance(calibration_end, str):
                reasons.append("calibration_label_boundary_missing")
            elif bars is not None and not bars_error:
                if calibration_end not in session_index:
                    reasons.append("calibration_label_end_missing_from_frozen_market_bars")
                elif decision_index is not None and session_index[calibration_end] >= decision_index:
                    reasons.append("calibration_label_not_matured_before_decision")
            elif bars is None and isinstance(decision, str) and calibration_end >= decision:
                reasons.append("calibration_label_not_matured_before_decision")
        elif calibration_applied is False:
            if (_valid_probability(raw_probability) and _valid_probability(probability)
                    and abs(float(raw_probability) - float(probability)) > 1e-9):
                reasons.append("uncalibrated_probability_differs_from_raw")
        else:
            if calibration_applied is not None:
                reasons.append("invalid_calibration_marker")
            # A mapped probability that differs from its raw output without a recorded origin
            # boundary is exactly the retrofitted-calibration pattern the audit must reject.
            if (_valid_probability(raw_probability) and _valid_probability(probability)
                    and abs(float(raw_probability) - float(probability)) > 1e-9):
                reasons.append("calibrated_probability_without_recorded_origin_boundary")
        if isinstance(decision, str) and decision in duplicates:
            reasons.append("duplicate_decision_session")
        if reasons:
            summary["rejected_samples"] += 1
            for reason in set(reasons):
                summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
            if len(summary["rejected_examples"]) < 5:
                summary["rejected_examples"].append({"record_index": row_number,
                                                     "decision_session": decision if isinstance(decision, str) else None,
                                                     "reason_codes": sorted(set(reasons))})
            continue
        rows[decision] = {**row, "decision_session": decision, "label_end_session": label_end,
                          "target": target, "target_definition": recorded_target}
    summary["valid_samples"] = len(rows)
    summary["rejected_sample_rate"] = summary["rejected_samples"] / len(source_rows) if source_rows else None
    summary["valid_predictions"] = sum(_valid_probability(row.get("probability")) for row in rows.values())
    summary["prediction_days"] = summary["valid_predictions"]
    if session_index and horizon:
        predicted_indices = sorted(
            session_index[row["decision_session"]] for row in rows.values()
            if _valid_probability(row.get("probability")) and row.get("decision_session") in session_index
        )
        summary["non_overlapping_prediction_count"] = len(non_overlapping(predicted_indices, horizon))
        summary["effective_sample_size_estimate"] = round(summary["valid_predictions"] / (horizon + 1))
        summary["overlap_disclosure"] = (
            f"daily walk-forward ledger; adjacent {horizon}-session labels overlap; "
            "non-overlapping count and effective size are the conservative references for thresholds; "
            "skill intervals use block resampling"
        )
    else:
        summary["non_overlapping_prediction_count"] = None
        summary["effective_sample_size_estimate"] = None
    summary["status"] = "rejected_records" if summary["rejected_samples"] else "validated"
    if bars is None:
        summary["structural_validation"] = "limited_no_frozen_session_index"
    else:
        summary["structural_validation"] = "verified_against_frozen_session_index_and_close_labels"
    return rows, summary


def audit_oos_result(
    result: dict[str, Any], manifest: dict[str, Any], bars: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Verify dated OOS records against the frozen horizon and, when provided, bars."""
    if any(result.get(key) != manifest.get(key) for key in ("ticker", "horizon", "snapshot_id")):
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        source_rows = validation.get("oos_forecasts")
        supplied = len(source_rows) if isinstance(source_rows, list) else 0
        return {"status": "rejected_snapshot_identity", "supplied_samples": supplied,
                "valid_samples": 0, "valid_predictions": 0, "valid_prediction_count": 0,
                "rejected_samples": supplied, "rejected_sample_rate": 1.0 if supplied else None,
                "rejection_reasons": {"snapshot_identity_mismatch": supplied or 1},
                "rejected_examples": [], "reason": "oos_rows_do_not_match_frozen_snapshot_identity"}
    rows, summary = _audit_oos_path(result, manifest, bars)
    return {**summary, "valid_prediction_count": sum(_valid_probability(row.get("probability")) for row in rows.values())}


def _validated_oos_rows(
    researchers: list[dict[str, Any]], manifest: dict[str, Any],
    bars: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    audits: dict[str, dict[str, Any]] = {}
    for path in researchers:
        if path.get("result_role", "forecast") != "forecast":
            continue
        if any(path.get(key) != manifest.get(key) for key in ("ticker", "horizon", "snapshot_id")):
            validation = path.get("validation") if isinstance(path.get("validation"), dict) else {}
            source_rows = validation.get("oos_forecasts")
            supplied = len(source_rows) if isinstance(source_rows, list) else 0
            audits[str(path.get("researcher_id", "unknown"))] = {
                "status": "rejected_snapshot_identity", "supplied_samples": supplied,
                "valid_samples": 0, "valid_predictions": 0, "rejected_samples": supplied,
                "rejected_sample_rate": 1.0 if supplied else None,
                "rejection_reasons": {"snapshot_identity_mismatch": supplied or 1},
                "rejected_examples": [], "reason": "oos_rows_do_not_match_frozen_snapshot_identity",
            }
            continue
        name = str(path.get("researcher_id", "unknown"))
        rows, summary = _audit_oos_path(path, manifest, bars)
        audits[name] = summary
        if rows:
            result[name] = rows
    return result, audits


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
                 or (index == 9 and float(row["probability"]) == 1)]
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


def _common_oos_metrics(
    rows_by_path: dict[str, dict[str, dict[str, Any]]], names: list[str],
) -> dict[str, Any]:
    if not names:
        return {"common_oos_samples": 0,
                "reason": "no dated forecast paths supplied valid OOS records"}
    missing_names = sorted(name for name in names if name not in rows_by_path)
    if missing_names:
        return {"common_oos_samples": 0, "all_paths_valid_prediction_samples": 0,
                "all_available_label_samples": 0,
                "reason": "current forecast paths missing valid dated OOS records",
                "missing_path_ids": missing_names,
                "path_metrics_on_common_oos_dates": {},
                "path_metrics_on_same_complete_samples": {}}
    common_dates = set.intersection(*(set(rows_by_path[name]) for name in names))
    compatible: list[str] = []
    for decision in sorted(common_dates):
        row_set = [rows_by_path[name][decision] for name in names]
        if (len({row["target"] for row in row_set}) == 1
                and len({row["label_end_session"] for row in row_set}) == 1
                and len({row.get("target_definition") for row in row_set}) == 1):
            compatible.append(decision)
    common_rows = {name: [rows_by_path[name][decision] for decision in compatible] for name in names}
    complete_dates = [decision for decision in compatible
                      if all(_valid_probability(rows_by_path[name][decision].get("probability"))
                             for name in names)]
    complete_rows = {name: [rows_by_path[name][decision] for decision in complete_dates]
                     for name in names}
    return {
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
    }


def _policy_replay_quality(
    rows_by_path: dict[str, dict[str, dict[str, Any]]], manifest: dict[str, Any],
    bars: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not rows_by_path:
        return {"status": "not_assessed", "reason": "no_valid_dated_oos_records"}
    if not bars:
        return {"status": "partial", "reason": "frozen_market_bars_unavailable_for_causal_baseline"}
    horizon = HORIZONS.get(manifest.get("horizon"))
    if not horizon:
        return {"status": "not_assessed", "reason": "unsupported_horizon"}
    sessions = [str(bar.get("date", ""))[:10] for bar in bars]
    closes = [float(bar["close"]) for bar in bars]
    if len(set(sessions)) != len(sessions) or any(close <= 0 or not math.isfinite(close) for close in closes):
        return {"status": "not_assessed", "reason": "frozen_market_sessions_or_closes_invalid"}
    index_by_date = {session: index for index, session in enumerate(sessions)}
    forward = forward_returns(closes, horizon)
    all_dates = sorted(set().union(*(set(path_rows) for path_rows in rows_by_path.values())))
    cohort: list[dict[str, Any]] = []
    rejected = 0
    path_counts: dict[str, int] = {}
    for decision in all_dates:
        index = index_by_date.get(decision)
        if index is None or index + horizon >= len(bars):
            rejected += 1
            continue
        date_rows = [path_rows[decision] for path_rows in rows_by_path.values() if decision in path_rows]
        if (not date_rows or len({row["target"] for row in date_rows}) != 1
                or len({row["label_end_session"] for row in date_rows}) != 1
                or len({row.get("target_definition") for row in date_rows}) != 1):
            rejected += 1
            continue
        matured = non_overlapping(list(range(max(0, index - horizon + 1))), horizon)
        matured_outcomes = [forward[i] for i in matured if i + horizon <= index and forward[i] is not None]
        if not matured_outcomes:
            continue
        # Jeffreys-smoothed expanding base rate: (wins + 1/2) / (n + 1).
        baseline = (sum(value > 0 for value in matured_outcomes) + 0.5) / (len(matured_outcomes) + 1)
        predictions = [row for row in date_rows if _valid_probability(row.get("probability"))]
        if predictions:
            path_counts[str(len(predictions))] = path_counts.get(str(len(predictions)), 0) + 1
            probability = sum(float(row["probability"]) for row in predictions) / len(predictions)
            cohort.append({"decision_session": decision,
                           "label_end_session": date_rows[0]["label_end_session"],
                           "target": date_rows[0]["target"], "probability": probability,
                           "baseline_probability": baseline,
                           "available_path_ids": sorted(name for name, values in rows_by_path.items()
                                                         if decision in values and _valid_probability(values[decision].get("probability")))})
        else:
            cohort.append({"decision_session": decision,
                           "label_end_session": date_rows[0]["label_end_session"],
                           "target": date_rows[0]["target"], "probability": None,
                           "baseline_probability": baseline, "available_path_ids": []})
    predicted = [row for row in cohort if _valid_probability(row.get("probability"))]
    baseline_scored = [{**row, "probability": row["baseline_probability"]} for row in predicted]
    metrics = _probability_metrics(cohort, denominator=len(cohort))
    baseline_metrics = _probability_metrics(baseline_scored, denominator=len(predicted))
    candidate_brier = metrics.get("brier_score")
    baseline_brier = baseline_metrics.get("brier_score")
    skill = (1 - candidate_brier / baseline_brier
             if isinstance(candidate_brier, (int, float)) and isinstance(baseline_brier, (int, float))
             and baseline_brier > 0 else None)
    strict_decisions = non_overlapping([index_by_date[row["decision_session"]] for row in predicted
                                        if row["decision_session"] in index_by_date], horizon)
    strict_dates = {sessions[index] for index in strict_decisions}
    strict_rows = [row for row in predicted if row["decision_session"] in strict_dates]
    strict_metrics = _probability_metrics(strict_rows, denominator=len(strict_rows)) if strict_rows else {}
    strict_baseline_metrics = _probability_metrics(
        [{**row, "probability": row["baseline_probability"]} for row in strict_rows],
        denominator=len(strict_rows),
    ) if strict_rows else {}
    strict_candidate = strict_metrics.get("brier_score")
    strict_baseline = strict_baseline_metrics.get("brier_score")
    strict_skill = (1 - strict_candidate / strict_baseline
                    if isinstance(strict_candidate, (int, float)) and isinstance(strict_baseline, (int, float))
                    and strict_baseline > 0 else None)
    uncertainty = _block_bootstrap_skill_interval(predicted, horizon=horizon)
    if not predicted:
        replay_status = "not_assessed"
    elif uncertainty.get("status") == "estimated":
        replay_status = "estimated_with_block_bootstrap_interval"
    else:
        replay_status = "descriptive_no_uncertainty_interval"
    return {
        "status": replay_status,
        "method": "date-by-date equal mean of every valid frozen OOS path; paths may abstain; compared on identical predicted dates with a Jeffreys-smoothed causal expanding base rate",
        "evidence_scope": "full-history daily causal walk-forward ledger from the current frozen snapshot; overlapping labels; retrospective replay, not a prospective prediction ledger",
        "coverage_denominator_scope": "union_of_submitted_valid_dated_oos_records; dates omitted by every path are not observable here",
        "current_paths_used_for_filtering": False,
        "eligible_oos_label_dates": len(cohort),
        "predicted_dates": len(predicted),
        "non_overlapping_predicted_dates": len(strict_rows),
        "oos_coverage": len(predicted) / len(cohort) if cohort else 0.0,
        "abstain_rate": 1 - len(predicted) / len(cohort) if cohort else None,
        "path_count_by_prediction_date": path_counts,
        "active_path_mask": [
            {"decision_session": row["decision_session"],
             "available_path_ids": row["available_path_ids"]}
            for row in cohort
        ],
        "conflicting_or_invalid_label_dates": rejected,
        "forecast_metrics": metrics,
        "expanding_base_rate_on_same_dates": baseline_metrics,
        "brier_skill_vs_expanding_base_rate": skill,
        "non_overlapping_brier_skill_vs_expanding_base_rate": strict_skill,
        "skill_uncertainty": uncertainty,
        "_predicted_rows": predicted,
    }


def _block_bootstrap_skill_interval(
    predicted: list[dict[str, Any]], *, horizon: int, iterations: int = 2000, seed: int = 0,
) -> dict[str, Any]:
    """Moving-block bootstrap percentile interval for the Brier skill versus the expanding base rate.

    The daily walk-forward ledger carries horizon-overlapping labels, so resampling must preserve
    local dependence: blocks of consecutive predicted sessions are drawn with replacement and the
    skill is recomputed inside every resample. The interval is retrospective, descriptive evidence,
    not a prospective guarantee. It is reported only when the ledger supplies at least
    MIN_BOOTSTRAP_INDEPENDENT_BLOCKS independent blocks and the resamples vary; otherwise the
    interval is marked not estimable.
    """
    block = horizon + 1
    if len(predicted) < 2:
        return {"status": "insufficient_samples", "predicted_dates": len(predicted),
                "block_sessions": block, "independent_blocks": 0}
    pairs = [
        ((float(row["probability"]) - int(row["target"])) ** 2,
         (float(row["baseline_probability"]) - int(row["target"])) ** 2)
        for row in predicted
    ]
    n = len(pairs)
    independent_blocks = n // block
    if independent_blocks < MIN_BOOTSTRAP_INDEPENDENT_BLOCKS:
        return {"status": "insufficient_independent_blocks", "predicted_dates": n,
                "block_sessions": block, "independent_blocks": independent_blocks,
                "minimum_independent_blocks": MIN_BOOTSTRAP_INDEPENDENT_BLOCKS}
    rng = random.Random(seed)  # noqa: S311 - statistical block bootstrap, not cryptographic
    starts_range = max(1, n - block + 1)
    skills: list[float] = []
    for _ in range(iterations):
        sum_model = 0.0
        sum_base = 0.0
        drawn = 0
        while drawn < n:
            start = rng.randrange(0, starts_range)
            for offset in range(block):
                if drawn >= n:
                    break
                position = start + offset
                if position < n:
                    model_loss, base_loss = pairs[position]
                    sum_model += model_loss
                    sum_base += base_loss
                    drawn += 1
        if sum_base > 0:
            skills.append(1 - sum_model / sum_base)
    if not skills:
        return {"status": "not_estimated", "reason": "bootstrap_resamples_degenerate",
                "independent_blocks": independent_blocks}
    if min(skills) == max(skills):
        return {"status": "degenerate_zero_width_interval", "predicted_dates": n,
                "block_sessions": block, "independent_blocks": independent_blocks,
                "reason": "every resample reproduced the same skill; the interval is not informative"}
    skills.sort()
    lower = skills[int(0.025 * len(skills))]
    upper = skills[min(len(skills) - 1, int(0.975 * len(skills)))]
    return {
        "status": "estimated",
        "method": "moving_block_bootstrap_percentile",
        "block_sessions": block,
        "independent_blocks": independent_blocks,
        "iterations": iterations,
        "seed": seed,
        "confidence_level": 0.95,
        "ci_low": lower,
        "ci_high": upper,
        "excludes_zero": lower > 0,
        "notes": "block resampling preserves horizon-overlap dependence; descriptive retrospective evidence, not a prospective guarantee",
    }


def _fit_platt_logits(scores: list[float], targets: list[int]) -> tuple[float, float] | None:
    """Deterministic Platt fit on logits, mirroring the ML path's calibration style."""
    if len(scores) < 40 or len(set(targets)) < 2:
        return None
    slope, intercept = 1.0, 0.0
    for _ in range(500):
        grad_slope = grad_intercept = 0.0
        for score, target in zip(scores, targets, strict=True):
            residual = logistic(intercept + slope * score) - target
            grad_slope += residual * score
            grad_intercept += residual
        n = len(scores)
        slope -= 0.05 * (grad_slope / n + 1e-3 * slope)
        intercept -= 0.05 * grad_intercept / n
    return slope, intercept


def _apply_platt_logit(calibrator: tuple[float, float], probability: float) -> float:
    clipped = max(1e-6, min(1 - 1e-6, float(probability)))
    return logistic(calibrator[1] + calibrator[0] * math.log(clipped / (1 - clipped)))


def _ensemble_calibration_quality(predicted: list[dict[str, Any]], *, horizon: int) -> dict[str, Any]:
    """Chronological fit/validation split of a Platt calibrator for the equal-weight ensemble.

    The calibrator is fitted on the earliest 60% of predicted dates and checked on later dates
    whose decisions follow the fit block's last label end (purge). It is accepted only when the
    check block shows a strictly lower Brier score than the raw equal-weight ensemble.
    Counts are prediction days; adjacent labels overlap and the check is chronological. Both
    blocks are retrospective ledger dates that overlap windows already consumed by path-level
    evaluation (including the ML path's final chronological holdout), so the comparison is a
    descriptive retrospective check, not an independent confirmation.
    """
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        return {"status": "not_assessed", "reason": "unsupported_horizon"}
    if len(predicted) < 60:
        return {"status": "insufficient_dates", "predicted_dates": len(predicted), "minimum_predicted_dates": 60}
    split = int(len(predicted) * 0.6)
    fit_rows = predicted[:split]
    last_fit_label_end = fit_rows[-1]["label_end_session"]
    validation_rows = [row for row in predicted[split:] if row["decision_session"] > last_fit_label_end]
    if len(fit_rows) < 30 or len(validation_rows) < 30:
        return {"status": "insufficient_dates", "fit_dates": len(fit_rows),
                "validation_dates": len(validation_rows), "minimum_per_block": 30}
    calibrator = _fit_platt_logits(
        [math.log(max(1e-6, min(1 - 1e-6, float(row["probability"])))
                  / (1 - max(1e-6, min(1 - 1e-6, float(row["probability"])))))
         for row in fit_rows],
        [int(row["target"]) for row in fit_rows],
    )
    if calibrator is None:
        return {"status": "not_assessed", "reason": "calibrator_fit_unavailable"}
    fit_pairs = [(float(row["probability"]), int(row["target"])) for row in fit_rows]
    validation_pairs = [(float(row["probability"]), int(row["target"])) for row in validation_rows]
    fit_raw_brier = mean([(p - y) ** 2 for p, y in fit_pairs])
    validation_raw_brier = mean([(p - y) ** 2 for p, y in validation_pairs])
    validation_calibrated_brier = mean([(_apply_platt_logit(calibrator, p) - y) ** 2 for p, y in validation_pairs])
    improved = validation_calibrated_brier < validation_raw_brier
    return {
        "status": "improved_on_subsequent_ledger_block" if improved else "no_improvement_on_subsequent_ledger_block",
        "method": "Platt scaling on ensemble logits; fitted on the earliest 60% of predicted dates and checked on later dates after a purge gap (retrospective ledger check, not an independent holdout)",
        "evidence_scope": "retrospective; the check block reuses ledger dates that overlap windows already consumed by path-level evaluation (including the ML path's final holdout), so the improvement is descriptive and not an independent confirmation",
        "fit_dates": len(fit_rows),
        "validation_dates": len(validation_rows),
        "fit_start": fit_rows[0]["decision_session"],
        "fit_end": fit_rows[-1]["decision_session"],
        "validation_start": validation_rows[0]["decision_session"],
        "validation_end": validation_rows[-1]["decision_session"],
        "purge_rule": "every fit label matures before the first validation decision",
        "fit_raw_brier": fit_raw_brier,
        "validation_raw_brier": validation_raw_brier,
        "validation_calibrated_brier": validation_calibrated_brier,
        "validation_improvement": validation_raw_brier - validation_calibrated_brier,
        "calibrator": {"kind": "platt_logit", "slope": calibrator[0], "intercept": calibrator[1]},
        "overlap_disclosure": f"daily walk-forward ledger; adjacent {horizon}-session labels overlap; the check block is chronological but overlaps windows already consumed by path-level evaluation, so it is not an independent confirmation",
        "acceptance": "accepted only when the check block's calibrated Brier is strictly lower than the raw ensemble's",
    }


def build_oos_probability_quality(
    researchers: list[dict[str, Any]], manifest: dict[str, Any],
    *, bars: list[dict[str, Any]] | None = None,
    assessments: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    researchers, input_rejections = unique_research_paths(researchers)
    rows_by_path, row_audits = _validated_oos_rows(researchers, manifest, bars)
    path_metrics = {
        name: _probability_metrics(list(rows.values()))
        for name, rows in rows_by_path.items()
    }
    all_names = sorted(rows_by_path)
    active_names = sorted({str(path.get("researcher_id", "unknown")) for path in researchers
                           if path.get("result_role", "forecast") == "forecast"
                            and _path_assessment(path, assessments).get("forecast_eligible", assessments is None) is True
                            and not path_has_detected_leakage(path, assessments)})
    common_oos = _common_oos_metrics(rows_by_path, active_names)
    # A path with detected information leakage cannot support historical policy skill,
    # even when individual labels and dates in its submitted rows are well formed.
    replay_rows = {name: rows for name, rows in rows_by_path.items()
                   if not any(path.get("researcher_id") == name
                               and path_has_detected_leakage(path, assessments)
                              for path in researchers)}
    replay = _policy_replay_quality(replay_rows, manifest, bars)
    predicted_rows = replay.pop("_predicted_rows", [])
    ensemble_calibration = _ensemble_calibration_quality(predicted_rows, horizon=HORIZONS.get(manifest.get("horizon")))
    return {
        "status": "available" if common_oos.get("common_oos_samples", 0) else "partial" if path_metrics else "not_assessed",
        "method": "Brier, log loss, ten equal-width reliability bins, ECE, coverage and abstain rate; full-history walk-forward ledger diagnostics; policy replay with block-bootstrap skill interval; ensemble calibrator chronological holdout",
        "current_forecast_paths": active_names,
        "assessment_source": "explicit_assessments" if assessments is not None else "legacy_path_annotations",
        "path_input_rejections": input_rejections,
        "target_definition": f"close[t+{HORIZONS.get(manifest.get('horizon'), 0)}] / close[t] - 1 > 0",
        "paths": path_metrics,
        "row_validation": row_audits,
        "common_oos": common_oos,
        "candidate_path_common_oos": _common_oos_metrics(rows_by_path, all_names),
        "algorithm_policy_replay": replay,
        "ensemble_calibration": ensemble_calibration,
    }


def build_brier_skill_weight_diagnostic(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    bars: list[dict[str, Any]] | None = None,
    assessments: dict[str, dict[str, Any]] | None = None,
    epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
    minimum_per_window: int = MINIMUM_WEIGHT_WINDOW_SAMPLES,
) -> dict[str, Any]:
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("Brier skill epsilon must be finite and non-negative")
    if isinstance(minimum_per_window, bool) or not isinstance(minimum_per_window, int) or minimum_per_window < 2:
        raise ValueError("minimum_per_window must be an integer of at least 2")
    researchers, input_rejections = unique_research_paths(researchers)
    rows_by_path, _ = _validated_oos_rows(researchers, manifest, bars)
    active_names = {str(path.get("researcher_id", "unknown")) for path in researchers
                    if path.get("result_role", "forecast") == "forecast"
                    and _path_assessment(path, assessments).get("forecast_eligible", assessments is None) is True
                    and not path_has_detected_leakage(path, assessments)}
    names = sorted(name for name in rows_by_path if name in active_names)
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
        "assessment_source": "explicit_assessments" if assessments is not None else "legacy_path_annotations",
        "path_input_rejections": input_rejections,
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

    # The evaluation window starts strictly after the last estimation label so that every
    # estimation label matures first; daily walk-forward ledgers need this because adjacent
    # horizon labels overlap.
    split = len(complete_dates) // 2
    estimation_dates = complete_dates[:split]
    latest_estimation_label = max(rows_by_path[names[0]][date]["label_end_session"] for date in estimation_dates)
    evaluation_dates = [date for date in complete_dates[split:] if date > latest_estimation_label]
    if len(estimation_dates) < minimum_per_window or len(evaluation_dates) < minimum_per_window:
        base["fallback_reason"] = "one_temporal_window_has_too_few_common_oos_samples"
        return base
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
    weighted_brier = mean((probability - target) ** 2 for probability, target in zip(weighted_probs, evaluation_targets, strict=True))
    equal_brier = mean((probability - target) ** 2 for probability, target in zip(equal_probs, evaluation_targets, strict=True))
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
    bars: list[dict[str, Any]] | None = None,
    assessments: dict[str, dict[str, Any]] | None = None,
    skill_epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
    minimum_weight_window_samples: int = MINIMUM_WEIGHT_WINDOW_SAMPLES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_oos_probability_quality(researchers, manifest, bars=bars, assessments=assessments),
        build_brier_skill_weight_diagnostic(
            researchers, manifest, bars=bars, assessments=assessments, epsilon=skill_epsilon,
            minimum_per_window=minimum_weight_window_samples,
        ),
    )
