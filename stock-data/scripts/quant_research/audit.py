"""Adversarial audit of the fixed-policy forecast.

The audit has one job: decide whether the production probability may be shown as
a direction.  It does not invent a confidence score, does not grade shadow paths
and does not blend probabilities.  Every check is either a hard veto, a warning
that bounds the claim, or an informational note.
"""

from __future__ import annotations

from typing import Any

from .forecast_policy import (
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_VALID_OOS_NONOVERLAP,
    POLICY_VERSION,
    PRICE_BASIS,
)


def _veto(state: dict[str, Any], code: str, message: str) -> None:
    state["reason_codes"].append(code)
    state["vetoes"].append(message)


def _warn(state: dict[str, Any], code: str, message: str) -> None:
    state["reason_codes"].append(code)
    state["warnings"].append(message)


def audit(
    manifest: dict[str, Any],
    researchers: list[dict[str, Any]],
    consensus: dict[str, Any],
    *,
    bars: list[dict[str, Any]] | None = None,
    sessions: list[Any] | None = None,
    news: dict[str, Any] | None = None,
    prospective_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    del bars, sessions  # replay checks read the consensus assessment, not raw bars
    state: dict[str, Any] = {"vetoes": [], "warnings": [], "reason_codes": []}
    checks: dict[str, Any] = {}
    request = manifest.get("request") or {}
    snapshot_id = manifest.get("snapshot_id")
    policy = manifest.get("policy") or {}
    gate = manifest.get("data_validation") or {}

    if request.get("intent") != "forecast":
        _veto(state, "DESCRIPTIVE_REQUEST", "Request intent does not authorize a future-direction probability.")
    if not snapshot_id or not manifest.get("source_payload_sha256"):
        _veto(state, "SNAPSHOT_IDENTITY_INVALID", "Snapshot identity or source payload digest is missing.")
    for result in researchers:
        if result.get("snapshot_id") != snapshot_id:
            _veto(state, "SNAPSHOT_IDENTITY_INVALID", f"{result.get('researcher_id')} used a different snapshot identity.")
    checks["snapshot_identity"] = "checked"

    policy_ok = (
        policy.get("policy_version") == POLICY_VERSION
        and policy.get("coverage_status") == "complete"
        and bool(policy.get("calendar_verified"))
    )
    checks["policy"] = {
        "policy_version": policy.get("policy_version"),
        "coverage_status": policy.get("coverage_status"),
        "calendar_verified": policy.get("calendar_verified"),
        "expected_sessions": policy.get("expected_sessions"),
        "observed_sessions": policy.get("observed_sessions"),
        "missing_sessions": policy.get("missing_sessions"),
    }
    if policy.get("policy_version") != POLICY_VERSION:
        _veto(state, "POLICY_VERSION_MISMATCH", "Snapshot policy version does not match the active fixed-window policy.")
    if policy.get("coverage_status") != "complete":
        _veto(state, "INSUFFICIENT_HISTORY_COVERAGE",
              f"52-week coverage is {policy.get('coverage_status') or 'missing'}; expected "
              f"{policy.get('expected_sessions')} sessions, observed {policy.get('observed_sessions')}, "
              f"missing {policy.get('missing_sessions')}.")
    if not policy.get("calendar_verified"):
        _veto(state, "CALENDAR_UNVERIFIED", "Exchange calendar is unverified; a formal 5-session probability cannot be published.")
    price_basis = (gate.get("policy") or {}).get("price_basis") or policy.get("price_basis")
    checks["price_basis"] = price_basis
    if price_basis != PRICE_BASIS:
        _veto(state, "PRICE_BASIS_UNVERIFIED", "Price basis is not the declared split-adjusted close.")

    assessment = consensus.get("assessment") or {}
    checks["current_prediction_integrity_pass"] = bool(assessment.get("current_prediction_integrity_pass"))
    checks["oos_integrity_pass"] = bool(assessment.get("oos_integrity_pass"))
    if not assessment.get("current_prediction_integrity_pass"):
        _veto(state, "PREDICTION_INTEGRITY_FAILED", "Current probability could not be reproduced from frozen bars and fixed parameters.")
    if not assessment.get("oos_integrity_pass"):
        _veto(state, "OOS_INTEGRITY_FAILED", "At least one OOS probability record failed the integrity audit.")
    if consensus.get("prob_up") is not None and not consensus.get("production_probability_eligible"):
        _veto(state, "PRODUCTION_PROBABILITY_LEAKED", "A production probability is present although A is not eligible.")
    if consensus.get("aggregation_method") != "single_path" or int(consensus.get("production_path_count") or 0) != 1:
        _veto(state, "AGGREGATION_CONTRACT", "Consensus must use exactly one production path.")
    if consensus.get("confidence_score") is not None:
        _veto(state, "CONFIDENCE_SCORE_PRESENT", "A 0-100 aggregate confidence score must not be published.")
    for item in consensus.get("shadow_predictions") or []:
        if item.get("production_probability"):
            _veto(state, "SHADOW_PATH_PROMOTED", f"{item.get('path')} is shadow-only but is marked as production.")
        probability = item.get("probability")
        if probability is not None and (isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 <= probability <= 1):
            _veto(state, "SHADOW_PROBABILITY_INVALID", f"{item.get('path')} shadow probability is outside [0,1].")

    for result in researchers:
        role = result.get("result_role", "forecast")
        path = str(result.get("researcher_id"))
        probability = result.get("prob_up")
        if role == "diagnostic" and probability is not None:
            _veto(state, "DIAGNOSTIC_EMITTED_PROBABILITY", f"{path} is diagnostic-only and must not emit a probability.")
        if probability is not None:
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
                _veto(state, "PREDICTION_FAILED", f"{path} produced an invalid probability value.")
            if not result.get("probability_source"):
                _veto(state, "PREDICTION_FAILED", f"{path} probability has no provenance.")
        if role == "forecast":
            leakage = (result.get("validation") or {}).get("leakage_audit") or {}
            if leakage.get("future_returns_used_only_as_labels") is False:
                _veto(state, "LEAKAGE_DETECTED", f"{path} declared future returns outside the label role.")
            if leakage.get("features_use_data_through_decision_close_only") is False:
                _veto(state, "LEAKAGE_DETECTED", f"{path} declared features beyond the decision close.")

    validation = (next((r for r in researchers if r.get("researcher_id") == "quant"), {}) or {}).get("validation") or {}
    nonoverlap = int((assessment.get("oos_evidence_audit") or {}).get("nonoverlap_valid_count") or 0)
    checks["oos_nonoverlap"] = nonoverlap
    if nonoverlap < MIN_VALID_OOS_NONOVERLAP:
        _warn(state, "OOS_SUPPORT_INSUFFICIENT",
              f"Only {nonoverlap} valid non-overlapping OOS predictions; {MIN_VALID_OOS_NONOVERLAP} required for publication.")
    prospective = [row for row in (prospective_records or []) if row.get("sampled") is True and row.get("status") == "matured"]
    checks["prospective_nonoverlap"] = len(prospective)
    if not assessment.get("directional_edge_supported"):
        _warn(state, "DIRECTIONAL_EDGE_UNPROVEN",
              f"No supported directional edge; {len(prospective)}/{MIN_PROSPECTIVE_NONOVERLAP} prospective non-overlapping outcomes.")
    if validation.get("price_basis", {}).get("dividends_reinvested") is not False:
        _warn(state, "PRICE_BASIS_UNVERIFIED", "Price series does not explicitly declare dividends as excluded.")
    if (manifest.get("data_quality") or {}).get("overall") == "partial":
        _warn(state, "DATA_WARNING", "Snapshot has non-fatal data-quality limitations; coverage and price basis are reported separately.")
    if news is not None and (news.get("status") in {"unavailable", "not_assessed", None}):
        _warn(state, "NEWS_NOT_ASSESSED", "News evidence is unavailable or not assessed; it neither supports nor blocks the quant probability.")

    eligible = bool(consensus.get("production_probability_eligible")) and not state["vetoes"]
    may_report_probability = eligible and consensus.get("prob_up") is not None
    checks["news_relationship"] = "independent_of_quant_probability"
    return {
        "status": "approved" if may_report_probability else "vetoed",
        "verification_scope": "fixed 52-week / 5-session policy: transform replay, coverage, price basis, path roles, OOS integrity",
        "vetoes": state["vetoes"],
        "warnings": state["warnings"],
        "reason_codes": sorted(set(state["reason_codes"])),
        "may_report_probability": may_report_probability,
        "may_report_direction": may_report_probability,
        "production_eligibility": {
            "computable": bool(consensus.get("computable")),
            "current_prediction_integrity_pass": bool(consensus.get("current_prediction_integrity_pass")),
            "oos_integrity_pass": bool(consensus.get("oos_integrity_pass")),
            "production_probability_eligible": bool(consensus.get("production_probability_eligible")),
            "directional_edge_supported": bool(consensus.get("directional_edge_supported")),
            "trading_signal_eligible": False,
        },
        "checks": checks,
        "bias_audit": {
            "survivorship": "not_assessed_no_delisting_universe",
            "look_ahead": "transform replay verifies labels matured by each decision close",
            "data_snooping": "single predeclared model; no path or weight selection on this window",
            "costs": "gross close-to-close; no execution, spread, slippage or fees",
            "provider_revision": (manifest.get("policy") or {}).get("provider_revision_status", "disclosed"),
        },
        "confidence_score": None,
        "confidence_score_deprecated": True,
        "evidence_summary": consensus.get("evidence") or {},
        "policy_ok": policy_ok,
    }
