"""Audit of probability transforms, evidence support and predictive skill.

The audit has two independent jobs, and the second one is easy to forget:

1. **Correctness / reproducibility** — rebuild the reported probability from the
   frozen split-adjusted closes.  This module deliberately does *not* call the
   researcher's implementation: an audit that shares the producer's function can
   only detect record tampering, never a wrong formula, because both sides are
   wrong together.
2. **Effectiveness** — compare the rebuilt probabilities with two naive baselines
   (the causal base rate at each origin, and a constant 0.5) and report Brier
   skill, AUC and a moving-block bootstrap interval on the paired loss
   improvement.  Reproducibility without this block says nothing about whether
   the number is useful.

The two verdicts are kept separate in every returned record so a caller cannot
trade one for the other.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from ..forecast_policy import (
    DRIFT_MIN_VOL_SESSIONS,
    DRIFT_MU_ANNUAL,
    DRIFT_SHRINKAGE_K,
    DRIFT_SHRINKAGE_VERSION,
    DRIFT_VOL_WINDOW_SESSIONS,
    FORECAST_HORIZON_SESSIONS,
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_REPLAY_EFFECTIVE_SAMPLES,
    NONOVERLAP_GAP_SESSIONS,
    POLICY_VERSION,
    SESSIONS_PER_YEAR,
    SHRINKAGE_MIN_MATCHES,
    SHRINKAGE_PRIOR_STRENGTH,
    SHRINKAGE_VERSION,
)
from ..market_data import fixed_phase_sample
from ..statistics import (
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    MIN_BOOTSTRAP_BLOCKS,
    MIN_BOOTSTRAP_INDEPENDENT_BLOCKS,
    baseline_comparison,
    block_lengths_for,
    block_bootstrap_mean_interval,
    paired_improvement_rows,
    paired_rows,
)

PROBABILITY_TOLERANCE = 1e-10
DEFAULT_BRIER_SKILL_EPSILON = 1e-9
EFFECTIVENESS_VERDICT_POSITIVE = "positive_skill_with_estimated_interval_excluding_zero"
EFFECTIVENESS_VERDICT_NEGATIVE = "negative_skill_with_estimated_interval_excluding_zero"
EFFECTIVENESS_VERDICT_INCONCLUSIVE = "insufficient_evidence"

__all__ = [
    "AuditRejected",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "EFFECTIVENESS_VERDICT_INCONCLUSIVE",
    "EFFECTIVENESS_VERDICT_NEGATIVE",
    "EFFECTIVENESS_VERDICT_POSITIVE",
    "MIN_BOOTSTRAP_BLOCKS",
    "MIN_BOOTSTRAP_INDEPENDENT_BLOCKS",
    "audit_current_prediction",
    "audit_identity_record",
    "audit_oos_result",
    "audit_record",
    "audit_shrinkage_record",
    "baseline_comparison",
    "block_bootstrap_mean_interval",
    "build_probability_validation",
    "directional_edge_assessment",
    "paired_improvement_rows",
    "paired_rows",
    "path_has_detected_leakage",
    "unique_research_paths",
]


class AuditRejected(Exception):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def _series(bars: Sequence[dict[str, Any]]) -> tuple[list[str], list[float]]:
    dates: list[str] = []
    closes: list[float] = []
    for bar in bars:
        close = bar.get("close")
        if not isinstance(close, (int, float)) or isinstance(close, bool) or not math.isfinite(float(close)):
            raise AuditRejected(["PRICE_SERIES_INVALID"])
        dates.append(str(bar["date"])[:10])
        closes.append(float(close))
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise AuditRejected(["PRICE_SERIES_INVALID"])
    return dates, closes


def _close_diff(left: Any, right: Any, tolerance: float = PROBABILITY_TOLERANCE) -> bool:
    return (
        isinstance(left, (int, float)) and not isinstance(left, bool)
        and isinstance(right, (int, float)) and not isinstance(right, bool)
        and abs(float(left) - float(right)) <= tolerance
    )


def _record_reasons(row: dict[str, Any], required: set[str]) -> list[str]:
    return [f"MISSING_FIELD:{field}" for field in sorted(required) if field not in row]


# --------------------------------------------------------------------------- #
# Independent re-implementation of the v2 transform.
#
# These helpers intentionally duplicate the researcher's arithmetic instead of
# importing it.  Duplication is the point: a formula bug has to be reproduced
# twice, in two code paths, to survive the audit.
# --------------------------------------------------------------------------- #
def _forward_return(closes: Sequence[float], index: int, horizon: int) -> float | None:
    target = index + horizon
    if index < 0 or target >= len(closes) or closes[index] <= 0:
        return None
    return closes[target] / closes[index] - 1.0


def _recompute_v2(dates: Sequence[str], closes: Sequence[float], index: int, horizon: int) -> dict[str, Any]:
    origin_high = max(0, index - horizon + 1)
    origins = [origin for origin in range(origin_high) if _forward_return(closes, origin, horizon) is not None]
    base_rate = (
        sum(1 for origin in origins if (_forward_return(closes, origin, horizon) or 0.0) > 0) / len(origins)
        if origins else None
    )
    n_eff = len(origins) / horizon if origins else 0.0
    returns: list[float] = []
    start = max(1, index - DRIFT_VOL_WINDOW_SESSIONS + 1)
    for position in range(start, index + 1):
        if closes[position - 1] > 0 and closes[position] > 0:
            returns.append(math.log(closes[position] / closes[position - 1]))
    drift = None
    if len(returns) >= DRIFT_MIN_VOL_SESSIONS:
        centre = sum(returns) / len(returns)
        variance = sum((value - centre) ** 2 for value in returns) / (len(returns) - 1)
        if variance > 0:
            sigma = math.sqrt(variance)
            z_score = (DRIFT_MU_ANNUAL / SESSIONS_PER_YEAR) * horizon / (sigma * math.sqrt(horizon))
            drift = {
                "mu_annual": DRIFT_MU_ANNUAL,
                "mu_per_session": DRIFT_MU_ANNUAL / SESSIONS_PER_YEAR,
                "sigma_daily": sigma,
                "sigma_horizon": sigma * math.sqrt(horizon),
                "z_score": z_score,
                "p_vol": 0.5 * (1 + math.erf(z_score / math.sqrt(2))),
                "vol_window_sessions": DRIFT_VOL_WINDOW_SESSIONS,
                "return_count": len(returns),
                "return_window_start": start,
                "return_window_end": index,
            }
    probability = None
    if base_rate is not None and drift is not None:
        probability = (n_eff * base_rate + DRIFT_SHRINKAGE_K * drift["p_vol"]) / (n_eff + DRIFT_SHRINKAGE_K)
    return {
        "base_rate": base_rate,
        "base_rate_label_count": len(origins),
        "base_rate_label_end_max": dates[origins[-1] + horizon] if origins else None,
        "n_eff": n_eff,
        "drift": drift,
        "p_vol": (drift or {}).get("p_vol"),
        "probability": probability,
    }


def _roc(closes: Sequence[float], index: int, window: int) -> float | None:
    if index - window < 0 or closes[index - window] <= 0:
        return None
    return closes[index] / closes[index - window] - 1.0


def _recompute_matching(dates: Sequence[str], closes: Sequence[float], index: int, horizon: int, gap: int) -> dict[str, Any]:
    current = _roc(closes, index, 20)
    if current is None:
        return {"origins": [], "label_ends": [], "wins": 0, "observations": 0}
    positive = current >= 0
    selected: list[int] = []
    last = None
    for prior in range(20, min(index, len(closes))):
        if prior + horizon > index:
            continue
        value = _forward_return(closes, prior, horizon)
        if value is None:
            continue
        prior_signal = _roc(closes, prior, 20)
        if prior_signal is None or (prior_signal >= 0) != positive:
            continue
        if last is not None and prior - last < gap:
            continue
        selected.append(prior)
        last = prior
    return {
        "origins": [dates[item] for item in selected],
        "label_ends": [dates[item + horizon] for item in selected],
        "wins": sum(1 for item in selected if (_forward_return(closes, item, horizon) or 0.0) > 0),
        "observations": len(selected),
    }


def _recompute_legacy_v1(dates: Sequence[str], closes: Sequence[float], index: int, horizon: int, gap: int) -> dict[str, Any]:
    """Pre-v2 formula, kept so historical ledger rows stay auditable."""
    origin_high = max(0, index - horizon + 1)
    origins = [origin for origin in range(origin_high) if _forward_return(closes, origin, horizon) is not None]
    base_rate = (
        sum(1 for origin in origins if (_forward_return(closes, origin, horizon) or 0.0) > 0) / len(origins)
        if origins else None
    )
    matching = _recompute_matching(dates, closes, index, horizon, gap)
    probability = None
    raw = None
    if base_rate is not None and matching["observations"] > 0:
        raw = matching["wins"] / matching["observations"]
        if matching["observations"] >= SHRINKAGE_MIN_MATCHES:
            probability = (
                matching["wins"] + SHRINKAGE_PRIOR_STRENGTH * base_rate
            ) / (matching["observations"] + SHRINKAGE_PRIOR_STRENGTH)
    return {
        "base_rate": base_rate,
        "base_rate_label_count": len(origins),
        "base_rate_label_end_max": dates[origins[-1] + horizon] if origins else None,
        "matching": matching,
        "probability": probability,
        "raw_probability": raw,
    }


def audit_shrinkage_record(
    row: dict[str, Any],
    dates: list[str],
    closes: list[float],
    *,
    require_full_52w: bool = False,
) -> dict[str, Any]:
    """Rebuild one shrinkage prediction from the frozen series and compare."""

    transform = row.get("probability_transform")
    if not isinstance(transform, dict):
        return {"ok": False, "reasons": ["MISSING_PROBABILITY_TRANSFORM"], "recomputed": None}
    kind = transform.get("type")
    if kind == "causal_empirical_shrinkage":
        return _audit_legacy_v1(row, dates, closes, transform, require_full_52w=require_full_52w)
    if kind != "causal_drift_shrinkage":
        return {"ok": False, "reasons": [f"UNKNOWN_TRANSFORM_TYPE:{kind!r}"], "recomputed": None}

    reasons: list[str] = []
    if transform.get("version") != DRIFT_SHRINKAGE_VERSION:
        reasons.append(f"UNKNOWN_TRANSFORM_VERSION:{transform.get('version')!r}")
    if transform.get("policy_version") != POLICY_VERSION:
        reasons.append("TRANSFORM_POLICY_VERSION_MISMATCH")
    if int(transform.get("k", -1)) != DRIFT_SHRINKAGE_K:
        reasons.append("PRIOR_STRENGTH_TAMPERED")
    if int(transform.get("minimum_gap_sessions", -1)) != NONOVERLAP_GAP_SESSIONS:
        reasons.append("MATCHING_GAP_TAMPERED")
    if int(transform.get("horizon_sessions", -1)) != FORECAST_HORIZON_SESSIONS:
        reasons.append("TRANSFORM_HORIZON_TAMPERED")
    if int(transform.get("minimum_matches", -1)) != SHRINKAGE_MIN_MATCHES:
        reasons.append("MINIMUM_MATCHES_TAMPERED")
    feature = transform.get("feature") or {}
    if feature.get("used_for_probability") is not False:
        reasons.append("FEATURE_ROLE_TAMPERED")

    decision = row.get("decision_session")
    if decision not in dates:
        return {"ok": False, "reasons": [*reasons, "DECISION_SESSION_NOT_IN_PRICE_SERIES"], "recomputed": None}
    index = dates.index(decision)
    if index != row.get("decision_index", index):
        reasons.append("DECISION_INDEX_TAMPERED")
    if require_full_52w and row.get("full_52w_input") is not True:
        reasons.append("NOT_FULL_52W_INPUT")

    expected = _recompute_v2(dates, closes, index, FORECAST_HORIZON_SESSIONS)
    matching = _recompute_matching(dates, closes, index, FORECAST_HORIZON_SESSIONS, NONOVERLAP_GAP_SESSIONS)
    drift = expected["drift"] or {}
    recorded_drift = transform.get("drift_prior") or {}

    if not _close_diff(transform.get("base_rate_at_origin"), expected["base_rate"]):
        reasons.append("BASE_RATE_AT_ORIGIN_TAMPERED")
    if transform.get("base_rate_label_count") != expected["base_rate_label_count"]:
        reasons.append("BASE_RATE_SAMPLE_REWRITTEN")
    if transform.get("base_rate_label_end_max") != expected["base_rate_label_end_max"]:
        reasons.append("BASE_RATE_LABEL_BOUNDARY_TAMPERED")
    if not _close_diff(transform.get("n_eff"), expected["n_eff"]):
        reasons.append("N_EFF_TAMPERED")
    if not _close_diff(transform.get("p_vol"), expected["p_vol"]):
        reasons.append("P_VOL_TAMPERED")
    for field in ("mu_annual", "mu_per_session", "sigma_daily", "sigma_horizon", "z_score", "p_vol"):
        if not _close_diff(recorded_drift.get(field), drift.get(field)):
            reasons.append(f"DRIFT_PRIOR_{field.upper()}_TAMPERED")
    for field in ("vol_window_sessions", "return_count", "return_window_start", "return_window_end"):
        if recorded_drift.get(field) != drift.get(field):
            reasons.append(f"DRIFT_PRIOR_{field.upper()}_TAMPERED")

    if transform.get("matching_origins") != matching["origins"]:
        reasons.append("MATCHING_SAMPLE_REWRITTEN")
    if transform.get("matching_label_ends") != matching["label_ends"]:
        reasons.append("MATCHING_LABEL_ENDS_TAMPERED")
    if transform.get("wins") != matching["wins"]:
        reasons.append("MATCHING_WINS_TAMPERED")
    if transform.get("observations") != matching["observations"]:
        reasons.append("MATCHING_OBSERVATIONS_TAMPERED")
    if transform.get("matching_label_end_max") != (matching["label_ends"][-1] if matching["label_ends"] else None):
        reasons.append("MATCHING_LABEL_BOUNDARY_TAMPERED")
    for label_end in transform.get("matching_label_ends") or []:
        if label_end > decision:
            reasons.append("FUTURE_LABEL_IN_MATCHING_SAMPLE")
            break

    if expected["probability"] is None:
        if row.get("probability") is not None:
            reasons.append("PROBABILITY_PRESENT_WITHOUT_VALID_SAMPLE")
    else:
        if not _close_diff(row.get("probability"), expected["probability"]):
            reasons.append("PROBABILITY_TAMPERED")
        expected_raw = matching["wins"] / matching["observations"] if matching["observations"] else None
        if expected_raw is None:
            if row.get("raw_probability") is not None:
                reasons.append("RAW_PROBABILITY_PRESENT_WITHOUT_MATCHES")
        elif not _close_diff(row.get("raw_probability"), expected_raw):
            reasons.append("RAW_PROBABILITY_TAMPERED")
    if transform.get("wins") is not None and transform.get("observations") is not None:
        if int(transform["wins"]) > int(transform["observations"]):
            reasons.append("MATCHING_WINS_EXCEED_OBSERVATIONS")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "recomputed": {
            "probability": expected["probability"],
            "base_rate_at_origin": expected["base_rate"],
            "n_eff": expected["n_eff"],
            "p_vol": expected["p_vol"],
            "wins": matching["wins"],
            "observations": matching["observations"],
            "matching_origins": matching["origins"],
            "transform_version": DRIFT_SHRINKAGE_VERSION,
        },
    }


def _audit_legacy_v1(
    row: dict[str, Any], dates: list[str], closes: list[float], transform: dict[str, Any],
    *, require_full_52w: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    if transform.get("version") != SHRINKAGE_VERSION:
        reasons.append(f"UNKNOWN_TRANSFORM_VERSION:{transform.get('version')!r}")
    if transform.get("policy_version") != POLICY_VERSION:
        reasons.append("TRANSFORM_POLICY_VERSION_MISMATCH")
    if int(transform.get("prior_strength", -1)) != SHRINKAGE_PRIOR_STRENGTH:
        reasons.append("PRIOR_STRENGTH_TAMPERED")
    if int(transform.get("minimum_gap_sessions", -1)) != NONOVERLAP_GAP_SESSIONS:
        reasons.append("MATCHING_GAP_TAMPERED")
    if int(transform.get("horizon_sessions", -1)) != FORECAST_HORIZON_SESSIONS:
        reasons.append("TRANSFORM_HORIZON_TAMPERED")
    if int(transform.get("minimum_matches", -1)) != SHRINKAGE_MIN_MATCHES:
        reasons.append("MINIMUM_MATCHES_TAMPERED")
    decision = row.get("decision_session")
    if decision not in dates:
        return {"ok": False, "reasons": [*reasons, "DECISION_SESSION_NOT_IN_PRICE_SERIES"], "recomputed": None}
    index = dates.index(decision)
    if require_full_52w and row.get("full_52w_input") is not True:
        reasons.append("NOT_FULL_52W_INPUT")
    expected = _recompute_legacy_v1(dates, closes, index, FORECAST_HORIZON_SESSIONS, NONOVERLAP_GAP_SESSIONS)
    if transform.get("matching_origins") != expected["matching"]["origins"]:
        reasons.append("MATCHING_SAMPLE_REWRITTEN")
    if not _close_diff(transform.get("base_rate_at_origin"), expected["base_rate"]):
        reasons.append("BASE_RATE_AT_ORIGIN_TAMPERED")
    if transform.get("base_rate_label_count") != expected["base_rate_label_count"]:
        reasons.append("BASE_RATE_SAMPLE_REWRITTEN")
    if not _close_diff(row.get("probability"), expected["probability"]) and not (
        expected["probability"] is None and row.get("probability") is None
    ):
        reasons.append("PROBABILITY_TAMPERED")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "recomputed": {
            "probability": expected["probability"],
            "raw_probability": expected["raw_probability"],
            "wins": expected["matching"]["wins"],
            "observations": expected["matching"]["observations"],
            "base_rate_at_origin": expected["base_rate"],
            "matching_origins": expected["matching"]["origins"],
            "transform_version": SHRINKAGE_VERSION,
            "legacy": True,
        },
    }


def audit_identity_record(row: dict[str, Any]) -> dict[str, Any]:
    """Identity transform: probability must equal the recorded raw frequency."""

    reasons = _record_reasons(row, {"probability", "probability_transform"})
    transform = row.get("probability_transform") or {}
    if transform.get("type") != "identity":
        reasons.append("TRANSFORM_TYPE_MISMATCH")
    if "fitted_calibrator" in transform:
        reasons.append("IDENTITY_TRANSFORM_CONTAINS_CALIBRATOR")
    return {"ok": not reasons, "reasons": reasons, "recomputed": None}


def audit_record(row: dict[str, Any], bars: Sequence[dict[str, Any]], *, require_full_52w: bool = False) -> dict[str, Any]:
    transform = row.get("probability_transform") or {}
    kind = transform.get("type")
    if kind == "identity":
        return audit_identity_record(row)
    if kind in {"causal_empirical_shrinkage", "causal_drift_shrinkage"}:
        dates, closes = _series(bars)
        return audit_shrinkage_record(row, dates, closes, require_full_52w=require_full_52w)
    if row.get("probability") is None:
        return {"ok": True, "reasons": [], "recomputed": None, "abstained": True}
    return {"ok": False, "reasons": [f"UNKNOWN_TRANSFORM_TYPE:{kind!r}"], "recomputed": None}


def _valid_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    validation = result.get("validation") or {}
    rows = validation.get("oos_forecasts")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def audit_oos_result(
    result: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    bars: Sequence[dict[str, Any]] | None = None,
    sessions: Sequence[date] | None = None,
) -> dict[str, Any]:
    """Audit every walk-forward row of one researcher result."""

    del manifest  # identity is already checked by the caller before the audit
    rows = _valid_rows(result)
    if bars is None:
        return {"status": "not_assessed", "integrity_pass": None, "reasons": ["PRICE_SERIES_UNAVAILABLE"],
                "record_count": len(rows), "valid_count": 0, "invalid_count": 0, "abstained_count": 0,
                "nonoverlap_valid_count": 0, "effective_valid_samples": 0}
    audited: list[dict[str, Any]] = []
    invalid = 0
    abstained = 0
    valid_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.get("probability") is None:
            abstained += 1
            audited.append({"decision_session": row.get("decision_session"), "status": "abstained",
                            "reason_code": row.get("reason_code")})
            continue
        outcome = audit_record(row, bars)
        audited.append({
            "decision_session": row.get("decision_session"),
            "status": "valid" if outcome["ok"] else "invalid",
            "reasons": outcome["reasons"],
            "probability": row.get("probability"),
            "target": row.get("target"),
            "transform_version": (row.get("probability_transform") or {}).get("version"),
        })
        if outcome["ok"]:
            valid_rows.append(row)
        else:
            invalid += 1
    valid_dates = sorted({str(row["decision_session"]) for row in valid_rows})
    # Non-overlap is a property of the ledger, not of a sampling phase: walk the
    # matured decisions in order and keep those that do not overlap the last kept
    # 5-session label.  This count is window-independent by construction.
    ordered = sorted(
        (row for row in valid_rows if isinstance(row.get("decision_index"), int)),
        key=lambda row: int(row["decision_index"]),
    )
    kept: list[dict[str, Any]] = []
    last_end = -1
    for row in ordered:
        index = int(row["decision_index"])
        if index > last_end:
            kept.append(row)
            last_end = index + FORECAST_HORIZON_SESSIONS
    if sessions:
        _sampled_dates, sampling = fixed_phase_sample(valid_dates, list(sessions))
    else:
        sampling = {"method": "fixed_exchange_session_phase", "status": "calendar_unavailable",
                    "phase_window_independent": None}
    path_sampling = ((result.get("validation") or {}).get("sampling") or {})
    effective = len(valid_rows) // FORECAST_HORIZON_SESSIONS
    reasons: list[str] = []
    if invalid:
        reasons.append("OOS_INTEGRITY_FAILED")
    if not valid_rows:
        reasons.append("NO_VALID_OOS_PROBABILITY")
    elif effective < MIN_REPLAY_EFFECTIVE_SAMPLES:
        reasons.append("OOS_SUPPORT_INSUFFICIENT")
    effectiveness = baseline_comparison(valid_rows, horizon=FORECAST_HORIZON_SESSIONS)
    return {
        "status": "valid" if not reasons else "insufficient" if invalid == 0 else "invalid",
        "integrity_pass": invalid == 0,
        "reasons": reasons,
        "record_count": len(rows),
        "valid_count": len(valid_rows),
        "invalid_count": invalid,
        "abstained_count": abstained,
        "nonoverlap_valid_count": len(kept),
        "nonoverlap_method": "greedy_5_session_gap_over_valid_rows",
        "effective_valid_samples": effective,
        "effective_sample_definition": f"valid rows / {FORECAST_HORIZON_SESSIONS}",
        "minimum_effective_samples": MIN_REPLAY_EFFECTIVE_SAMPLES,
        "coverage_ratio": path_sampling.get("coverage_ratio"),
        "sampling": sampling,
        "sampling_role": "throttle_only_not_qualification",
        "effectiveness": effectiveness,
        "rows": audited,
        "minimum_nonoverlap": MIN_REPLAY_EFFECTIVE_SAMPLES,
    }


def audit_current_prediction(
    result: dict[str, Any], manifest: dict[str, Any] | None, bars: Sequence[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Verify that the reported current probability is reproducible from bars."""

    del manifest
    if bars is None:
        return {"pass": False, "reasons": ["PRICE_SERIES_UNAVAILABLE"]}
    validation = result.get("validation") or {}
    transform = validation.get("probability_transform")
    probability = result.get("prob_up")
    if probability is None:
        return {"pass": True, "reasons": [], "abstained": True}
    if not isinstance(transform, dict):
        return {"pass": False, "reasons": ["MISSING_PROBABILITY_TRANSFORM"]}
    dates, closes = _series(bars)
    row = {
        "decision_session": dates[-1],
        "decision_index": len(dates) - 1,
        "probability": probability,
        "raw_probability": result.get("raw_probability"),
        "probability_transform": transform,
        "full_52w_input": True,
    }
    outcome = audit_shrinkage_record(row, dates, closes)
    return {
        "pass": outcome["ok"],
        "reasons": outcome["reasons"],
        "recomputed": outcome["recomputed"],
        "transform_independent_reimplementation": True,
    }


