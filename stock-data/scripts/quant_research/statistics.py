"""Effectiveness statistics shared by the researcher and the auditor.

Reproducibility and effectiveness are orthogonal properties: a probability
transform can be rebuilt bit-for-bit from frozen bars and still be worse than
reporting the base rate.  This module supplies the second half — Brier skill
against two naive baselines, AUC, a moving-block bootstrap for the paired loss
improvement, effective sample size for overlapping labels, and the power curve of
a binomial evidence floor — so every producer and auditor uses one definition.

It deliberately imports nothing from ``researchers`` or ``validation`` so either
side can use it without a cycle.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from typing import Any

BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 520052026
MIN_BOOTSTRAP_BLOCKS = 10
MIN_BOOTSTRAP_INDEPENDENT_BLOCKS = 3


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def paired_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-record baseline vs model squared error.

    ``improvement = (base_rate - target)**2 - (probability - target)**2``; a
    positive value means the model beat the causal base rate available at that
    origin.  Only matured records that carry a probability, a target and the
    base rate recorded at the same origin are returned.
    """

    rows: list[dict[str, Any]] = []
    for record in records:
        probability = _number(record.get("probability"))
        target = _number(record.get("target"))
        transform = record.get("probability_transform") or {}
        baseline = _number(transform.get("base_rate_at_origin", record.get("causal_base_rate")))
        if probability is None or target is None or baseline is None:
            continue
        rows.append({
            "decision_session": record.get("decision_session"),
            "baseline_probability": baseline,
            "model_probability": probability,
            "target": target,
            "baseline_squared_error": (baseline - target) ** 2,
            "model_squared_error": (probability - target) ** 2,
            "improvement": (baseline - target) ** 2 - (probability - target) ** 2,
        })
    return sorted(rows, key=lambda item: str(item["decision_session"]))


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def brier(values: Sequence[float]) -> float | None:
    return mean([float(value) ** 2 for value in values])


