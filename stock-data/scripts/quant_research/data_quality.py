"""Classify market-data quality findings into data usability and reporting state.

Only reason codes emitted by ``data_checks``, ``snapshot`` and the
``forecast_policy`` error constants are catalogued.  Unknown codes fail closed as
fatal because a finding the classifier cannot explain must not be ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REASON_CATALOG: dict[str, dict[str, str]] = {
    # Emitted by data_checks.py.
    "INSUFFICIENT_OHLCV": {"severity": "fatal", "scope": "data"},
    "SEVERE_MISSING_BARS": {"severity": "fatal", "scope": "data"},
    "INVALID_TIME_ORDER": {"severity": "fatal", "scope": "data"},
    "INVALID_PRICE_VALUES": {"severity": "fatal", "scope": "data"},
    "LATEST_PRICE_CONFLICT": {"severity": "fatal", "scope": "data"},
    "LATEST_OBSERVATION_UNRESOLVED": {"severity": "fatal", "scope": "data"},
    "CORPORATE_ACTION_CONTAMINATION": {"severity": "fatal", "scope": "data"},
    "SOURCE_IDENTITY_UNVERIFIED": {"severity": "fatal", "scope": "data"},
    "SOURCE_IDENTITY_CONFLICT": {"severity": "fatal", "scope": "data"},
    "FREQUENCY_UNVERIFIED": {"severity": "fatal", "scope": "data"},
    "TEMPORAL_PROVENANCE_MISSING": {"severity": "fatal", "scope": "data"},
    "MARKET_UNITS_CONFLICT": {"severity": "fatal", "scope": "data"},
    "STALE_MARKET_DATA": {"severity": "fatal", "scope": "data"},
    "DECISION_SESSION_UNRESOLVED": {"severity": "fatal", "scope": "data"},
    "OHLCV_OUT_OF_WINDOW": {"severity": "fatal", "scope": "data"},
    "MARKET_UNITS_UNVERIFIED": {"severity": "degraded", "scope": "data"},
    "CALENDAR_OFFICIAL_MISSING": {"severity": "degraded", "scope": "data"},
    "CALENDAR_LIB_MISSING": {"severity": "degraded", "scope": "data"},
    "CALENDAR_FALLBACK_USED": {"severity": "degraded", "scope": "data"},
    "CALENDAR_SOURCE_INVALID": {"severity": "degraded", "scope": "data"},
    "ADJUSTMENT_UNKNOWN": {"severity": "degraded", "scope": "data"},
    "ADJUSTMENT_EVIDENCE_UNAVAILABLE": {"severity": "degraded", "scope": "data"},
    "ADJUSTMENT_EVIDENCE_CONFLICT": {"severity": "degraded", "scope": "data"},
    "CORPORATE_ACTION_UNKNOWN": {"severity": "degraded", "scope": "data"},
    "ASSET_TYPE_UNVERIFIED": {"severity": "degraded", "scope": "data"},
    "EXTREME_PRICE_MOVE_UNRECONCILED": {"severity": "degraded", "scope": "data"},
    "POINT_IN_TIME_UNVERIFIED": {"severity": "degraded", "scope": "data"},
    "LATEST_CLOSE_SINGLE_PROVIDER": {"severity": "warning", "scope": "data"},
    "LATEST_CLOSE_CROSS_VERIFIED": {"severity": "info", "scope": "data"},
    # Emitted by snapshot.py.
    "SNAPSHOT_IDENTITY_INVALID": {"severity": "fatal", "scope": "data"},
    # forecast_policy.py error constants.
    "FIXED_WINDOW_CONFLICT": {"severity": "fatal", "scope": "data"},
    "INSUFFICIENT_HISTORY_COVERAGE": {"severity": "fatal", "scope": "data"},
    "CALENDAR_UNVERIFIED": {"severity": "degraded", "scope": "data"},
    "PRICE_BASIS_UNVERIFIED": {"severity": "fatal", "scope": "data"},
    "POLICY_VERSION_MISMATCH": {"severity": "fatal", "scope": "data"},
    "FORECAST_REMOVED": {"severity": "fatal", "scope": "reporting"},
}

_FATAL_POLICY = {"severity": "fatal", "scope": "unknown"}


def reason_policy(code: str) -> dict[str, str]:
    """Return the policy for a code; unknown codes fail closed as fatal."""
    return dict(REASON_CATALOG.get(code, _FATAL_POLICY))


def classify_reasons(codes: Iterable[str]) -> dict[str, Any]:
    unique = list(dict.fromkeys(str(code) for code in codes))
    policies = {code: reason_policy(code) for code in unique}
    blockers = [code for code in unique if policies[code]["severity"] == "fatal"]
    warnings = [code for code in unique if policies[code]["severity"] in {"degraded", "warning"}]
    informational = [code for code in unique if policies[code]["severity"] == "info"]
    status = "veto" if blockers else "degraded" if warnings else "pass"
    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "informational": informational,
        "data_usable": not blockers,
    }
