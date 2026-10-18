"""Dependency-aware factor-selection statistics for the Research B path.

A 52-week single-name study leaves about ten non-overlapping matured labels in the
selection segment at a five-session horizon, so picking five factors out of eleven
candidates would rest on ten observations. Discarding the other matured labels is not
required by causality: every label used here already matured inside the selection
segment. What overlap does require is that the dependence be measured rather than
ignored, so this module keeps the causal boundary, estimates the Rank IC on every
matured label of the segment, and reports an interval from a moving-block bootstrap
whose block length equals the label horizon.

Nothing here selects factors on its own; callers decide, and this module only returns
evidence plus an explicit verdict so the decision can be audited after the fact.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .math_utils import (
    benjamini_yekutieli,
    circular_block_bootstrap_spearman,
    circular_shift_spearman_p_value,
    spearman,
)

MINIMUM_OBSERVATIONS = 24
SUBWINDOW_COUNT = 3
MINIMUM_SUBWINDOW_OBSERVATIONS = 5

DIRECTION_FLAT_BAND = 1e-9


def _pairs(values: Sequence[float | None], labels: Sequence[float | None], indices: Sequence[int]) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for index in indices:
        if index >= len(values) or index >= len(labels):
            continue
        left, right = values[index], labels[index]
        if left is None or right is None:
            continue
        if not math.isfinite(float(left)) or not math.isfinite(float(right)):
            continue
        pairs.append((float(left), float(right)))
    return pairs


def _subwindow_ics(pairs: list[tuple[float, float]], count: int) -> list[float | None]:
    if count <= 1 or len(pairs) < count * MINIMUM_SUBWINDOW_OBSERVATIONS:
        return []
    size = len(pairs) // count
    out: list[float | None] = []
    for window in range(count):
        start = window * size
        end = len(pairs) if window == count - 1 else start + size
        chunk = pairs[start:end]
        out.append(spearman([pair[0] for pair in chunk], [pair[1] for pair in chunk]))
    return out


def _direction(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value > DIRECTION_FLAT_BAND:
        return "positive"
    if value < -DIRECTION_FLAT_BAND:
        return "negative"
    return "flat"


def overlapping_ic(
    values: Sequence[float | None],
    labels: Sequence[float | None],
    indices: Sequence[int],
    *,
    horizon: int,
    replicates: int = 500,
    seed: int = 0,
) -> dict[str, Any]:
    """Rank IC over every matured label in ``indices``, with a block-bootstrap interval.

    ``indices`` must already be restricted to labels that matured inside the caller's
    training boundary; this function does not add a second maturity filter and cannot
    detect a caller that passes immature labels.
    """
    pairs = _pairs(values, labels, indices)
    block_length = max(1, int(horizon) + 1)
    base: dict[str, Any] = {
        "method": "overlapping_block_rank_ic",
        "observations": len(pairs),
        "non_overlapping_equivalent": len(pairs) // block_length,
        "block_length": block_length,
        "requested_indices": len(indices),
    }
    if len(pairs) < MINIMUM_OBSERVATIONS:
        return {**base, "status": "insufficient_samples", "rank_ic": None, "interval": None,
                "sign_consistency": None, "direction": "unknown",
                "reason": "fewer_matured_labels_than_minimum_observations"}
    observed = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
    if observed is None:
        return {**base, "status": "not_assessed", "rank_ic": None, "interval": None,
                "sign_consistency": None, "direction": "unknown", "reason": "rank_ic_undefined"}
    bootstrap = circular_block_bootstrap_spearman(
        [pair[0] for pair in pairs], [pair[1] for pair in pairs],
        block_length=block_length, replicates=replicates, seed=seed,
    )
    windows = _subwindow_ics(pairs, SUBWINDOW_COUNT)
    valid_windows = [value for value in windows if value is not None]
    reference = _direction(observed)
    consistency = None
    if valid_windows and reference != "flat":
        agreeing = sum(1 for value in valid_windows if _direction(value) == reference)
        consistency = agreeing / len(valid_windows)
    interval = bootstrap.get("interval")
    return {
        **base,
        "status": "assessed",
        "rank_ic": observed,
        "direction": reference,
        "interval": interval,
        "interval_status": bootstrap.get("status"),
        "interval_replicates": bootstrap.get("replicates"),
        "interval_excludes_zero": (interval[0] > 0 or interval[1] < 0) if isinstance(interval, list) else None,
        "subwindow_ics": windows,
        "valid_subwindow_count": len(valid_windows),
        "sign_consistency": consistency,
        "overlap_treated_as_dependence": True,
        "coverage_validated": False,
    }


def stability_verdict(
    metrics: dict[str, Any],
    *,
    min_abs_rank_ic: float,
    min_sign_consistency: float,
) -> dict[str, Any]:
    """Apply the pre-declared selection rule to one factor's evidence.

    The rule is deliberately declared before the data is read and is not tuned per
    symbol: a factor must clear the point-estimate floor and must not have the opposite
    sign in most of its own time sub-windows. The bootstrap interval is reported but is
    not used as a pass/fail gate, because at 52 weeks it is too wide to admit any factor.
    """
    status = metrics.get("status")
    if status != "assessed":
        return {"passed": False, "reason": "insufficient_selection_evidence", "evidence_status": status}
    observed = metrics.get("rank_ic")
    if observed is None:
        return {"passed": False, "reason": "rank_ic_undefined", "evidence_status": status}
    if abs(float(observed)) < min_abs_rank_ic:
        return {"passed": False, "reason": "below_minimum_rank_ic",
                "rank_ic": observed, "threshold": min_abs_rank_ic}
    consistency = metrics.get("sign_consistency")
    if consistency is None:
        return {"passed": False, "reason": "direction_unverifiable", "rank_ic": observed,
                "threshold": min_sign_consistency}
    if float(consistency) < min_sign_consistency:
        return {"passed": False, "reason": "direction_unstable", "rank_ic": observed,
                "sign_consistency": consistency, "threshold": min_sign_consistency}
    return {"passed": True, "reason": "cleared_rank_ic_and_stability", "rank_ic": observed,
            "sign_consistency": consistency, "threshold": min_sign_consistency}


def multiple_testing_disclosure(
    p_values: dict[str, float | None],
) -> dict[str, Any]:
    """Disclose dependence-aware FDR without turning it into a selection gate.

    ``benjamini_yekutieli`` is valid under arbitrary dependence, which matches the
    overlapping labels used here. It is reported for audit only: the plan requires that
    no significance verdict substitute for independent out-of-sample evidence.
    """
    adjusted = benjamini_yekutieli(p_values, trials=len(p_values))
    tested = [name for name, value in p_values.items() if value is not None]
    return {
        "method": "benjamini_yekutieli_arbitrary_dependence",
        "tested_candidates": len(tested),
        "adjusted": adjusted,
        "used_as_selection_gate": False,
        "note": "Adjusted p-values are disclosure only; selection still requires out-of-sample evidence.",
    }


def shift_p_values(
    values: Sequence[float | None],
    labels: Sequence[float | None],
    indices: Sequence[int],
    *,
    horizon: int,
) -> dict[str, float | None]:
    """Per-factor circular-shift p-values used for the FDR disclosure."""
    pairs = _pairs(values, labels, indices)
    if len(pairs) < MINIMUM_OBSERVATIONS:
        return {}
    result = circular_shift_spearman_p_value(
        [pair[0] for pair in pairs], [pair[1] for pair in pairs], block_length=max(1, int(horizon) + 1),
    )
    count = result.get("permutations")
    if result.get("status") != "exploratory" or not isinstance(count, int) or count < 19:
        return {}
    return {"p_value": result.get("p_value")}
