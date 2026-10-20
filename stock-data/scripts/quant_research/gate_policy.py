"""Central prediction/reporting/strategy gate policy.

Data quality and strategy evidence can lower report confidence without erasing a
numerical forecast. Only explicitly fatal prediction-integrity codes veto it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REASON_CATALOG: dict[str, dict[str, Any]] = {
    "INSUFFICIENT_OHLCV": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "SEVERE_MISSING_BARS": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "INVALID_TIME_ORDER": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "UNRECOVERABLE_DUPLICATE_BARS": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "INVALID_PRICE_VALUES": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "LATEST_PRICE_CONFLICT": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "LATEST_OBSERVATION_UNRESOLVED": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "CORPORATE_ACTION_CONTAMINATION": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "LEAKAGE_DETECTED": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "PREDICTION_FAILED": {"severity": "fatal", "scope": "prediction", "can_veto_prediction": True},
    "SOURCE_IDENTITY_UNVERIFIED": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "SOURCE_IDENTITY_CONFLICT": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "FREQUENCY_UNVERIFIED": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "TEMPORAL_PROVENANCE_MISSING": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "MARKET_UNITS_UNVERIFIED": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "MARKET_UNITS_CONFLICT": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "STALE_MARKET_DATA": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "DESCRIPTIVE_REQUEST": {"severity": "fatal", "scope": "reporting", "can_veto_prediction": True},
    "SNAPSHOT_IDENTITY_INVALID": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
    "CALENDAR_OFFICIAL_MISSING": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "CALENDAR_FALLBACK_USED": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "CALENDAR_SOURCE_INVALID": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "ADJUSTMENT_UNKNOWN": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "ADJUSTMENT_EVIDENCE_UNAVAILABLE": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "ADJUSTMENT_EVIDENCE_CONFLICT": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "CORPORATE_ACTION_UNKNOWN": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "LATEST_CLOSE_SINGLE_PROVIDER": {"severity": "warning", "scope": "data", "can_veto_prediction": False},
    "LATEST_CLOSE_CROSS_VERIFIED": {"severity": "info", "scope": "data", "can_veto_prediction": False},
    "ASSET_TYPE_UNVERIFIED": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "EXTREME_PRICE_MOVE_UNRECONCILED": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "OOS_INSUFFICIENT": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "OOS_EVIDENCE_INVALID": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "DATA_QUALITY_LOW": {"severity": "degraded", "scope": "data", "can_veto_prediction": False},
    "OOS_MARKER_MISSING": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "OOS_SELF_REPORT_MISMATCH": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "SURVIVORSHIP_NOT_ASSESSED": {"severity": "warning", "scope": "strategy", "can_veto_prediction": False},
    "CALIBRATION_MISSING": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "FACTOR_BACKTEST_INSUFFICIENT": {"severity": "warning", "scope": "validation", "can_veto_prediction": False},
    "LOW_FACTOR_AGREEMENT": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "PBO_NOT_ASSESSED": {"severity": "warning", "scope": "strategy", "can_veto_prediction": False},
    "DSR_NOT_ASSESSED": {"severity": "warning", "scope": "strategy", "can_veto_prediction": False},
    "TRANSACTION_COST_NOT_ASSESSED": {"severity": "warning", "scope": "strategy", "can_veto_prediction": False},
    "PORTFOLIO_BACKTEST_MISSING": {"severity": "warning", "scope": "strategy", "can_veto_prediction": False},
    "LEAKAGE_AUDIT_INCOMPLETE": {"severity": "degraded", "scope": "validation", "can_veto_prediction": False},
    "PATH_FAILED": {"severity": "warning", "scope": "validation", "can_veto_prediction": False},
    "DATA_WARNING": {"severity": "warning", "scope": "data", "can_veto_prediction": False},
    "PATH_WARNING": {"severity": "warning", "scope": "validation", "can_veto_prediction": False},
    "NEWS_NOT_ASSESSED": {"severity": "warning", "scope": "news", "can_veto_prediction": False},
}

PREDICTION_FATAL_CODES = frozenset(
    code for code, policy in REASON_CATALOG.items() if policy["can_veto_prediction"]
)
STRATEGY_ONLY_CODES = frozenset(
    code for code, policy in REASON_CATALOG.items() if policy["scope"] == "strategy"
)


def reason_policy(code: str) -> dict[str, Any]:
    """Return policy metadata; unknown codes fail closed as fatal diagnostics."""
    return REASON_CATALOG.get(code, {
        "severity": "fatal", "scope": "unknown", "can_veto_prediction": True,
    })


def classify_reasons(codes: Iterable[str]) -> dict[str, Any]:
    unique = list(dict.fromkeys(str(code) for code in codes))
    fatal = [code for code in unique if reason_policy(code)["can_veto_prediction"]]
    nonfatal = [code for code in unique if code not in fatal]
    warnings = [code for code in nonfatal if reason_policy(code)["severity"] in {"warning", "degraded"}]
    informational = [code for code in nonfatal if reason_policy(code)["severity"] == "info"]
    requires_degraded = any(reason_policy(code)["severity"] in {"warning", "degraded"}
                            and reason_policy(code)["scope"] != "strategy" for code in nonfatal)
    status = "veto" if fatal else "degraded" if requires_degraded else "pass"
    return {
        "status": status,
        "reason_codes": unique,
        "blockers": fatal,
        "warnings": warnings,
        "informational": informational,
        "prediction_eligible": not fatal,
    }


def reporting_status(*, prediction_available: bool, veto_codes: Iterable[str] = (), warning_codes: Iterable[str] = ()) -> str:
    """Resolve the final reporting state without allowing strategy-only warnings to veto."""
    classified = classify_reasons([*veto_codes, *warning_codes])
    if not prediction_available and not classified["blockers"]:
        return "veto"
    return classified["status"]


def analysis_status(*, may_report_direction: bool, reporting_state: str, researcher_statuses: Iterable[str],
                    news_status: str) -> str:
    """Derive the final analysis status from stage outcomes only.

    The report embeds this value, and its validation re-derives the same value from the
    frozen stages, so an edited status field cannot claim a state the stages did not reach.
    """
    if not may_report_direction:
        return "abstain"
    if (reporting_state == "degraded"
            or any(status not in {"success", "partial"} for status in researcher_statuses)
            or news_status != "complete"):
        return "degraded"
    return "complete_with_limitations"


POSITIVE_SKILL_GRADE = "positive_skill_with_estimated_interval_excluding_zero"


def build_report_eligibility(consensus: dict[str, Any], adversarial: dict[str, Any]) -> dict[str, Any]:
    """Split the three questions a reader actually asks.

    A reportable probability is not a reportable direction, and neither is a
    trading signal: entry timing, costs and an edge over the base rate are all
    separate claims.
    """
    probability = consensus.get("reportable_probability", consensus.get("prob_up"))
    probability_ok = isinstance(probability, (int, float)) and not isinstance(probability, bool)
    direction_ok = bool(adversarial.get("may_report_direction")) and probability_ok
    grade = str(consensus.get("predictive_evidence_grade", "insufficient_oos_evidence"))
    reasons = ["entry_basis_not_modelled", "transaction_costs_not_modelled"]
    if grade != POSITIVE_SKILL_GRADE:
        reasons.append("no_independent_edge_vs_expanding_base_rate")
    if not probability_ok:
        reasons.append("probability_not_reportable")
    if not direction_ok:
        reasons.append("direction_not_reportable")
    return {
        "probability_report": probability_ok,
        "direction_report": direction_ok,
        "trading_signal": False,
        "trading_signal_reasons": reasons,
        "predictive_evidence_grade": grade,
    }


def aggregate_gate_telemetry(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-analysis rows; callers can combine daily multi-ticker runs."""
    rows = list(records)
    total = len(rows)
    if total == 0:
        return {"sample_count": 0, "prediction_availability_rate": None, "abstain_rate": None,
                "degraded_rate": None, "pass_rate": None, "scope": "no samples"}
    available = sum(bool(row.get("prediction_available")) for row in rows)
    degraded = sum(row.get("reporting_status") == "degraded" for row in rows)
    passed = sum(row.get("reporting_status") == "pass" for row in rows)
    return {
        "sample_count": total,
        "prediction_availability_rate": round(available / total, 4),
        "abstain_rate": round((total - available) / total, 4),
        "degraded_rate": round(degraded / total, 4),
        "pass_rate": round(passed / total, 4),
        "scope": "per-analysis records supplied by caller; not a cross-ticker reliability claim unless sample_count covers that batch",
    }