def auc(probabilities: Sequence[float], targets: Sequence[int]) -> float | None:
    """Rank-based AUC with tie handling; ``None`` when one class is absent."""

    pairs = [(float(p), int(t)) for p, t in zip(probabilities, targets, strict=True)]
    positives = sum(1 for _p, t in pairs if t == 1)
    negatives = len(pairs) - positives
    if not positives or not negatives:
        return None
    order = sorted(range(len(pairs)), key=lambda index: pairs[index][0])
    ranks = [0.0] * len(pairs)
    position = 0
    while position < len(order):
        stop = position
        while stop + 1 < len(order) and pairs[order[stop + 1]][0] == pairs[order[position]][0]:
            stop += 1
        average_rank = (position + stop) / 2 + 1
        for offset in range(position, stop + 1):
            ranks[order[offset]] = average_rank
        position = stop + 1
    rank_sum = sum(ranks[index] for index in range(len(pairs)) if pairs[index][1] == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def effective_sample_size(observations: int, horizon: int) -> int:
    """Non-overlapping equivalents inside a daily label ledger."""

    return max(0, int(observations) // max(1, int(horizon)))


def block_lengths_for(observations: int, horizon: int) -> list[int]:
    """Bootstrap block lengths that respect the label's dependence span.

    Adjacent 5-session labels share four of their five sessions, so a moving
    block shorter than ``horizon + 1`` resamples correlated rows as if they were
    independent and understates the interval.  The primary block is therefore
    ``horizon + 1``; longer blocks test sensitivity.  A length that would leave
    fewer than the minimum number of blocks is dropped.
    """

    candidates = (horizon + 1, horizon + 5, horizon + 20)
    usable = [length for length in candidates if observations - length + 1 >= MIN_BOOTSTRAP_BLOCKS]
    if usable:
        return usable
    return [max(1, min(horizon + 1, observations))]


def base_rate_standard_error(base_rate: float | None, effective: int) -> float | None:
    if base_rate is None or effective <= 0:
        return None
    return math.sqrt(max(0.0, base_rate * (1 - base_rate)) / effective)


def bootstrap_mean_interval(
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
    return {
        "status": "estimated",
        "block_length": length,
        "blocks": blocks,
        "observations": n,
        "mean": sum(values) / n,
        "lower": means[int(0.025 * (replicates - 1))],
        "upper": means[int(0.975 * (replicates - 1))],
        "replicates": replicates,
        "seed": seed,
        "method": "moving_block_bootstrap",
        "interpretation": "overlapping labels remain correlated; this is an in-sample interval, not a future coverage guarantee",
    }


def baseline_comparison(
    records: Iterable[dict[str, Any]],
    *,
    horizon: int,
    block_lengths: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compare the model with the causal base rate and with a constant 0.5.

    A probability audit that only proves arithmetic reproducibility has not said
    whether the number is useful.  This block is the mandatory answer: Brier
    skill against both naive baselines, AUC, the paired bootstrap interval, and
    the effective sample size behind all of them.
    """

    paired = paired_rows(records)
    observations = len(paired)
    if not observations:
        return {
            "status": "unavailable",
            "reason": "NO_MATURED_REPLAY_ROWS",
            "observations": 0,
            "effective_sample_size": 0,
            "baselines": {"causal_base_rate": None, "constant_half": None},
        }
    probabilities = [row["model_probability"] for row in paired]
    targets = [int(row["target"]) for row in paired]
    baselines = [row["baseline_probability"] for row in paired]
    brier_model = brier([probability - target for probability, target in zip(probabilities, targets, strict=True)])
    brier_base = brier([baseline - target for baseline, target in zip(baselines, targets, strict=True)])
    brier_half = brier([0.5 - target for target in targets])
    skills = {
        "vs_causal_base_rate": (1 - brier_model / brier_base) if brier_base else None,
        "vs_constant_half": (1 - brier_model / brier_half) if brier_half else None,
    }
    improvements = [row["improvement"] for row in paired]
    lengths = list(block_lengths) if block_lengths else block_lengths_for(observations, horizon)
    intervals = [bootstrap_mean_interval(improvements, length) for length in lengths]
    for position, interval in enumerate(intervals):
        interval["primary"] = position == 0
    primary = intervals[0]
    effective = effective_sample_size(observations, horizon)
    verdict = "insufficient_evidence"
    if primary.get("status") == "estimated":
        if float(primary["lower"]) > 0:
            verdict = "positive_skill_with_estimated_interval_excluding_zero"
        elif float(primary["upper"]) < 0:
            verdict = "negative_skill_with_estimated_interval_excluding_zero"
    return {
        "status": "estimated",
        "observations": observations,
        "effective_sample_size": effective,
        "effective_sample_definition": f"observations / horizon ({horizon})",
        "horizon_sessions": horizon,
        "brier_model": brier_model,
        "brier_causal_base_rate": brier_base,
        "brier_constant_half": brier_half,
        "skill": skills,
        "auc": auc(probabilities, targets),
        "mean_probability": mean(probabilities),
        "observed_up_rate": mean([float(target) for target in targets]),
        "base_rate_standard_error": base_rate_standard_error(mean(baselines), effective),
        "intervals": intervals,
        "verdict": verdict,
        "baselines": {
            "causal_base_rate": "rate of matured 5D labels available at each origin (same row)",
            "constant_half": "0.5 for every decision",
        },
        "evidence_source": "snapshot_bars_unclassified",
        "evidence_source_note": (
            "the frozen snapshot does not record whether its bars are synthetic; a report must declare the "
            "source, and synthetic bars may support process conclusions only, never an effectiveness conclusion"
        ),
        "note": "skill is a relative Brier improvement; it is not a trading edge and ignores costs",
    }


#: Compatibility alias: the historical name described the method, not the module.
block_bootstrap_mean_interval = bootstrap_mean_interval
#: Compatibility alias kept for callers that imported the old name.
paired_improvement_rows = paired_rows


def _binomial_tail(n: int, k: int, probability: float) -> float:
    """P(X >= k) for X ~ Binomial(n, probability)."""

    return sum(
        math.comb(n, index) * probability ** index * (1 - probability) ** (n - index)
        for index in range(max(0, k), n + 1)
    )


def _critical_count(n: int, probability: float = 0.5, alpha: float = 0.05) -> int:
    for count in range(n + 1):
        if _binomial_tail(n, count, probability) <= alpha:
            return count
    return n


def power_curve(
    sample_sizes: Sequence[int] = (30, 60, 90, 120, 250),
    true_rates: Sequence[float] = (0.55, 0.60, 0.63),
    *,
    null_rate: float = 0.5,
    alpha: float = 0.05,
) -> list[dict[str, Any]]:
    """Power of "k successes out of n" as an evidence floor.

    A 30-outcome floor passes only when at least 20 of 30 hit.  If the model's
    true hit rate were 60%, that test rejects a genuinely useful model about
    seven times in ten.  Publishing this table next to any such floor is what
    stops a system from being designed so that it can never be falsified.
    """

    rows = []
    for n in sample_sizes:
        critical = _critical_count(n, null_rate, alpha)
        rows.append({
            "sample_size": n,
            "critical_successes": critical,
            "alpha_one_sided": alpha,
            "power_by_true_rate": {
                f"{rate:.2f}": _binomial_tail(n, critical, rate) for rate in true_rates
            },
        })
    return rows
