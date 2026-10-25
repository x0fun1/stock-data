"""Audit of probability transforms and evidence support.

The audit never trusts the numbers a researcher reports about its own sample.
For the production shrinkage estimator it rebuilds the base rate and the
matching-origin selection from the frozen split-adjusted closes, then compares
the recomputed probability with the reported one at a fixed tolerance.  Any
future-dated label, rewritten sample, unknown transform type or inconsistent
count is a hard rejection of that record.

Compatibility names from the pre-policy module are kept as thin wrappers so old
reports remain readable; they are not used to grade the fixed-policy forecast.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from ..forecast_policy import (
    FORECAST_HORIZON_SESSIONS,
    MIN_PROSPECTIVE_NONOVERLAP,
    MIN_VALID_OOS_NONOVERLAP,
    NONOVERLAP_GAP_SESSIONS,
    POLICY_VERSION,
    SHRINKAGE_MIN_MATCHES,
    SHRINKAGE_PRIOR_STRENGTH,
    SHRINKAGE_VERSION,
)
from ..market_data import fixed_phase_sample
from ..researchers.quant import predict_at_origin

PROBABILITY_TOLERANCE = 1e-10
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 520052026
MIN_BOOTSTRAP_BLOCKS = 10
DEFAULT_BRIER_SKILL_EPSILON = 1e-9
MIN_BOOTSTRAP_INDEPENDENT_BLOCKS = 3


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
    reasons: list[str] = []
    if transform.get("type") != "causal_empirical_shrinkage":
        return {"ok": False, "reasons": [f"UNKNOWN_TRANSFORM_TYPE:{transform.get('type')!r}"], "recomputed": None}
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
    if index != row.get("decision_index", index):
        reasons.append("DECISION_INDEX_TAMPERED")
    if require_full_52w and row.get("full_52w_input") is not True:
        reasons.append("NOT_FULL_52W_INPUT")
    recomputed = predict_at_origin(dates, closes, index, window_start_exclusive=None)
    expected = recomputed["probability_transform"]
    reasons.extend([
        f"{field.upper()}_TAMPERED"
        for field in ("base_rate_at_origin", "wins", "observations")
        if not _close_diff(transform.get(field), expected.get(field)) and transform.get(field) != expected.get(field)
    ])
    if transform.get("matching_origins") != expected.get("matching_origins"):
        reasons.append("MATCHING_SAMPLE_REWRITTEN")
    if transform.get("base_rate_label_count") != expected.get("base_rate_label_count"):
        reasons.append("BASE_RATE_SAMPLE_REWRITTEN")
    if transform.get("base_rate_label_end_max") != expected.get("base_rate_label_end_max"):
        reasons.append("BASE_RATE_LABEL_BOUNDARY_TAMPERED")
    if transform.get("matching_label_end_max") != expected.get("matching_label_end_max"):
        reasons.append("MATCHING_LABEL_BOUNDARY_TAMPERED")
    for label_end in transform.get("matching_label_ends") or []:
        if label_end > decision:
            reasons.append("FUTURE_LABEL_IN_MATCHING_SAMPLE")
            break
    if recomputed["probability"] is None:
        if row.get("probability") is not None:
            reasons.append("PROBABILITY_PRESENT_WITHOUT_VALID_SAMPLE")
    else:
        if not _close_diff(row.get("probability"), recomputed["probability"]):
            reasons.append("PROBABILITY_TAMPERED")
        if not _close_diff(row.get("raw_probability"), recomputed["raw_probability"]):
            reasons.append("RAW_PROBABILITY_TAMPERED")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "recomputed": {
            "probability": recomputed["probability"],
            "raw_probability": recomputed["raw_probability"],
            "wins": expected.get("wins"),
            "observations": expected.get("observations"),
            "base_rate_at_origin": expected.get("base_rate_at_origin"),
            "matching_origins": expected.get("matching_origins"),
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
    if kind == "causal_empirical_shrinkage":
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
                "nonoverlap_valid_count": 0}
    audited: list[dict[str, Any]] = []
    invalid = 0
    abstained = 0
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
        })
        if not outcome["ok"]:
            invalid += 1
    valid = [row for row in audited if row["status"] == "valid"]
    dates = sorted({str(row["decision_session"]) for row in valid})
    if sessions:
        sampled_dates, sampling = fixed_phase_sample(dates, list(sessions))
    else:
        sampled_dates, sampling = [], {"method": "fixed_exchange_session_phase", "status": "calendar_unavailable"}
    sampled = [row for row in valid if str(row["decision_session"]) in set(sampled_dates)]
    path_sampling = ((result.get("validation") or {}).get("sampling") or {})
    reasons: list[str] = []
    if invalid:
        reasons.append("OOS_INTEGRITY_FAILED")
    if not valid:
        reasons.append("NO_VALID_OOS_PROBABILITY")
    elif len(sampled) < MIN_VALID_OOS_NONOVERLAP:
        reasons.append("OOS_SUPPORT_INSUFFICIENT")
    return {
        "status": "valid" if not reasons else "insufficient" if invalid == 0 else "invalid",
        "integrity_pass": invalid == 0,
        "reasons": reasons,
        "record_count": len(rows),
        "valid_count": len(valid),
        "invalid_count": invalid,
        "abstained_count": abstained,
        "nonoverlap_valid_count": len(sampled),
        "sampled_sessions": sampled_dates,
        "sampling": sampling,
        "coverage_ratio": path_sampling.get("coverage_ratio"),
        "rows": audited,
        "minimum_nonoverlap": MIN_VALID_OOS_NONOVERLAP,
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
    # Reuse the last row pipeline by auditing a synthetic record.
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
    return {"pass": outcome["ok"], "reasons": outcome["reasons"], "recomputed": outcome["recomputed"]}


def paired_improvement_rows(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-record baseline vs model squared error, using the causal base rate.

    ``improvement = (base_rate - target)**2 - (probability - target)**2``; a
    positive value means the model beat the base rate that was available at that
    origin.  Only matured records with a probability and a target are returned.
    """

    rows = []
    for record in records:
        probability, target = record.get("probability"), record.get("target")
        transform = record.get("probability_transform") or {}
        baseline = transform.get("base_rate_at_origin", record.get("causal_base_rate"))
        if (
            isinstance(probability, (int, float)) and not isinstance(probability, bool)
            and isinstance(baseline, (int, float)) and not isinstance(baseline, bool)
            and isinstance(target, (int, float)) and not isinstance(target, bool)
        ):
            rows.append({
                "decision_session": record.get("decision_session"),
                "baseline_probability": float(baseline),
                "model_probability": float(probability),
                "target": float(target),
                "baseline_squared_error": (float(baseline) - float(target)) ** 2,
                "model_squared_error": (float(probability) - float(target)) ** 2,
                "improvement": (float(baseline) - float(target)) ** 2 - (float(probability) - float(target)) ** 2,
            })
    return sorted(rows, key=lambda item: str(item["decision_session"]))


