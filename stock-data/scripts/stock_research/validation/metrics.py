"""Probability, ranking, calibration and bootstrap metrics."""

from __future__ import annotations

from typing import Callable, Iterable

import numpy as np
import pandas as pd

from ..models.calibrate import PlattCalibrator


def _arrays(y, p) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    return y[mask], np.clip(p[mask], 1e-6, 1.0 - 1e-6)


def brier(y, p) -> float:
    y, p = _arrays(y, p)
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def log_loss(y, p) -> float:
    y, p = _arrays(y, p)
    if not len(y):
        return float("nan")
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def accuracy(y, p, threshold: float = 0.5) -> float:
    y, p = _arrays(y, p)
    return float(np.mean((p >= threshold) == (y >= 0.5))) if len(y) else float("nan")


def balanced_accuracy(y, p, threshold: float = 0.5) -> float:
    y, p = _arrays(y, p)
    pred = p >= threshold
    recalls = []
    for label in (0.0, 1.0):
        mask = y == label
        if mask.any():
            recalls.append(float(np.mean(pred[mask] == (label == 1.0))))
    return float(np.mean(recalls)) if recalls else float("nan")


def auc(y, p) -> float | None:
    y, p = _arrays(y, p)
    if len(y) == 0 or np.all(y == y[0]):
        return None
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    sorted_p = p[order]
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    n_pos = float(np.sum(y == 1))
    n_neg = float(np.sum(y == 0))
    u = float(np.sum(ranks[y == 1]) - n_pos * (n_pos + 1) / 2.0)
    return u / (n_pos * n_neg) if n_pos and n_neg else None


def calibration_table(y, p, n_bins: int = 10) -> list[dict]:
    y, p = _arrays(y, p)
    if len(y) == 0:
        return []
    order = np.argsort(p)
    y, p = y[order], p[order]
    bins = np.array_split(np.arange(len(y)), min(n_bins, len(y)))
    rows = []
    for i, idx in enumerate(bins):
        if len(idx) == 0:
            continue
        rows.append(
            {
                "bin": i,
                "count": int(len(idx)),
                "p_mean": float(np.mean(p[idx])),
                "y_rate": float(np.mean(y[idx])),
                "p_min": float(np.min(p[idx])),
                "p_max": float(np.max(p[idx])),
            }
        )
    return rows


def ece(y, p, n_bins: int = 10) -> float:
    table = calibration_table(y, p, n_bins)
    total = sum(row["count"] for row in table)
    if total == 0:
        return float("nan")
    return float(sum(row["count"] / total * abs(row["p_mean"] - row["y_rate"]) for row in table))


def calibration_fit(y, p) -> dict:
    y, p = _arrays(y, p)
    if len(y) < 2 or np.all(y == y[0]):
        return {"intercept": None, "slope": None, "converged": False}
    calibrator = PlattCalibrator().fit(p, y)
    return {"intercept": calibrator.a, "slope": calibrator.b, "converged": calibrator.converged_}


def pinball(y, pred, tau: float) -> float:
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    residual = y - pred
    return float(np.mean(np.where(residual >= 0, tau * residual, (tau - 1.0) * residual)))


def interval_coverage(y, lower, upper) -> dict:
    y = np.asarray(y, dtype=float)
    lower = np.asarray(lower, dtype=float)
    upper = np.asarray(upper, dtype=float)
    mask = np.isfinite(y) & np.isfinite(lower) & np.isfinite(upper)
    if not mask.any():
        return {"coverage": None, "mean_width": None, "n": 0}
    inside = (y[mask] >= lower[mask]) & (y[mask] <= upper[mask])
    return {
        "coverage": float(np.mean(inside)),
        "mean_width": float(np.mean(upper[mask] - lower[mask])),
        "n": int(mask.sum()),
    }


def date_block_bootstrap(
    dates: Iterable,
    values: Iterable[float],
    n_resamples: int = 500,
    block_days: int = 5,
    seed: int = 20261106,
    confidence: float = 0.95,
) -> dict:
    frame = pd.DataFrame({"date": list(dates), "value": list(values)})
    frame = frame[np.isfinite(frame["value"].astype(float))]
    if frame.empty:
        return {"mean": None, "low": None, "high": None, "n_dates": 0}
    daily = frame.groupby("date")["value"].mean().sort_index()
    unique_dates = list(daily.index)
    if len(unique_dates) < 2:
        mean = float(daily.mean())
        return {"mean": mean, "low": None, "high": None, "n_dates": len(unique_dates)}
    block = max(1, int(block_days))
    blocks = [unique_dates[i:i + block] for i in range(0, len(unique_dates), block)]
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(int(n_resamples)):
        chosen = []
        while len(chosen) < len(unique_dates):
            chosen.extend(blocks[int(rng.integers(0, len(blocks)))])
        chosen = chosen[:len(unique_dates)]
        stats.append(float(daily.loc[chosen].mean()))
    alpha = (1.0 - confidence) / 2.0
    return {
        "mean": float(daily.mean()),
        "low": float(np.quantile(stats, alpha)),
        "high": float(np.quantile(stats, 1.0 - alpha)),
        "n_dates": int(len(unique_dates)),
        "n_resamples": int(n_resamples),
        "block_days": block,
    }
