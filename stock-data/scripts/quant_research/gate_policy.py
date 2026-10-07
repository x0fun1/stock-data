"""Central prediction/reporting/strategy gate policy.

Data quality and strategy evidence can lower report confidence without erasing a
numerical forecast. Only explicitly fatal prediction-integrity codes veto it.
"""

from __future__ import annotations

from typing import Any, Iterable


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
    "MARKET_UNITS_UNVERIFIED": {"severity": "fatal", "scope": "data", "can_veto_prediction": True},
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