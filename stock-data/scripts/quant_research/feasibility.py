"""Run-context disclosures: sample capacity and decision timing.

These blocks are metadata only; they never move a probability. Capacity is an
upper bound on the labels a path can use, so a path marked ``feasible`` can still
abstain after its own filters (same-sign matching, bucket balance, class balance).
Reporting the ceiling up front stops a run from being read as "no signal" when the
window simply cannot host the test.
"""

from __future__ import annotations

from typing import Any

#: Retained for external callers and the self-check only; no runtime entry point
#: calls it after v2.  ``build_timing`` in this module is live.
LEGACY_UNUSED = ("build_forecast_feasibility",)

QUANT_MIN_MATCHES = 12
FACTOR_MIN_MATURED = 18
FACTOR_MIN_SELECTION_LABELS = 10
ML_MIN_TRAIN = 60
ML_CALIBRATION_MIN = 40

QUANT_WARMUP_SESSIONS = 20
FACTOR_WARMUP_SESSIONS = 60
FACTOR_SELECTION_FRACTION = 0.5


def _horizon_sessions(manifest: dict[str, Any]) -> int:
    digits = "".join(ch for ch in str(manifest.get("horizon", "5D")) if ch.isdigit())
    horizon = int(digits) if digits else 5
    return max(1, horizon)


def _spaced_capacity(bar_count: int, horizon: int, warmup: int) -> int:
    """Non-overlapping labels matured by the final bar, spaced ``horizon + 1`` apart."""
    last = bar_count - 1 - horizon
    if last < warmup:
        return 0
    return (last - warmup) // (horizon + 1) + 1


def _selection_capacity(bar_count: int, horizon: int) -> int:
    """Non-overlapping labels inside the fixed chronological selection segment."""
    last = int(bar_count * FACTOR_SELECTION_FRACTION) - horizon - 1
    if last < FACTOR_WARMUP_SESSIONS:
        return 0
    return (last - FACTOR_WARMUP_SESSIONS) // (horizon + 1) + 1


def _daily_matured(bar_count: int, horizon: int, warmup: int) -> int:
    """Daily (possibly overlapping) observations whose label is already known."""
    last = bar_count - 1 - horizon
    return max(0, last - warmup + 1)


def _status(capacity: int, required: int) -> str:
    return "feasible" if capacity >= required else "capacity_insufficient"


def _observed(results: dict[str, Any] | None, researcher_id: str) -> int | None:
    if not results:
        return None
    validation = (results.get(researcher_id) or {}).get("validation") or {}
    if researcher_id == "quant":
        value = validation.get("non_overlapping_training_labels")
    elif researcher_id == "factor":
        counts = validation.get("calibration_bucket_counts") or {}
        value = sum(int(item) for item in counts.values() if isinstance(item, int)) if counts else None
    elif researcher_id == "ml":
        value = (validation.get("production_replay") or {}).get(
            "observations", validation.get("non_overlapping_prediction_count"),
        )
    else:
        value = None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_forecast_feasibility(
    manifest: dict[str, Any], bars: list[dict[str, Any]],
    results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-path label ceiling versus requirement for the frozen window."""
    horizon = _horizon_sessions(manifest)
    sessions = len(bars)
    quant_capacity = _spaced_capacity(sessions, horizon, QUANT_WARMUP_SESSIONS)
    factor_capacity = _spaced_capacity(sessions, horizon, FACTOR_WARMUP_SESSIONS)
    selection_capacity = _selection_capacity(sessions, horizon)
    ml_training_capacity = _daily_matured(sessions, horizon, FACTOR_WARMUP_SESSIONS)
    ml_replay_capacity = max(
        0,
        (sessions - FACTOR_WARMUP_SESSIONS - horizon - ML_MIN_TRAIN) // (horizon + 1),
    )

    quant = {
        "required_matured_non_overlapping": QUANT_MIN_MATCHES,
        "capacity_matured_non_overlapping": quant_capacity,
        "status": _status(quant_capacity, QUANT_MIN_MATCHES),
        "basis": "upper bound: the same-sign filter and matured-label ordering can only reduce it",
    }
    factor = {
        "required_matured_non_overlapping": FACTOR_MIN_MATURED,
        "required_selection_non_overlapping": FACTOR_MIN_SELECTION_LABELS,
        "capacity_matured_non_overlapping": factor_capacity,
        "capacity_selection_non_overlapping": selection_capacity,
        "status": (
            "feasible"
            if factor_capacity >= FACTOR_MIN_MATURED and selection_capacity >= FACTOR_MIN_SELECTION_LABELS
            else "capacity_insufficient"
        ),
        "basis": "three buckets of six matured labels plus ten selection labels",
    }
    ml = {
        "required_training_rows": ML_MIN_TRAIN,
        "capacity_training_rows": ml_training_capacity,
        "required_non_overlapping_replay": 1,
        "capacity_non_overlapping_replay": ml_replay_capacity,
        "required_calibration_observations": ML_CALIBRATION_MIN,
        "capacity_calibration_observations": ml_replay_capacity,
        "calibration_status": _status(ml_replay_capacity, ML_CALIBRATION_MIN),
        "status": _status(ml_training_capacity, ML_MIN_TRAIN),
        "basis": (
            "60 matured training rows are required; the replay scores non-overlapping H+1 decisions. "
            "The 52-week window usually supplies fewer than the 40 matured replay rows Platt needs, "
            "so the published probability is raw and calibration is disclosed as capacity-limited."
        ),
    }
    paths = {"quant": quant, "factor": factor, "ml": ml}
    for name in paths:
        observed = _observed(results, name)
        if observed is not None:
            paths[name]["observed"] = observed
    return {
        "sessions": sessions,
        "horizon_sessions": horizon,
        "paths": paths,
        "note": "capacity_insufficient means the 52-week window cannot host the path's declared minimum; the path abstains instead of lowering its threshold.",
    }


def build_timing(manifest: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Decision timestamp and label basis for the reported probability."""
    validation = manifest.get("data_validation") or {}
    latest = validation.get("latest_confirmed_close") or {}
    last_session = bars[-1].get("date") if bars else None
    return {
        "asof_timestamp": manifest.get("asof_timestamp"),
        "last_bar_session": last_session,
        "decision_session": latest.get("session_date") or last_session,
        "decision_at": latest.get("close_at"),
        "label_basis": "close_to_close",
        "entry_basis": "not_modelled",
        "first_executable_at": None,
        "first_executable_rule": "next session open after the confirmed close; not resolved in this run",
        "note": "The probability is a close-to-close research estimate, not an executable trade signal.",
    }
