"""Small deterministic statistics helpers; no market or model opinions live here."""

from __future__ import annotations

import math
import random
from typing import Iterable


def mean(values: Iterable[float]) -> float | None:
    items = [float(x) for x in values if math.isfinite(float(x))]
    return sum(items) / len(items) if items else None


def sample_std(values: Iterable[float]) -> float | None:
    items = [float(x) for x in values if math.isfinite(float(x))]
    if len(items) < 2:
        return None
    center = sum(items) / len(items)
    return math.sqrt(sum((x - center) ** 2 for x in items) / (len(items) - 1))


def pearson(x: list[float], y: list[float]) -> float | None:
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if math.isfinite(float(a)) and math.isfinite(float(b))]
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs)
    sx, sy = sample_std(xs), sample_std(ys)
    if not sx or not sy:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((a - mx) * (b - my) for a, b in pairs) / ((len(pairs) - 1) * sx * sy)


def average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for j in range(index, end):
            ranks[ordered[j][0]] = rank
        index = end
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if math.isfinite(float(a)) and math.isfinite(float(b))]
    if len(pairs) < 3:
        return None
    rx = average_ranks([pair[0] for pair in pairs])
    ry = average_ranks([pair[1] for pair in pairs])
    return pearson(rx, ry)


def quantile(values: list[float], probability: float) -> float | None:
    items = sorted(float(x) for x in values if math.isfinite(float(x)))
    if not items:
        return None
    position = max(0.0, min(1.0, probability)) * (len(items) - 1)
    lower = int(position)
    upper = min(len(items) - 1, lower + 1)
    fraction = position - lower
    return items[lower] * (1 - fraction) + items[upper] * fraction


def wilson_interval(successes: int, count: int, z: float = 1.96) -> tuple[float, float] | None:
    if count <= 0:
        return None
    p = successes / count
    denom = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denom
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denom
    return max(0.0, center - radius), min(1.0, center + radius)


def logistic(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def benjamini_yekutieli(p_values: dict[str, float | None], *, trials: int | None = None) -> dict[str, float | None]:
    """Conservative FDR adjustment for arbitrary dependence among tested candidates."""
    valid = [(name, float(value)) for name, value in p_values.items()
             if isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(float(value)) and 0 <= float(value) <= 1]
    family_size = max(len(valid), int(trials or 0))
    adjusted: dict[str, float | None] = {name: None for name in p_values}
    if family_size == 0:
        return adjusted
    harmonic = sum(1.0 / rank for rank in range(1, family_size + 1))
    ordered = sorted(valid, key=lambda pair: (pair[1], pair[0]))
    running = 1.0
    for index in range(len(ordered) - 1, -1, -1):
        name, value = ordered[index]
        rank = index + 1
        running = min(running, value * family_size * harmonic / rank)
        adjusted[name] = min(1.0, running)
    return adjusted


def circular_block_bootstrap_spearman(
    x: list[float], y: list[float], *, block_length: int, replicates: int = 500, seed: int = 0,
) -> dict[str, object]:
    """Approximate paired moving-block bootstrap interval for a time-series Spearman IC."""
    pairs = [(float(a), float(b)) for a, b in zip(x, y)
             if math.isfinite(float(a)) and math.isfinite(float(b))]
    count = len(pairs)
    if count < max(20, 4 * block_length) or block_length < 1 or replicates < 1:
        return {"status": "insufficient_samples", "observations": count, "interval": None,
                "replicates": 0, "block_length": block_length,
                "method": "circular_moving_block_bootstrap_percentile",
                "coverage_validated": False}
    observed = spearman([a for a, _ in pairs], [b for _, b in pairs])
    if observed is None:
        return {"status": "not_assessed", "observations": count, "interval": None,
                "replicates": 0, "block_length": block_length,
                "method": "circular_moving_block_bootstrap_percentile",
                "coverage_validated": False}
    rng = random.Random(seed)
    estimates: list[float] = []
    attempts = 0
    while len(estimates) < replicates and attempts < replicates * 5:
        attempts += 1
        sample_indices: list[int] = []
        while len(sample_indices) < count:
            start = rng.randrange(count)
            sample_indices.extend((start + offset) % count for offset in range(block_length))
        sample_indices = sample_indices[:count]
        estimate = spearman([pairs[i][0] for i in sample_indices], [pairs[i][1] for i in sample_indices])
        if estimate is not None:
            estimates.append(estimate)
    if len(estimates) < max(50, replicates // 2):
        return {"status": "unstable_resamples", "observations": count, "observed_ic": observed,
                "interval": None, "replicates": len(estimates), "block_length": block_length,
                "method": "circular_moving_block_bootstrap_percentile",
                "coverage_validated": False}
    return {"status": "exploratory", "observations": count, "observed_ic": observed,
            "interval": [quantile(estimates, .025), quantile(estimates, .975)],
            "replicates": len(estimates), "block_length": block_length,
            "method": "circular_moving_block_bootstrap_percentile",
            "coverage_validated": False}


def circular_shift_spearman_p_value(x: list[float], y: list[float], *, block_length: int) -> dict[str, object]:
    """Circular-shift null diagnostic; interpretable as a p-value only under stationarity."""
    pairs = [(float(a), float(b)) for a, b in zip(x, y)
             if math.isfinite(float(a)) and math.isfinite(float(b))]
    count = len(pairs)
    observed = spearman([a for a, _ in pairs], [b for _, b in pairs])
    offsets = range(block_length, count - block_length + 1) if block_length >= 1 else ()
    if observed is None or len(offsets) < 19:
        return {"status": "insufficient_samples", "observations": count, "p_value": None,
                "permutations": 0, "block_length": block_length,
                "method": "circular_shift_test_stationarity_assumption"}
    baseline_x = [a for a, _ in pairs]
    baseline_y = [b for _, b in pairs]
    exceedances = 0
    permutations = 0
    for offset in offsets:
        shifted = baseline_y[offset:] + baseline_y[:offset]
        statistic = spearman(baseline_x, shifted)
        if statistic is None:
            continue
        permutations += 1
        if abs(statistic) >= abs(observed):
            exceedances += 1
    p_value = (1 + exceedances) / (1 + permutations) if permutations else None
    return {"status": "exploratory" if p_value is not None else "not_assessed",
            "observations": count, "p_value": p_value, "permutations": permutations,
            "block_length": block_length,
            "method": "circular_shift_test_stationarity_assumption",
            "stationarity_assumption_validated": False}
