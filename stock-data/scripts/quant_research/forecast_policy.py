"""Single source of truth for the fixed-52-week / 5-session forecast policy.

Every producer and auditor of a production forecast must read the constants and
helpers below instead of re-implementing the window, horizon, sampling phase or
version labels.  The policy is deliberately narrow:

* one production horizon (5 exchange sessions);
* one history window (364 calendar days, left-open / right-closed);
* one production path (``quant``); factor/ml are shadow candidates and the
  factor backtest is a descriptive diagnostic;
* no fitted calibrator is applied in this policy.

The module has no provider or filesystem access so it can be imported by the
collector, the freezer, the researchers, the auditors and the CLI alike.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

POLICY_VERSION = "fixed_52w_5d_v1"

#: History window length.  The window is ``(decision - 364d, decision]`` so it
#: spans 52 calendar weeks without importing an extra session at either edge.
HISTORY_CALENDAR_DAYS = 364

#: Forecast target: the close of the 5th exchange session after the decision
#: session, measured against the decision session close.
FORECAST_HORIZON_SESSIONS = 5
FORECAST_HORIZON_LABEL = f"{FORECAST_HORIZON_SESSIONS}D"
WINDOW_CLOSED = "right"

#: Evaluation sampling keeps the historical 6-session gap.  It is a conservative
#: sampling policy, not a mathematical upper bound on 5-session labels.
NONOVERLAP_GAP_SESSIONS = 6
MIN_VALID_OOS_NONOVERLAP = 12
MIN_PROSPECTIVE_NONOVERLAP = 30

#: Reference session and modulus for the fixed evaluation phase.  Sessions are
#: counted on the exchange calendar; the first session on/after the anchor is
#: ordinal 0.  Sampling selects ordinals divisible by the modulus.
SAMPLING_ANCHOR_SESSION = date(2000, 1, 3)
SAMPLING_MODULUS = 6

PRODUCTION_PATHS: tuple[str, ...] = ("quant",)
SHADOW_PATHS: tuple[str, ...] = ("factor", "ml")
DIAGNOSTIC_PATHS: tuple[str, ...] = ("factor_backtest",)

#: Version labels frozen for this policy.  A new target definition (for example
#: total return including dividends) must introduce a new target version rather
#: than silently sharing a ledger group with this one.
MODEL_VERSIONS: dict[str, str] = {
    "quant": "quant_causal_shrinkage_v1",
    "factor": "factor_restricted_two_honest_v1",
    "ml": "ml_compact_daily_v1",
    "factor_backtest": "factor_backtest_5d_descriptive_v1",
}
FEATURE_VERSIONS: dict[str, str] = {
    "quant": "roc20_v1",
    "factor": "factor_bucket_v1",
    "ml": "compact_v1",
    "factor_backtest": "factor_diagnostic_v1",
}
TARGET_VERSION = "split_adjusted_price_direction_5d_v1"

#: Price basis required by this policy: provider close that is split-adjusted
#: but excludes cash-dividend reinvestment.  A provider that only exposes a
#: total-return style adjusted close cannot satisfy this declaration.
PRICE_BASIS = "split_adjusted"
PRICE_BASIS_SOURCE = "provider_declared_split_adjusted_close"
PRICE_BASIS_NOTE = (
    "provider close, split-adjusted, cash dividends not reinvested; "
    "fields that include dividend adjustment are not accepted as this basis"
)

#: Prior strength and matching rules retained from the pre-policy shrinkage
#: estimator.  They are intentionally not tuned inside the 52-week window.
SHRINKAGE_PRIOR_STRENGTH = 12
SHRINKAGE_MIN_MATCHES = 12
SHRINKAGE_VERSION = "shrinkage_v1"
ROC_LOOKBACK_SESSIONS = 20
FACTOR_MIN_BUCKET_OBSERVATIONS = 6
ML_MIN_TRAIN_ROWS = 60
ML_WARMUP_SESSIONS = 20
FACTOR_BACKTEST_HOLDOUT_MIN = 20

#: Receipt stages required for a complete forecast run.
REQUIRED_STAGES: tuple[str, ...] = (
    "request",
    "collection",
    "data",
    "quant",
    "audit",
    "news",
    "cross-validation",
    "risk",
    "final",
)

#: Error codes raised by policy validation.  They are also declared in the CLI
#: schema so hosts can branch on them without parsing prose.
ERROR_UNSUPPORTED_HORIZON = "UNSUPPORTED_FORECAST_HORIZON"
ERROR_FIXED_WINDOW_CONFLICT = "FIXED_WINDOW_CONFLICT"
ERROR_INSUFFICIENT_COVERAGE = "INSUFFICIENT_HISTORY_COVERAGE"
ERROR_CALENDAR_UNVERIFIED = "CALENDAR_UNVERIFIED"
ERROR_PRICE_BASIS_UNVERIFIED = "PRICE_BASIS_UNVERIFIED"
ERROR_POLICY_MISMATCH = "POLICY_VERSION_MISMATCH"


class PolicyError(ValueError):
    """Validation failure carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def policy_metadata() -> dict[str, Any]:
    """Serialisable policy block written into requests, snapshots and reports."""

    return {
        "policy_version": POLICY_VERSION,
        "history_calendar_days": HISTORY_CALENDAR_DAYS,
        "horizon_sessions": FORECAST_HORIZON_SESSIONS,
        "horizon_label": FORECAST_HORIZON_LABEL,
        "window_closed": WINDOW_CLOSED,
        "target_version": TARGET_VERSION,
        "model_versions": dict(MODEL_VERSIONS),
        "feature_versions": dict(FEATURE_VERSIONS),
        "production_paths": list(PRODUCTION_PATHS),
        "shadow_paths": list(SHADOW_PATHS),
        "diagnostic_paths": list(DIAGNOSTIC_PATHS),
        "nonoverlap_gap_sessions": NONOVERLAP_GAP_SESSIONS,
        "min_valid_oos_nonoverlap": MIN_VALID_OOS_NONOVERLAP,
        "min_prospective_nonoverlap": MIN_PROSPECTIVE_NONOVERLAP,
        "price_basis": PRICE_BASIS,
        "price_basis_source": PRICE_BASIS_SOURCE,
        "sampling_anchor_session": SAMPLING_ANCHOR_SESSION.isoformat(),
        "sampling_modulus": SAMPLING_MODULUS,
        "calibration_policy": "disabled_by_policy",
    }