def block_bootstrap_mean_interval(
    values: Sequence[float], block_length: int, *, replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Moving-block bootstrap for the mean of a sampled, correlated series."""

    if not values:
        return {"status": "unavailable", "reason": "NO_VALUES", "block_length": block_length}
    n = len(values)
    length = max(1, min(block_length, n))
    blocks = n - length + 1
    if blocks < MIN_BOOTSTRAP_BLOCKS:
        return {"status": "unavailable", "reason": "INSUFFICIENT_BLOCKS", "block_length": length,
                "blocks": blocks, "observations": n}
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical bootstrap, not cryptography
    means = []
    for _ in range(replicates):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(blocks)
            sample.extend(values[start:start + length])
        means.append(sum(sample[:n]) / n)
    means.sort()
    if means[0] == means[-1]:
        return {"status": "degenerate_zero_width", "block_length": length, "blocks": blocks,
                "observations": n, "reason": "every resample reproduced the same mean"}
    lower = means[int(0.025 * (replicates - 1))]
    upper = means[int(0.975 * (replicates - 1))]
    return {"status": "estimated", "block_length": length, "blocks": blocks, "observations": n,
            "mean": sum(values) / n, "lower": lower, "upper": upper, "replicates": replicates,
            "seed": seed, "method": "moving_block_bootstrap",
            "record_spacing_sessions": NONOVERLAP_GAP_SESSIONS,
            "interpretation": "sampled records remain correlated; the interval is not a future coverage guarantee"}


def directional_edge_assessment(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Require 30+ prospective non-overlapping outcomes and three positive intervals."""

    matured = [row for row in records if row.get("status") == "matured" and row.get("probability") is not None]
    sampled = [row for row in matured if row.get("sampled") is True]
    paired = paired_improvement_rows(sampled)
    reasons: list[str] = []
    if len(sampled) < MIN_PROSPECTIVE_NONOVERLAP:
        reasons.append("PROSPECTIVE_SUPPORT_INSUFFICIENT")
    intervals = [
        block_bootstrap_mean_interval([row["improvement"] for row in paired], length)
        for length in (2, 1, 3)
    ]
    if len(sampled) >= MIN_PROSPECTIVE_NONOVERLAP:
        for interval in intervals:
            if interval.get("status") != "estimated":
                reasons.append("INTERVAL_UNESTIMABLE")
                break
            if not float(interval["lower"]) > 0:
                reasons.append("INTERVAL_CROSSES_ZERO")
                break
    return {
        "supported": not reasons,
        "reasons": reasons if reasons else ["DIRECTIONAL_EDGE_UNPROVEN"],
        "prospective_nonoverlap_count": len(sampled),
        "minimum_required": MIN_PROSPECTIVE_NONOVERLAP,
        "paired_records": len(paired),
        "intervals": intervals,
        "definition": "baseline_squared_error - model_squared_error vs the causal base rate at each origin",
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
    quality = {
        "status": "valid" if all(audit.get("integrity_pass") for audit in audits.values()) else "invalid",
        "paths": audits,
        "calibration_status": "disabled_by_policy",
        "ensemble_calibration": {"status": "disabled_by_policy"},
        "skill_metric": "causal_base_rate_squared_error",
        "skill_epsilon": skill_epsilon,
    }
    skill = {
        "status": "not_applicable",
        "weighting": "none",
        "reason": "single production path; shadow candidates are not weighted",
    }
    return quality, skill
