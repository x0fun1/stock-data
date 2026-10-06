"""Adversarial checks that can warn or veto; this module does not predict."""

from __future__ import annotations

from typing import Any


def _has_non_null_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        if any(key in value and value[key] is not None for key in keys):
            return True
        return any(_has_non_null_key(child, keys) for child in value.values())
    if isinstance(value, list):
        return any(_has_non_null_key(child, keys) for child in value)
    return False


def _oos_baseline_checks(researchers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def visit(value: Any, researcher_id: str, path: str) -> None:
        if isinstance(value, dict):
            score = value.get("brier", value.get("holdout_brier"))
            baseline = value.get("base_rate_brier", value.get("holdout_base_rate_brier"))
            if isinstance(score, (int, float)) and isinstance(baseline, (int, float)):
                checks.append({
                    "researcher_id": researcher_id,
                    "validation_path": path,
                    "brier": float(score),
                    "base_rate_brier": float(baseline),
                    "beats_base_rate": float(score) < float(baseline),
                })
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit(child, researcher_id, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, researcher_id, f"{path}[{index}]")

    for result in researchers:
        if result.get("result_role", "forecast") != "forecast":
            continue
        validation = result.get("validation")
        if isinstance(validation, dict):
            visit(validation, str(result.get("researcher_id", "unknown")), "validation")
    return checks


def _has_chronological_holdout(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("one_time_chronological_holdout") is True:
            return True
        if any(
            isinstance(value.get(key), str) and value[key]
            for key in ("holdout_start_session", "start_session")
        ):
            return True
        return any(_has_chronological_holdout(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_chronological_holdout(child) for child in value)
    return False


def _bias_audit(manifest: dict[str, Any], researchers: list[dict[str, Any]]) -> dict[str, Any]:
    forecasts = [
        result for result in researchers
        if result.get("result_role", "forecast") == "forecast"
        and isinstance(result.get("prob_up"), (int, float))
    ]
    auditable_paths = [result for result in researchers if result.get("status") not in {"failed", "invalid"}]
    leak_checks: list[bool] = []
    holdout_checks: list[bool] = []
    candidate_counts: dict[str, int] = {}
    observed_factor_names: set[str] = set()
    for result in auditable_paths:
        validation = result.get("validation") or {}
        leakage = validation.get("leakage_audit", {}) if isinstance(validation, dict) else {}
        labels_ok = leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features"))
        leak_checks.append(
            leakage.get("features_use_data_through_decision_close_only") is True
            and labels_ok is True
        )
        holdout_checks.append(_has_chronological_holdout(validation))
        decay = validation.get("factor_decay")
        if isinstance(decay, dict):
            candidate_counts[str(result.get("researcher_id"))] = len(decay)
            observed_factor_names.update(str(name) for name in decay)
        factor_metrics = validation.get("factor_metrics")
        if isinstance(factor_metrics, dict):
            candidate_counts[str(result.get("researcher_id"))] = len(factor_metrics)
            observed_factor_names.update(str(name) for name in factor_metrics)
        selected = validation.get("candidate_factor_count")
        if isinstance(selected, int) and str(result.get("researcher_id")) not in candidate_counts:
            candidate_counts[str(result.get("researcher_id"))] = selected
    candidate_total = len(observed_factor_names) if observed_factor_names else sum(candidate_counts.values())

    oos_checks = _oos_baseline_checks(researchers)
    pbo_available = _has_non_null_key(manifest, {"pbo", "pbo_estimate"}) or any(
        _has_non_null_key(result.get("validation", {}), {"pbo", "pbo_estimate"}) for result in researchers
    )
    dsr_available = _has_non_null_key(manifest, {"dsr", "deflated_sharpe_ratio"}) or any(
        _has_non_null_key(result.get("validation", {}), {"dsr", "deflated_sharpe_ratio"}) for result in researchers
    )
    cost_available = any(
        _has_non_null_key(result.get("validation", {}), {"cost_model", "cost_sensitivity", "net_returns"})
        for result in researchers
    )
    single_security = any(
        (result.get("validation") or {}).get("universe_type") == "single_security"
        for result in researchers
    ) or not manifest.get("universe")
    if not leak_checks:
        leakage_status, leakage_detail = "not_assessed", "no research path was available"
    elif all(leak_checks):
        leakage_status, leakage_detail = "pass", "all available research paths supplied passing feature/label timing audits"
    else:
        leakage_status, leakage_detail = "warning", "at least one available research path lacks a complete feature/label timing audit"
    if not holdout_checks:
        holdout_status, holdout_detail = "not_assessed", "no research path was available"
    elif all(holdout_checks):
        holdout_status, holdout_detail = "pass", "all available research paths report chronological OOS evidence"
    else:
        holdout_status, holdout_detail = "warning", "at least one available research path lacks a chronological holdout marker"
    if pbo_available or dsr_available:
        overfit_status = "partial"
        overfit_detail = "a PBO or DSR value is present but its trial universe and assumptions are not independently verified"
    elif candidate_total > 1:
        overfit_status = "warning"
        overfit_detail = (
            f"{candidate_total} distinct factor candidates were inspected; selection-adjusted significance is not computed, "
            "and no complete strategy-trial return matrix is available for PBO/DSR"
        )
    else:
        overfit_status = "not_assessed"
        overfit_detail = "no complete strategy-trial return matrix or registered parameter-search count is available"
    cost_detail = (
        "cost-related fields are present but their execution assumptions and calculations have not been independently verified."
        if cost_available
        else "forecast and factor diagnostics use gross close-to-close returns; no executable strategy or cost inputs were supplied."
    )
    return {
        "look_ahead_and_label_timing": {"status": leakage_status, "detail": leakage_detail},
        "chronological_out_of_sample": {"status": holdout_status, "detail": holdout_detail},
        "oos_brier_vs_base_rate": oos_checks,
        "multiple_testing_and_overfitting": {
            "status": overfit_status,
            "candidate_factor_counts_by_path": candidate_counts,
            "distinct_factor_candidates_observed": candidate_total,
            "detail": overfit_detail,
        },
        "pbo": {
            "status": "present_unverified" if pbo_available else "not_assessed",
            "detail": "PBO requires a candidate-strategy return matrix across repeated splits; it is not inferred from one factor or ticker series.",
        },
        "deflated_sharpe_ratio": {
            "status": "present_unverified" if dsr_available else "not_assessed",
            "detail": "DSR requires strategy returns, a Sharpe estimate, and a defensible count of all trials.",
        },
        "transaction_costs_and_market_impact": {
            "status": "present_unverified" if cost_available else "not_assessed",
            "detail": cost_detail,
        },
        "survivorship_bias": {
            "status": "not_assessed_single_security" if single_security else "not_assessed",
            "detail": "no point-in-time universe membership, historical security lifecycle, or delisted-name coverage was supplied for an independent survivorship audit.",
        },
    }


def audit(manifest: dict[str, Any], researchers: list[dict[str, Any]], consensus: dict[str, Any]) -> dict[str, Any]:
    vetoes: list[str] = []
    warnings: list[str] = []
    snapshot_id = manifest.get("snapshot_id")
    if not snapshot_id or not manifest.get("source_payload_sha256"):
        vetoes.append("Snapshot identity or source payload digest is missing.")
    if manifest.get("data_quality", {}).get("overall") == "low":
        warnings.append("Snapshot quality is low; any forecast should be treated as exploratory.")
    for item in manifest.get("warnings", []):
        if any(word in str(item).lower() for word in ("future", "timestamp mismatch", "invalid", "after the requested")):
            vetoes.append(f"Snapshot integrity issue: {item}")
        else:
            warnings.append(f"Snapshot: {item}")
    for result in researchers:
        name = result.get("researcher_id", "unknown")
        if result.get("snapshot_id") != snapshot_id:
            vetoes.append(f"{name} researcher used a different snapshot identity.")
        if result.get("status") == "invalid":
            vetoes.append(f"{name} researcher declared its output invalid.")
        probability = result.get("prob_up")
        if result.get("result_role") == "diagnostic" and probability is not None:
            vetoes.append(f"{name} is diagnostic-only and must not emit a consensus probability.")
        if probability is not None:
            if not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
                vetoes.append(f"{name} produced an invalid probability value.")
            if not result.get("probability_source"):
                vetoes.append(f"{name} probability has no quantitative provenance.")
        elif result.get("result_role", "forecast") != "diagnostic" and result.get("status") in {"success", "partial"}:
            warnings.append(f"{name} status is {result.get('status')} but probability is unavailable.")
        for warning in result.get("warnings", []):
            warnings.append(f"{name}: {warning}")
        validation = result.get("validation", {})
        leakage = validation.get("leakage_audit", {}) if isinstance(validation, dict) else {}
        if probability is not None:
            if leakage.get("features_use_data_through_decision_close_only") is not True:
                vetoes.append(f"{name} has no passing feature-availability audit.")
            label_check = leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features"))
            if label_check is not True:
                vetoes.append(f"{name} has no passing target/label leakage audit.")
        purged = validation.get("purged_kfold", {}) if isinstance(validation, dict) else {}
        if purged and purged.get("retained_interval_overlaps", 0) != 0:
            vetoes.append(f"{name} retained overlapping training/test information intervals.")
        if result.get("status") == "insufficient_data":
            warnings.append(f"{name} path has insufficient data.")
    if consensus.get("available_paths", 0) < 2:
        warnings.append("Cross-validation by independent paths is unavailable; do not call this an ensemble consensus.")
    if consensus.get("agreement") in {"high_disagreement", "extreme_disagreement"}:
        warnings.append("Consensus probability is unstable across research paths.")
    bias_checks = _bias_audit(manifest, researchers)
    if bias_checks["look_ahead_and_label_timing"]["status"] == "warning":
        warnings.append("At least one research path lacks a complete declared feature/label timing audit.")
    if bias_checks["chronological_out_of_sample"]["status"] == "warning":
        warnings.append("At least one research path lacks a chronological out-of-sample marker.")
    overfit_status = bias_checks["multiple_testing_and_overfitting"]["status"]
    if overfit_status == "warning":
        warnings.append("Overfitting is not fully quantified: candidate selection was observed, but a complete strategy-trial history for selection-adjusted significance/PBO/DSR is unavailable.")
    elif overfit_status == "partial":
        warnings.append("PBO/DSR-related values are present but their trial universe and assumptions have not been independently verified.")
    elif overfit_status == "not_assessed":
        warnings.append("Overfitting is not quantified because a complete strategy-trial history is unavailable.")
    if bias_checks["transaction_costs_and_market_impact"]["status"] == "not_assessed":
        warnings.append("Transaction costs and market impact are not assessed; reported returns are gross diagnostics.")
    elif bias_checks["transaction_costs_and_market_impact"]["status"] == "present_unverified":
        warnings.append("Cost/market-impact fields are present but have not been independently verified.")
    if str(bias_checks["survivorship_bias"]["status"]).startswith("not_assessed"):
        warnings.append("Survivorship bias is not independently assessed because point-in-time universe and delisted-name history are unavailable.")
    if any(not check["beats_base_rate"] for check in bias_checks["oos_brier_vs_base_rate"]):
        warnings.append("At least one reported forecast OOS Brier score does not beat its base-rate benchmark.")
    status = "veto" if vetoes else "pass_with_warnings" if warnings else "pass"
    return {"status": status, "vetoes": sorted(set(vetoes)), "warnings": sorted(set(warnings)), "may_report_direction": not vetoes, "bias_audit": bias_checks}
