"""Research D: single-security time-series factor IC and forward-return checks.

This is a diagnostic, not the cross-sectional portfolio engine described by the
Factor Backtest upstream. It deliberately emits no directional probability.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import forward_returns, market_bars, non_overlapping
from ..math_utils import mean, pearson, quantile, sample_std, spearman
from ..snapshot import load_snapshot

MIN_HOLDOUT_OBSERVATIONS = 20


def _factor_values(bars: list[dict[str, Any]]) -> dict[str, tuple[str, list[float | None]]]:
    """Build a fixed OHLCV factor set locally from the shared raw bars."""
    n = len(bars)
    close = [row["close"] for row in bars]
    open_ = [row["open"] for row in bars]
    high = [row["high"] for row in bars]
    low = [row["low"] for row in bars]
    volume = [row["volume"] for row in bars]
    result: dict[str, tuple[str, list[float | None]]] = {}

    for window in (5, 20, 60):
        values: list[float | None] = [None] * n
        for i in range(window, n):
            if close[i - window] > 0:
                values[i] = close[i] / close[i - window] - 1.0
        result[f"momentum_{window}"] = ("momentum", values)

    reversal: list[float | None] = [None] * n
    gap: list[float | None] = [None] * n
    close_location: list[float | None] = [None] * n
    trend: list[float | None] = [None] * n
    range_position: list[float | None] = [None] * n
    volume_z: list[float | None] = [None] * n
    for i in range(n):
        if i >= 1 and close[i - 1] > 0:
            reversal[i] = close[i] / close[i - 1] - 1.0
            gap[i] = open_[i] / close[i - 1] - 1.0
        day_range = high[i] - low[i]
        if day_range > 0:
            close_location[i] = (2.0 * close[i] - high[i] - low[i]) / day_range
        if i >= 19:
            close_window = close[i - 19:i + 1]
            center = sum(close_window) / len(close_window)
            trend[i] = close[i] / center - 1.0 if center > 0 else None
            high20, low20 = max(high[i - 19:i + 1]), min(low[i - 19:i + 1])
            range_position[i] = (close[i] - low20) / (high20 - low20) if high20 > low20 else 0.5
            volume_window = volume[i - 19:i + 1]
            scale = sample_std(volume_window)
            volume_z[i] = (volume[i] - sum(volume_window) / len(volume_window)) / scale if scale else 0.0

    result.update({
        "reversal_1": ("reversal", reversal),
        "trend_sma20": ("trend", trend),
        "range_position_20": ("trend", range_position),
        "volume_z20": ("volume", volume_z),
        "overnight_gap": ("volatility", gap),
        "close_location": ("volume", close_location),
    })
    return result


def _aligned(values: list[float | None], labels: list[float | None], indices: list[int]) -> tuple[list[float], list[float]]:
    pairs = [
        (float(values[i]), float(labels[i]))
        for i in indices
        if i < len(values)
        and values[i] is not None
        and labels[i] is not None
        and math.isfinite(float(values[i]))
        and math.isfinite(float(labels[i]))
    ]
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _metrics(
    values: list[float | None],
    labels: list[float | None],
    indices: list[int],
    low_cut: float | None,
    high_cut: float | None,
) -> dict[str, Any]:
    x, y = _aligned(values, labels, indices)
    low_returns = [ret for value, ret in zip(x, y) if low_cut is not None and value <= low_cut]
    high_returns = [ret for value, ret in zip(x, y) if high_cut is not None and value >= high_cut]
    spread = mean(high_returns) - mean(low_returns) if high_returns and low_returns else None
    enough = len(x) >= MIN_HOLDOUT_OBSERVATIONS
    return {
        "status": "available" if enough else "insufficient_data",
        "observations": len(x),
        "minimum_observations": MIN_HOLDOUT_OBSERVATIONS,
        "spearman_time_series_ic": spearman(x, y) if len(x) >= 3 else None,
        "pearson_correlation": pearson(x, y) if len(x) >= 3 else None,
        "mean_forward_return": mean(y),
        "top_minus_bottom_training_quantile_return": spread,
        "top_quantile_observations": len(high_returns),
        "bottom_quantile_observations": len(low_returns),
    }


def run(snapshot_dir: Path) -> dict[str, Any]:
    manifest, market = load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    bars = market_bars(market)
    closes = [bar["close"] for bar in bars]
    factors = _factor_values(bars)
    n = len(bars)
    split = int(n * 0.7)
    requested_horizon = HORIZONS[horizon_name]
    requested_key = f"{requested_horizon}D"
    metrics: dict[str, Any] = {}
    requested_available = 0
    requested_observations = 0

    for name, (family, values) in factors.items():
        horizons: dict[str, Any] = {}
        for horizon in (1, 5, 20):
            labels = forward_returns(closes, horizon)
            warmup = 60
            train_indices = non_overlapping(
                [i for i in range(warmup, split) if i + horizon < split], horizon
            )
            holdout_indices = non_overlapping(
                [i for i in range(split, n) if i + horizon < n], horizon
            )
            train_x, _ = _aligned(values, labels, train_indices)
            low_cut, high_cut = quantile(train_x, 0.2), quantile(train_x, 0.8)
            training = _metrics(values, labels, train_indices, low_cut, high_cut)
            holdout = _metrics(values, labels, holdout_indices, low_cut, high_cut)
            if horizon == requested_horizon:
                requested_available += int(holdout["status"] == "available")
                requested_observations += int(holdout["observations"])
            horizons[f"{horizon}D"] = {"training": training, "holdout": holdout}
        metrics[name] = {"family": family, "horizons": horizons}

    status = "success" if requested_available else "partial" if requested_observations else "insufficient_data"
    warnings = [
        "Single-security results are time-series IC/forward-return diagnostics, not the cross-sectional Rank IC or portfolio backtest from the upstream skill.",
        "Forward returns are gross close-to-close diagnostics; this path does not simulate trades, benchmark-relative NAV, turnover costs, or market impact.",
    ]
    if not requested_available:
        warnings.append(
            f"No {requested_key} holdout factor reached the minimum observation count for an IC diagnostic; "
            "other horizons are still reported descriptively."
        )

    validation = {
        "method": "fixed-factor time-series IC and forward-return diagnostics",
        "universe_type": "single_security",
        "signal_timing": "factor uses OHLCV through close[t]; label is close[t+h] / close[t] - 1",
        "split": {"training_fraction": 0.7, "holdout_fraction": 0.3, "holdout_start_session": bars[split]["date"] if split < n else None},
        "non_overlapping_labels": True,
        "minimum_holdout_observations": MIN_HOLDOUT_OBSERVATIONS,
        "requested_horizon": requested_key,
        "candidate_factor_count": len(factors),
        "horizons": ["1D", "5D", "20D"],
        "factor_metrics": metrics,
        "leakage_audit": {
            "features_use_data_through_decision_close_only": True,
            "future_returns_used_only_as_labels": True,
            "training_quantile_cutoffs_fixed_before_holdout": True,
            "holdout_labels_non_overlapping": True,
        },
        "full_cross_sectional_backtest": {
            "status": "not_assessed",
            "reason": "requires a multi-ticker factor panel, point-in-time universe, benchmark, adjusted/tradable price matrices and security masks",
        },
    }
    return researcher_result(
        "factor_backtest", ticker, horizon_name, manifest["snapshot_id"],
        status=status, result_role="diagnostic", validation=validation,
        data_used=["daily raw OHLCV"], warnings=warnings,
    )
