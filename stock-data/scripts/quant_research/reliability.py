"""Reliability accounting for the Research B path.

Two jobs, both about refusing to overstate evidence.

The first is bookkeeping: a 52-week single-name study has a small and fixed number of
matured labels, and the gap between "raw observations", "matured observations" and
"independent observations" is large. Callers must be able to show that gap instead of
quoting one number that flatters the sample.

The second is grading: probability forecasts are scored on the same dates against an
explicit baseline, with an interval on the difference where the sample allows one, and
the path is given a route only when it clears a threshold that was declared in advance.
Thresholds live in module constants on purpose: a caller cannot lower them per symbol.
"""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Sequence
from typing import Any

from .market_data import anchored_non_overlapping, non_overlapping

ROUTE_LEGACY = "legacy_bucket"
ROUTE_ABSTAINED = "abstained"

# Declared before any evaluation and not adjustable per symbol or per request.
QUALIFICATION_THRESHOLDS: dict[str, dict[str, int]] = {
    "logistic": {"train_nonoverlap": 60, "calibration_nonoverlap": 30, "validation_nonoverlap": 30},
    "legacy_bucket": {"matured_nonoverlap": 12, "bucket_observations": 6, "validation_predictions": 8},
}

PROBABILITY_CLIP = 1e-6


def _stages() -> dict[str, int]:
    return dict(QUALIFICATION_THRESHOLDS["logistic"])


def logistic_shortfall(nonoverlap_matured: int) -> dict[str, Any]:
    """Report how far a study is from being able to host a fitted probability model.

    The three stages are training, calibration and validation. They may not share
    observations, so the requirement is their sum rather than their maximum.
    """
    stages = _stages()
    required = sum(stages.values())
    available = max(0, int(nonoverlap_matured))
    return {
        "status": "not_applicable_insufficient_samples" if available < required else "sample_threshold_met",
        "stage_requirements": stages,
        "required_non_overlapping_labels": required,
        "available_non_overlapping_labels": available,
        "shortfall": max(0, required - available),
        "stages_may_share_observations": False,
        # The gap is not a queue we are slowly filling: a 52-week window cannot host three
        # disjoint 60/30/30 stages at any horizon, so this route is out of scope by design
        # rather than pending. Research C already carries the fitted model.
        "implementation_policy": "deferred_by_fixed_52_week_design",
        "activation_target": "none_in_current_research_b_architecture",
        "alternative": "existing_research_c_logistic_path",
        "activation_note": (
            "A fitted L2 logistic path would need the training, calibration and validation labels "
            "separately and at once. A 52-week window cannot supply that at any horizon, so this is "
            "not a threshold awaiting more data but a design boundary; the fitted model lives in "
            "Research C. This field is retained so historical results stay traceable."
        ),
    }