def validate_forecast_horizon(horizon: str, *, intent: str = "forecast") -> str:
    """Forecast intent accepts exactly 5D; descriptive/factor research keeps range support."""

    label = str(horizon).strip().upper()
    if intent == "forecast" and label != FORECAST_HORIZON_LABEL:
        raise PolicyError(
            ERROR_UNSUPPORTED_HORIZON,
            f"forecast horizon must be {FORECAST_HORIZON_LABEL} under {POLICY_VERSION}; got {label}",
        )
    return label


def window_bounds(decision_session: date | str) -> tuple[str, str]:
    """Return ``(window_start_exclusive, window_end_inclusive)`` as ISO dates."""

    end = date.fromisoformat(decision_session) if isinstance(decision_session, str) else decision_session
    start = end - timedelta(days=HISTORY_CALENDAR_DAYS)
    return start.isoformat(), end.isoformat()


def window_contains(session: date | str, start_exclusive: str, end_inclusive: str) -> bool:
    value = session.isoformat() if isinstance(session, date) else str(session)
    return start_exclusive < value <= end_inclusive


def validate_declared_window(
    declared: dict[str, Any] | None,
    decision_session: date | str,
    *,
    allow_missing: bool = True,
) -> tuple[str, str]:
    """Reject user-supplied windows that do not match the policy boundary.

    A request may omit the window (the collector computes it).  If the request
    supplies one, both edges must equal the computed boundary; silently
    accepting a different window would let a caller smuggle in extra history.
    """

    start, end = window_bounds(decision_session)
    if not declared:
        if allow_missing:
            return start, end
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, "history window is required")
    declared_start = declared.get("start")
    declared_end = declared.get("end")
    if declared_start and declared_start != start:
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, f"history start {declared_start} does not match policy boundary {start}")
    if declared_end and declared_end != end:
        raise PolicyError(ERROR_FIXED_WINDOW_CONFLICT, f"history end {declared_end} does not match policy boundary {end}")
    return start, end


def session_ordinal(sessions: Sequence[date], session: date) -> int | None:
    """Return the zero-based ordinal of ``session`` on the supplied calendar."""

    anchor_index = next((index for index, item in enumerate(sessions) if item >= SAMPLING_ANCHOR_SESSION), None)
    if anchor_index is None:
        return None
    try:
        index = next(index for index, item in enumerate(sessions) if item == session)
    except StopIteration:
        return None
    if index < anchor_index:
        return None
    return index - anchor_index


def is_sampled_ordinal(ordinal: int | None) -> bool:
    return ordinal is not None and ordinal % SAMPLING_MODULUS == 0


def sessions_between(sessions: Iterable[date], start_exclusive: str, end_inclusive: str) -> list[date]:
    """Sessions strictly after ``start_exclusive`` and at/before ``end_inclusive``."""

    return [item for item in sessions if start_exclusive < item.isoformat() <= end_inclusive]


def horizon_session(sessions: Sequence[date], decision: date, horizon: int = FORECAST_HORIZON_SESSIONS) -> date | None:
    """Return the close-to-close target session, counted on the exchange calendar."""

    try:
        index = next(index for index, item in enumerate(sessions) if item == decision)
    except StopIteration:
        return None
    target = index + horizon
    return sessions[target] if target < len(sessions) else None


def role_for(path: str) -> str:
    if path in PRODUCTION_PATHS:
        return "production"
    if path in SHADOW_PATHS:
        return "shadow"
    if path in DIAGNOSTIC_PATHS:
        return "diagnostic"
    raise ValueError(f"unknown researcher path: {path}")


def model_identity(path: str) -> dict[str, str]:
    """Identity fields stamped on every prediction and ledger record."""

    return {
        "policy_version": POLICY_VERSION,
        "model_version": MODEL_VERSIONS[path],
        "feature_version": FEATURE_VERSIONS[path],
        "target_version": TARGET_VERSION,
    }


def ledger_group_key(*, ticker: str, market: str, path: str) -> str:
    identity = model_identity(path)
    return "|".join(
        (
            ticker.upper(),
            market.upper(),
            identity["policy_version"],
            identity["model_version"],
            identity["feature_version"],
            identity["target_version"],
        )
    )


def describe_window(decision_session: str) -> str:
    start, end = window_bounds(decision_session)
    return f"截至{end}的52周（{HISTORY_CALENDAR_DAYS}个自然日，窗口({start}, {end}]）"
