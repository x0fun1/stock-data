"""Small deterministic statistics helpers; no market or model opinions live here."""

from __future__ import annotations

import math
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