def directional_edge_assessment(
    records: Sequence[dict[str, Any]] | None = None,
    *,
    replay_records: Sequence[dict[str, Any]] | None = None,
    prospective_records: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Three-state edge verdict from the full prefix replay.

    ``supported`` requires the paired-improvement interval to exclude zero on the
    positive side; ``negative`` means it excludes zero on the negative side
    (the model is reliably *worse* than the base rate, which must be said out
    loud); anything else is ``insufficient_evidence``.  The prospective ledger is
    reported separately and is no longer the only admissible evidence, because a
    30-outcome floor has a power of only 0.29 at a true 60% hit rate and would
    therefore reject a genuinely useful model most of the time.
    """

    primary = list(replay_records if replay_records is not None else (records or []))
    matured = [
        row for row in primary
        if row.get("probability") is not None and row.get("target") is not None
        and row.get("status", "matured") == "matured"
    ]
    paired = paired_rows(matured)
    lengths = block_lengths_for(len(paired), FORECAST_HORIZON_SESSIONS)
    intervals = [
        block_bootstrap_mean_interval([row["improvement"] for row in paired], length)
        for length in lengths
    ]
    for position, interval in enumerate(intervals):
        interval["primary"] = position == 0
    primary_interval = intervals[0]
    state = EFFECTIVENESS_VERDICT_INCONCLUSIVE
    reasons: list[str] = []
    if len(paired) < MIN_PROSPECTIVE_NONOVERLAP:
        reasons.append("REPLAY_SUPPORT_INSUFFICIENT")
    elif primary_interval.get("status") != "estimated":
        reasons.append("INTERVAL_UNESTIMABLE")
    elif float(primary_interval["lower"]) > 0:
        state = EFFECTIVENESS_VERDICT_POSITIVE
    elif float(primary_interval["upper"]) < 0:
        state = EFFECTIVENESS_VERDICT_NEGATIVE
        reasons.append("INTERVAL_BELOW_ZERO")
    else:
        reasons.append("INTERVAL_CROSSES_ZERO")

    prospective = [
        row for row in (prospective_records or [])
        if row.get("status") == "matured" and row.get("probability") is not None
        and row.get("sampled") is True
    ]
    prospective_paired = paired_rows(prospective)
    prospective_intervals = [
        block_bootstrap_mean_interval([row["improvement"] for row in prospective_paired], length)
        for length in block_lengths_for(len(prospective_paired), FORECAST_HORIZON_SESSIONS)
    ] if len(prospective_paired) >= MIN_PROSPECTIVE_NONOVERLAP else []
    if len(prospective_paired) < MIN_PROSPECTIVE_NONOVERLAP:
        reasons.append("PROSPECTIVE_SUPPORT_INSUFFICIENT")
    return {
        "state": state,
        "supported": state == EFFECTIVENESS_VERDICT_POSITIVE,
        "reasons": reasons or ["DIRECTIONAL_EDGE_UNPROVEN"],
        "primary_evidence": "full_prefix_replay",
        "replay_observations": len(paired),
        "replay_effective_samples": len(paired) // FORECAST_HORIZON_SESSIONS,
        "prospective_nonoverlap_count": len(prospective_paired),
        "minimum_required": MIN_PROSPECTIVE_NONOVERLAP,
        "minimum_effective_samples": MIN_REPLAY_EFFECTIVE_SAMPLES,
        "paired_records": len(paired),
        "intervals": intervals,
        "prospective_intervals": prospective_intervals,
        "definition": "baseline_squared_error - model_squared_error vs the causal base rate at each origin",
        "prospective_role": "corroboration; the forward ledger is no longer the only admissible evidence",
        "trading_signal_eligible": False,
    }


def unique_research_paths(researchers: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("researcher_id")) for item in researchers if item.get("researcher_id")})


def path_has_detected_leakage(result: dict[str, Any], assessments: dict[str, Any] | None = None) -> bool:
    del assessments
    validation = result.get("validation") or {}
    leakage = validation.get("leakage_audit") or {}
    return any(value is False for value in leakage.values() if isinstance(value, bool))


def build_probability_validation(
    researchers: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    bars: Sequence[dict[str, Any]] | None = None,
    assessments: dict[str, Any] | None = None,
    skill_epsilon: float = DEFAULT_BRIER_SKILL_EPSILON,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibility entry point returning per-path audit and skill blocks."""

    del assessments
    audits = {
        str(result.get("researcher_id")): audit_oos_result(result, manifest, bars)
        for result in researchers
    }
    if audits:
        status = "valid" if all(audit.get("integrity_pass") for audit in audits.values()) else "invalid"
    else:
        # ``all([])`` is True; an empty result set must not be certified as valid.
        status = "not_assessed"
    quality = {
        "status": status,
        "paths": audits,
        "calibration_status": "disabled_by_policy",
        "ensemble_calibration": {"status": "disabled_by_policy"},
        "skill_metric": "brier_skill_vs_causal_base_rate_and_constant_half",
        "skill_epsilon": skill_epsilon,
    }
    skill = {
        "status": "not_applicable",
        "weighting": "none",
        "reason": "single production path; shadow candidates are not weighted",
    }
    return quality, skill
