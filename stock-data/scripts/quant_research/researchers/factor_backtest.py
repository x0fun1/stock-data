"""Research D: 5-session descriptive factor diagnostics (never a probability)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..contracts import researcher_result
from ..forecast_policy import FORECAST_HORIZON_SESSIONS
from ..market_data import forward_returns, market_bars, non_overlapping
from ..math_utils import mean, pearson, quantile, sample_std, spearman
from ..snapshot import load_snapshot

MIN_HOLDOUT_OBSERVATIONS = 20
MODEL_VERSION = "factor_backtest_5d_descriptive_v1"


def _factor_values(bars: list[dict[str, Any]]) -> dict[str, tuple[str, list[float | None]]]:
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

    one_day: list[float | None] = [None] * n
    gap: list[float | None] = [None] * n
    close_location: list[float | None] = [None] * n
    trend: list[float | None] = [None] * n
    range_position: list[float | None] = [None] * n
    volume_z: list[float | None] = [None] * n
    for i in range(n):
        if i >= 1 and close[i - 1] > 0:
            one_day[i] = close[i] / close[i - 1] - 1.0
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
        # The factor was already a one-day return; only the name and family change
        # in this round, so the sign is not flipped while its meaning is renamed.
        "momentum_1": ("momentum", one_day),
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
    values: list[float | None], labels: list[float | None], indices: list[int],
    low_cut: float | None, high_cut: float | None,
) -> dict[str, Any]:
    x, y = _aligned(values, labels, indices)
    low_returns = [ret for value, ret in zip(x, y, strict=True) if low_cut is not None and value <= low_cut]
    high_returns = [ret for value, ret in zip(x, y, strict=True) if high_cut is not None and value >= high_cut]
    spread = mean(high_returns) - mean(low_returns) if high_returns and low_returns else None
    enough = len(x) >= MIN_HOLDOUT_OBSERVATIONS
    return {
        "status": "available" if enough else ("descriptive_only" if len(x) >= 3 else "insufficient_data"),
        "descriptive_only": not enough,
        "observations": len(x),
        "minimum_observations": MIN_HOLDOUT_OBSERVATIONS,
        "spearman_time_series_ic": spearman(x, y) if len(x) >= 3 else None,
        "pearson_correlation": pearson(x, y) if len(x) >= 3 else None,
        "mean_forward_return": mean(y),
        "top_minus_bottom_training_quantile_return": spread,
        "top_quantile_observations": len(high_returns),
        "bottom_quantile_observations": len(low_returns),
        "ic_interpretation": "exploratory point estimate; no confirmatory conclusion below the 20-observation floor" if not enough else "meets the predeclared observation floor",
    }


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    bars = market_bars(market)
    closes = [bar["close"] for bar in bars]
    factors = _factor_values(bars)
    n = len(bars)
    split = int(n * 0.7)
    horizon = FORECAST_HORIZON_SESSIONS
    labels = forward_returns(closes, horizon)
    metrics: dict[str, Any] = {}
    available = 0
    observations = 0
    for name, (family, values) in factors.items():
        train_indices = non_overlapping(
            [i for i in range(60, split) if i + horizon < split], horizon
        )
        holdout_indices = non_overlapping(
            [i for i in range(split, n) if i + horizon < n], horizon
        )
        train_x, _ = _aligned(values, labels, train_indices)
        low_cut, high_cut = quantile(train_x, 0.2), quantile(train_x, 0.8)
        holdout = _metrics(values, labels, holdout_indices, low_cut, high_cut)
        available += int(holdout["status"] == "available")
        observations += int(holdout["observations"])
        metrics[name] = {
            "family": family,
            "horizon": "5D",
            "training": _metrics(values, labels, train_indices, low_cut, high_cut),
            "holdout": holdout,
        }

    status = "success" if available else ("partial" if observations else "insufficient_data")
    warnings = [
        "Single-security 5-session IC/forward-return diagnostics only; not a cross-sectional Rank IC or portfolio backtest.",
        "Descriptive evidence: this path never feeds the production probability, eligibility or model promotion.",
        "Gross close-to-close diagnostics; no trades, funding, turnover costs or market impact are simulated.",
    ]
    if not available:
        warnings.append(
            f"No 5D holdout factor reached {MIN_HOLDOUT_OBSERVATIONS} observations; ICs remain exploratory."
        )
    primary = metrics.get("momentum_20") or {}
    holdout = primary.get("holdout") or {}
    validation = {
        "model_version": MODEL_VERSION,
        "method": "fixed-factor 5D time-series IC and forward-return diagnostics",
        "universe_type": "single_security",
        "signal_timing": "factor uses OHLCV through close[t]; label is close[t+5] / close[t] - 1",
        "split": {"training_fraction": 0.7, "holdout_fraction": 0.3,
                  "holdout_start_session": bars[split]["date"] if split < n else None},
        "non_overlapping_labels": True,
        "minimum_holdout_observations": MIN_HOLDOUT_OBSERVATIONS,
        "horizons": ["5D"],
        "descriptive_only": True,
        "information_coefficient": holdout.get("spearman_time_series_ic"),
        "holdout_sample_count": holdout.get("observations"),
        "factor_metrics": metrics,
        "leakage_audit": {
            "features_use_data_through_decision_close_only": True,
            "future_returns_used_only_as_labels": True,
            "training_quantile_cutoffs_fixed_before_holdout": True,
            "holdout_labels_non_overlapping": True,
        },
        "full_cross_sectional_backtest": {
            "status": "not_assessed",
            "reason": "requires a multi-ticker factor panel, point-in-time universe and security masks",
        },
    }
    return researcher_result(
        "factor_backtest", ticker, horizon_name, manifest["snapshot_id"],
        status=status, result_role="diagnostic", validation=validation,
        data_used=["daily split-adjusted OHLCV"], warnings=warnings,
    )