def sample_diagnostics(
    *, bar_count: int, warmup: int, horizon: int, dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Separate raw, matured, independent and estimated-effective sample counts."""
    decision_dates = max(0, int(bar_count) - int(warmup))
    matured = max(0, decision_dates - int(horizon))
    indices = list(range(int(warmup), max(int(warmup), int(bar_count) - int(horizon))))
    independent_source = "legacy_spacing"
    if dates is not None and len(dates) >= int(bar_count):
        # Count with the same anchored phase the production ledger uses, so the reported
        # independent size matches the labels actually consumed instead of a different rule.
        _, provenance = anchored_non_overlapping(list(dates), indices, int(horizon))
        independent = int(provenance.get("kept", 0))
        independent_source = "anchored_phase"
    else:
        independent = len(non_overlapping(indices, int(horizon)))
    approximation = matured / int(horizon) if horizon else float(matured)
    return {
        "bar_count": int(bar_count),
        "feature_warmup_sessions": int(warmup),
        "horizon_sessions": int(horizon),
        "decision_dates": decision_dates,
        "raw_sample_count": matured,
        "matured_sample_count": matured,
        "nonoverlap_sample_count": independent,
        "nonoverlap_count_basis": independent_source,
        "effective_sample_size_estimate": approximation,
        "effective_sample_size_kind": "approximation_only_raw_over_horizon",
        "sample_coverage": (matured / decision_dates) if decision_dates else 0.0,
        "overlapping_observations_are_independent": False,
        "note": (
            "The non-overlapping count is the conservative figure and is never larger than "
            "the effective estimate; raw divided by horizon approximates dependence but is "
            "not a measured effective sample size."
        ),
    }


def _clip(probability: float) -> float:
    return min(1.0 - PROBABILITY_CLIP, max(PROBABILITY_CLIP, float(probability)))


def _logit(probability: float) -> float:
    value = _clip(probability)
    return math.log(value / (1.0 - value))


def _brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs)


def _log_loss(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for probability, outcome in pairs:
        value = _clip(probability)
        total -= math.log(value if outcome else 1.0 - value)
    return total / len(pairs)


def _calibration_line(pairs: Sequence[tuple[float, int]]) -> tuple[float | None, float | None]:
    """Least-squares slope and intercept of outcome on logit(probability)."""
    if len(pairs) < 4:
        return None, None
    xs = [_logit(probability) for probability, _ in pairs]
    ys = [float(outcome) for _, outcome in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((value - mean_x) ** 2 for value in xs)
    if variance <= 1e-12:
        return None, None
    covariance = sum((value - mean_x) * (target - mean_y) for value, target in zip(xs, ys, strict=True))
    slope = covariance / variance
    return slope, mean_y - slope * mean_x


def _paired_interval(
    differences: Sequence[float], *, block_length: int, replicates: int = 500, seed: int = 0,
) -> dict[str, Any]:
    count = len(differences)
    if count < max(20, 4 * block_length) or block_length < 1:
        return {"status": "insufficient_samples", "observations": count, "interval": None,
                "method": "circular_moving_block_bootstrap_percentile_mean"}
    rng = random.Random(seed)  # noqa: S311 - seeded statistical bootstrap, not cryptographic
    means: list[float] = []
    while len(means) < replicates:
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(differences[(start + offset) % count] for offset in range(block_length))
        sample = sample[:count]
        means.append(sum(sample) / len(sample))
    means.sort()
    low = means[int(0.025 * (len(means) - 1))]
    high = means[int(0.975 * (len(means) - 1))]
    return {"status": "exploratory", "observations": count, "interval": [low, high],
            "block_length": block_length, "replicates": len(means),
            "method": "circular_moving_block_bootstrap_percentile_mean", "coverage_validated": False}


def probabilistic_scores(
    predictions: Sequence[tuple[int, float, float | None, int]],
    *,
    horizon: int,
) -> dict[str, Any]:
    """Score the path against the historical base rate on identical dates.

    ``predictions`` holds ``(session_index, probability, base_rate, outcome)`` for every
    out-of-sample forecast whose label already matured, where ``session_index`` is the
    position of the decision session inside the frozen bar array - not a running number
    over the filtered predictions. Pairing is by construction: the baseline is the
    same-date base rate, so a difference cannot come from a different sample.

    The dependence unit follows the actual session spacing. When the caller already
    sampled non-overlapping decisions, adjacent forecasts share no label window, so the
    moving-block length collapses to one record; applying a ``horizon + 1`` block to an
    already-thinned list would treat ``(horizon + 1)**2`` sessions as one dependent block
    and produce an interval that means nothing.
    """
    model_pairs = [(float(probability), int(outcome)) for _, probability, _, outcome in predictions]
    base_pairs = [(float(base), int(outcome)) for _, _, base, outcome in predictions if base is not None]
    common = [(probability, base, outcome) for _, probability, base, outcome in predictions if base is not None]
    model_common = [(probability, outcome) for probability, _, outcome in common]
    base_common = [(base, outcome) for _, base, outcome in common]
    brier = _brier(model_common)
    base_brier = _brier(base_common)
    skill = (1.0 - brier / base_brier) if (brier is not None and base_brier not in (None, 0.0)) else None
    slope, intercept = _calibration_line(model_common)
    losses = [(probability - outcome) ** 2 for probability, outcome in model_common]
    base_losses = [(base - outcome) ** 2 for base, outcome in base_common]
    differences = [model - base for model, base in zip(losses, base_losses, strict=True)]
    sessions = sorted({int(index) for index, _, base, _ in predictions if base is not None})
    gaps = [right - left for left, right in itertools.pairwise(sessions)]
    non_overlapping_sample = bool(gaps) and min(gaps) > int(horizon)
    block_length = 1 if non_overlapping_sample else max(1, int(horizon) + 1)
    paired = _paired_interval(differences, block_length=block_length)
    mean_difference = (sum(differences) / len(differences)) if differences else None
    return {
        "brier": brier,
        "base_rate_brier": base_brier,
        "brier_skill_vs_base_rate": skill,
        "log_loss": _log_loss(model_common),
        "base_rate_log_loss": _log_loss(base_common),
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "calibration_slope_method": "ols_of_outcome_on_logit_probability_diagnostic_only",
        "calibration_slope_is_logistic_calibration_slope": False,
        "paired_common_samples": len(common),
        "brier_difference_model_minus_base": mean_difference,
        "brier_difference_interval": paired.get("interval"),
        "brier_difference_interval_status": paired.get("status"),
        "block_length": block_length,
        "block_length_basis": (
            "one_record_because_labels_do_not_overlap" if non_overlapping_sample
            else "horizon_plus_one_because_adjacent_labels_overlap"
        ),
        "minimum_session_gap": min(gaps) if gaps else None,
        "baseline": "same_date_historical_base_rate",
        "comparison_sample_matched": True,
        "scored_predictions": len(model_pairs),
        "base_rate_scored_predictions": len(base_pairs),
    }


def qualification(
    *,
    nonoverlap_matured: int,
    bucket_observations: int,
    buckets_available: int,
    validation_predictions: int,
) -> dict[str, Any]:
    """Decide the route from pre-declared thresholds only."""
    legacy_floor = QUALIFICATION_THRESHOLDS["legacy_bucket"]
    legacy_checks = {
        "matured_nonoverlap": (nonoverlap_matured, legacy_floor["matured_nonoverlap"]),
        "bucket_observations": (bucket_observations, legacy_floor["bucket_observations"]),
        "validation_predictions": (validation_predictions, legacy_floor["validation_predictions"]),
        "buckets_available": (buckets_available, 2),
    }
    failed = [name for name, (observed, floor) in legacy_checks.items() if int(observed) < int(floor)]
    route = ROUTE_ABSTAINED if failed else ROUTE_LEGACY
    return {
        "route": route,
        "route_reason": ("insufficient_matured_evidence:" + ",".join(failed)) if failed else "legacy_bucket_qualified",
        "checks": {name: {"observed": int(observed), "required": int(floor), "passed": int(observed) >= int(floor)}
                   for name, (observed, floor) in legacy_checks.items()},
        "withheld_models": {"logistic": logistic_shortfall(nonoverlap_matured)},
        "thresholds_declared_in_advance": True,
        "thresholds_adjustable_per_symbol": False,
    }
