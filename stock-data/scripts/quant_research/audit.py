"""Adversarial checks that can warn or veto; this module does not predict."""

from __future__ import annotations

from typing import Any

from .gate_policy import classify_reasons


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
    reason_codes: list[str] = []

    def warn(code: str, message: str) -> None:
        reason_codes.append(code)
        warnings.append(message)

    def veto(code: str, message: str) -> None:
        reason_codes.append(code)
        vetoes.append(message)

    snapshot_id = manifest.get("snapshot_id")
    gate = manifest.get("data_validation") or {}
    prediction_gate = gate.get("prediction_eligibility", {})
    prediction_state = prediction_gate.get("status")
    if prediction_state is None:  # Legacy snapshots used a single boolean gate.
        prediction_state = "eligible" if gate.get("forecast_eligible", False) else "blocked"
    if prediction_state != "eligible":
        blocked_codes = prediction_gate.get("blockers", [])
        reason_codes.extend(blocked_codes)
        if not blocked_codes:
            reason_codes.append("PREDICTION_FAILED")
        for item in gate.get("blockers", []) + gate.get("errors", []):
            vetoes.append("Prediction eligibility: " + str(item))
        if not vetoes:
            vetoes.append("Prediction eligibility is blocked by a fatal market-data integrity condition.")
    if consensus.get("available_paths", 0) == 0:
        veto("PREDICTION_FAILED", "No quantitative path produced a valid probability.")
    if manifest.get("request", {}).get("intent") in {"descriptive", "factor_research"}:
        veto("DESCRIPTIVE_REQUEST", "Request intent does not authorize a future-direction probability.")
    if not snapshot_id or not manifest.get("source_payload_sha256"):
        veto("SNAPSHOT_IDENTITY_INVALID", "Snapshot identity or source payload digest is missing.")

    reason_codes.extend(gate.get("reason_codes", []))
    for item in gate.get("warnings", []):
        warn("DATA_WARNING", "Data quality: " + str(item))
    for item in manifest.get("warnings", []):
        warn("DATA_WARNING", "Snapshot: " + str(item))
    if manifest.get("data_quality", {}).get("overall") in {"low", "very_low"}:
        warn("OOS_INSUFFICIENT", "Snapshot quality score is low; interpret the forecast as exploratory.")

    for result in researchers:
        name = str(result.get("researcher_id", "unknown"))
        probability = result.get("prob_up")
        if result.get("snapshot_id") != snapshot_id:
            veto("SNAPSHOT_IDENTITY_INVALID", f"{name} researcher used a different snapshot identity.")
        if result.get("result_role") == "diagnostic" and probability is not None:
            veto("PREDICTION_FAILED", f"{name} is diagnostic-only and must not emit a consensus probability.")
        if probability is not None:
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
                veto("PREDICTION_FAILED", f"{name} produced an invalid probability value.")
            if not result.get("probability_source"):
                veto("PREDICTION_FAILED", f"{name} probability has no quantitative provenance.")
        elif result.get("result_role", "forecast") != "diagnostic" and result.get("status") in {"success", "partial"}:
            warn("PATH_FAILED", f"{name} status is {result.get('status')} but its probability is unavailable.")
        elif result.get("status") in {"failed", "invalid", "insufficient_data"}:
            warn("PATH_FAILED", f"{name} path did not produce a usable probability ({result.get('status')}).")
        for warning in result.get("warnings", []):
            warn("PATH_WARNING", f"{name}: {warning}")

        validation = result.get("validation", {})
        leakage = validation.get("leakage_audit", {}) if isinstance(validation, dict) else {}
        if result.get("result_role", "forecast") == "forecast":
            feature_check = leakage.get("features_use_data_through_decision_close_only")
            label_check = leakage.get("future_returns_used_only_as_labels", leakage.get("target_excluded_from_features"))
            if feature_check is False or label_check is False:
                veto("LEAKAGE_DETECTED", f"{name} declared a failing feature-availability or target/label timing check.")
            elif feature_check is not True or label_check is not True:
                warn("LEAKAGE_AUDIT_INCOMPLETE", f"{name} has incomplete leakage audit evidence; its probability is retained with lower confidence.")
        purged = validation.get("purged_kfold", {}) if isinstance(validation, dict) else {}
        if isinstance(purged, dict) and purged.get("retained_interval_overlaps", 0) not in (None, 0):
            veto("LEAKAGE_DETECTED", f"{name} retained overlapping training/test information intervals.")

    if consensus.get("available_paths", 0) < 2:
        warn("PATH_FAILED", "Cross-validation by independent paths is unavailable; do not call this an ensemble consensus.")
    if consensus.get("agreement") in {"high_disagreement", "extreme_disagreement"}:
        warn("LOW_FACTOR_AGREEMENT", "Consensus probability is unstable across research paths.")
    bias_checks = _bias_audit(manifest, researchers)
    if bias_checks["look_ahead_and_label_timing"]["status"] == "warning":
        warn("LEAKAGE_AUDIT_INCOMPLETE", "At least one research path lacks a complete declared feature/label timing audit.")
    if bias_checks["chronological_out_of_sample"]["status"] == "warning":
        warn("OOS_INSUFFICIENT", "At least one research path lacks a chronological out-of-sample marker.")
    overfit_status = bias_checks["multiple_testing_and_overfitting"]["status"]
    strategy_limitations: list[str] = []
    if overfit_status == "warning":
        strategy_limitations.extend(["PBO_NOT_ASSESSED", "DSR_NOT_ASSESSED"])
        warn("PBO_NOT_ASSESSED", "Overfitting is not fully quantified: a complete strategy-trial history for selection-adjusted significance/PBO/DSR is unavailable.")
        warn("DSR_NOT_ASSESSED", "Deflated Sharpe Ratio is not assessed without a complete, reproducible strategy-trial return universe.")
    elif overfit_status == "partial":
        strategy_limitations.extend(["PBO_NOT_ASSESSED", "DSR_NOT_ASSESSED"])
        warn("PBO_NOT_ASSESSED", "PBO/DSR-related values are present but their trial universe and assumptions have not been independently verified.")
        warn("DSR_NOT_ASSESSED", "Deflated Sharpe Ratio inputs and strategy-trial assumptions have not been independently verified.")
    else:
        strategy_limitations.extend(["PBO_NOT_ASSESSED", "DSR_NOT_ASSESSED"])
        warn("PBO_NOT_ASSESSED", "PBO/DSR are not assessed; this does not cancel the direction probability.")
        warn("DSR_NOT_ASSESSED", "DSR is not assessed without a strategy-trial return matrix.")
    if bias_checks["transaction_costs_and_market_impact"]["status"] == "not_assessed":
        strategy_limitations.append("TRANSACTION_COST_NOT_ASSESSED")
        warn("TRANSACTION_COST_NOT_ASSESSED", "Transaction costs and market impact are not assessed; reported returns are gross diagnostics.")
    elif bias_checks["transaction_costs_and_market_impact"]["status"] == "present_unverified":
        strategy_limitations.append("TRANSACTION_COST_NOT_ASSESSED")
        warn("TRANSACTION_COST_NOT_ASSESSED", "Cost/market-impact fields are present but have not been independently verified.")
    strategy_limitations.append("PORTFOLIO_BACKTEST_MISSING")
    warn("PORTFOLIO_BACKTEST_MISSING", "No portfolio-level backtest was supplied; do not infer strategy profitability from a single-security probability.")
    if str(bias_checks["survivorship_bias"]["status"]).startswith("not_assessed"):
        warn("PBO_NOT_ASSESSED", "Survivorship bias is not independently assessed because point-in-time universe and delisted-name history are unavailable.")
    if any(not check["beats_base_rate"] for check in bias_checks["oos_brier_vs_base_rate"]):
        warn("OOS_INSUFFICIENT", "At least one reported forecast OOS Brier score does not beat its base-rate benchmark.")

    bias_checks["prediction_veto"] = "LEAKAGE_DETECTED" in reason_codes
    bias_checks["strategy_veto"] = bool(strategy_limitations)
    classification = classify_reasons(reason_codes)
    # Invalid paths that were excluded do not cancel valid independent paths; detected leakage does.
    has_probability = consensus.get("available_paths", 0) > 0
    status = "veto" if vetoes or not has_probability else classification["status"]
    strategy_status = "not_validated" if strategy_limitations else "validated"
    return {
        "status": "veto" if status == "veto" else "pass_with_warnings" if warnings else "pass",
        "reporting_status": status,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "vetoes": sorted(set(vetoes)),
        "warnings": sorted(set(warnings)),
        "may_report_direction": status != "veto" and has_probability,
        "prediction_eligibility": {"status": "eligible" if has_probability and status != "veto" else "blocked",
                                   "blockers": classification["blockers"]},
        "reporting_eligibility": {"status": status, "warnings": classification["warnings"],
                                  "informational": classification["informational"]},
        "strategy_eligibility": {"status": strategy_status, "blockers": [], "limitations": list(dict.fromkeys(strategy_limitations)),
                                 "required_for_probability": False, "required_for_strategy_claim": True},
        "bias_audit": bias_checks,
        "verification_scope": "data and path declarations plus reported OOS metrics; not independent proof of provider authenticity or a full strategy audit",
    }
