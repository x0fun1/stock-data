"""Research B: fixed OHLCV factor diagnostics, redundancy control, and calibration."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import forward_returns, market_bars, non_overlapping
from ..math_utils import mean, pearson, quantile, sample_std, spearman
from ..snapshot import load_snapshot


def _window_return(values: list[float], i: int, window: int) -> float | None:
    return values[i] / values[i - window] - 1.0 if i >= window and values[i - window] > 0 else None


def _factors(bars: list[dict[str, Any]]) -> dict[str, tuple[str, str, list[float | None]]]:
    """Candidate formulas are specified locally; no shared technical indicators are consumed."""
    n = len(bars)
    close = [row["close"] for row in bars]
    open_ = [row["open"] for row in bars]
    high = [row["high"] for row in bars]
    low = [row["low"] for row in bars]
    volume = [row["volume"] for row in bars]
    result: dict[str, tuple[str, str, list[float | None]]] = {}

    def add(name: str, family: str, formula: str, values: list[float | None]) -> None:
        result[name] = (family, formula, values)

    for window in (5, 20, 60):
        add(f"momentum_{window}", "momentum", f"close[t] / close[t-{window}] - 1", [_window_return(close, i, window) for i in range(n)])
    add("reversal_1", "reversal", "close[t] / close[t-1] - 1", [_window_return(close, i, 1) for i in range(n)])
    trend: list[float | None] = [None] * n
    vol20: list[float | None] = [None] * n
    volume_z: list[float | None] = [None] * n
    range_pos: list[float | None] = [None] * n
    close_location: list[float | None] = [None] * n
    gap: list[float | None] = [None] * n
    volume_return_corr: list[float | None] = [None] * n
    returns: list[float | None] = [_window_return(close, i, 1) for i in range(n)]
    for i in range(n):
        if i >= 19:
            window = close[i - 19:i + 1]
            avg = sum(window) / len(window)
            trend[i] = close[i] / avg - 1 if avg else None
            logs = [math.log(close[j] / close[j - 1]) for j in range(i - 18, i + 1) if close[j - 1] > 0]
            vol20[i] = sample_std(logs)
            vol_values = volume[i - 19:i + 1]
            vol_std = sample_std(vol_values)
            volume_z[i] = (volume[i] - sum(vol_values) / len(vol_values)) / vol_std if vol_std else 0.0
            lowest, highest = min(low[i - 19:i + 1]), max(high[i - 19:i + 1])
            range_pos[i] = (close[i] - lowest) / (highest - lowest) if highest > lowest else 0.5
            rr = [returns[j] for j in range(i - 18, i + 1)]
            vv = [volume[j] / volume[j - 1] - 1 for j in range(i - 18, i + 1) if volume[j - 1] > 0]
            if len(rr) == len(vv) and all(x is not None for x in rr):
                volume_return_corr[i] = pearson([float(x) for x in rr], vv)
        day_range = high[i] - low[i]
        if day_range > 0:
            close_location[i] = (2 * close[i] - high[i] - low[i]) / day_range
        if i > 0 and close[i - 1] > 0:
            gap[i] = open_[i] / close[i - 1] - 1
    add("trend_sma20", "trend", "close[t] / mean(close[t-19:t]) - 1", trend)
    add("realized_volatility_20", "volatility", "sample_std(log(close[j]/close[j-1]), j=t-18..t)", vol20)
    add("volume_z20", "volume", "(volume[t] - mean(volume[t-19:t])) / sample_std(volume[t-19:t])", volume_z)
    add("range_position_20", "trend", "(close[t] - rolling_low20) / (rolling_high20 - rolling_low20)", range_pos)
    add("close_location", "volume", "(2*close[t] - high[t] - low[t]) / (high[t] - low[t])", close_location)
    add("overnight_gap", "volatility", "open[t] / close[t-1] - 1", gap)
    add("volume_return_corr20", "volume", "corr(return, volume_change) over 20 sessions", volume_return_corr)
    return result


def _valid_pairs(values: list[float | None], labels: list[float | None], indices: list[int]) -> tuple[list[float], list[float], list[int]]:
    selected = [i for i in indices if i < len(values) and values[i] is not None and labels[i] is not None and math.isfinite(float(values[i])) and math.isfinite(float(labels[i]))]
    return [float(values[i]) for i in selected], [float(labels[i]) for i in selected], selected


def _factor_metrics(values: list[float | None], labels: list[float | None], indices: list[int]) -> dict[str, Any]:
    x, y, used = _valid_pairs(values, labels, indices)
    rank_ic = spearman(x, y)
    pearson_ic = pearson(x, y)
    q20, q80 = quantile(x, 0.2), quantile(x, 0.8)
    low = [target for value, target in zip(x, y) if q20 is not None and value <= q20]
    high = [target for value, target in zip(x, y) if q80 is not None and value >= q80]
    spread = mean(high) - mean(low) if high and low else None
    chunks: list[float] = []
    if len(used) >= 40:
        size = max(20, len(used) // 4)
        for start in range(0, len(used), size):
            part = used[start:start + size]
            px, py, _ = _valid_pairs(values, labels, part)
            ic = spearman(px, py)
            if ic is not None:
                chunks.append(ic)
    ic_mean = mean(chunks) if chunks else rank_ic
    ic_std = sample_std(chunks) if len(chunks) > 1 else None
    turnover = None
    if len(x) > 1:
        turnover = sum((x[i] >= q80) != (x[i - 1] >= q80) for i in range(1, len(x))) / (len(x) - 1) if q80 is not None else None
    return {
        "rank_ic": rank_ic,
        "pearson_ic": pearson_ic,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": ic_mean / ic_std if ic_mean is not None and ic_std not in (None, 0) else None,
        "top_bottom_quantile_spread": spread,
        "coverage": len(used) / len(indices) if indices else 0.0,
        "sample_size": len(used),
        "top_quantile_turnover": turnover,
    }


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    requested_horizon = HORIZONS[horizon_name]
    bars = market_bars(market)
    close = [row["close"] for row in bars]
    factors = _factors(bars)
    n = len(bars)
    holdout_start = int(n * 0.8)
    train_end = int(n * 0.5)
    calibration_end = holdout_start
    labels_by_horizon = {h: forward_returns(close, h) for h in (1, 5, 20)}
    # Diagnostics are horizon-specific and use only labels available before the final holdout.
    decay: dict[str, dict[str, Any]] = {}
    for name, (family, formula, values) in factors.items():
        horizons: dict[str, Any] = {}
        for horizon in (1, 5, 20):
            labels = labels_by_horizon[horizon]
            eligible = [i for i in range(60, holdout_start) if i + horizon < holdout_start]
            indices = non_overlapping(eligible, horizon)
            horizons[f"{horizon}D"] = _factor_metrics(values, labels, indices)
        decay[name] = {"family": family, "formula": formula, "horizons": horizons}

    # Feature selection and sign are fixed using the first chronological segment.
    select_labels = labels_by_horizon[requested_horizon]
    select_indices = non_overlapping([i for i in range(60, train_end) if i + requested_horizon < train_end], requested_horizon)
    ranked: list[tuple[float, str, str, str, list[float | None], dict[str, Any]]] = []
    for name, (family, formula, values) in factors.items():
        metrics = _factor_metrics(values, select_labels, select_indices)
        score = abs(metrics["rank_ic"] or 0.0)
        ranked.append((score, name, family, formula, values, metrics))
    ranked.sort(key=lambda row: (-row[0], row[1]))

    retained: list[tuple[float, str, str, str, list[float | None], dict[str, Any]]] = []
    for candidate in ranked:
        _, name, family, _, values, metrics = candidate
        if metrics["rank_ic"] is None or abs(metrics["rank_ic"]) < 0.02:
            continue
        correlation = None
        for previous in retained:
            px, py, _ = _valid_pairs(values, previous[4], select_indices)
            corr = pearson(px, py)
            if corr is not None and abs(corr) >= 0.85:
                correlation = {"with": previous[1], "correlation": corr}
                break
        if correlation:
            continue
        if sum(item[2] == family for item in retained) >= 2:
            continue
        retained.append(candidate)
        if len(retained) >= 5:
            break

    warnings: list[str] = ["Returns are gross close-to-close observations; this factor path does not model execution, spreads, slippage or fees."]
    if not retained:
        warnings.append("No candidate factor passed the minimum absolute Rank IC screen in the training segment.")
        return researcher_result("factor", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", validation={"factor_decay": decay}, data_used=["daily raw OHLCV"], warnings=warnings)

    # Standardize each selected factor using the selection segment only.
    standardized: dict[str, list[float | None]] = {}
    signs: dict[str, int] = {}
    for _, name, _, _, values, metrics in retained:
        train_values = [float(values[i]) for i in select_indices if values[i] is not None and math.isfinite(float(values[i]))]
        center, scale = mean(train_values), sample_std(train_values)
        if center is None or not scale:
            continue
        signs[name] = 1 if (metrics["rank_ic"] or 0) >= 0 else -1
        standardized[name] = [max(-3.0, min(3.0, (float(value) - center) / scale)) if value is not None else None for value in values]
    if not standardized:
        return researcher_result("factor", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", warnings=["Selected factors had no stable scale in the training segment."])

    def composite(index: int) -> float | None:
        values = [standardized[name][index] * signs[name] for name in standardized if standardized[name][index] is not None]
        return sum(values) / len(values) if len(values) == len(standardized) and values else None

    calibration_indices = non_overlapping([i for i in range(train_end, calibration_end) if i >= 60 and i + requested_horizon < calibration_end], requested_horizon)
    calibration_pairs = [(float(composite(i)), float(select_labels[i])) for i in calibration_indices if composite(i) is not None]
    scores = [pair[0] for pair in calibration_pairs]
    low_cut, high_cut = quantile(scores, 1 / 3), quantile(scores, 2 / 3)
    bucket_samples: dict[str, list[float]] = {"low": [], "mid": [], "high": []}
    for score, target in calibration_pairs:
        bucket = "low" if low_cut is not None and score <= low_cut else "high" if high_cut is not None and score >= high_cut else "mid"
        bucket_samples[bucket].append(target)
    current_score = composite(n - 1)
    current_bucket = None if current_score is None else "low" if low_cut is not None and current_score <= low_cut else "high" if high_cut is not None and current_score >= high_cut else "mid"
    selected_returns = bucket_samples.get(current_bucket, [])
    probability = sum(value > 0 for value in selected_returns) / len(selected_returns) if current_bucket is not None and len(selected_returns) >= 20 else None
    expected = mean(selected_returns) if probability is not None else None
    if probability is None:
        if current_bucket is None:
            warnings.append("The latest OHLCV row did not produce a complete selected-factor score.")
        else:
            warnings.append(f"Calibration bucket {current_bucket!r} has {len(selected_returns)} observations; at least 20 are required for a probability.")

    holdout_pairs: list[tuple[float, int]] = []
    holdout_indices = non_overlapping(list(range(holdout_start, n - requested_horizon)), requested_horizon)
    for i in holdout_indices:
        score = composite(i)
        target = select_labels[i]
        if score is None or target is None:
            continue
        bucket = "low" if low_cut is not None and score <= low_cut else "high" if high_cut is not None and score >= high_cut else "mid"
        sample = bucket_samples[bucket]
        if len(sample) >= 20:
            holdout_pairs.append((sum(value > 0 for value in sample) / len(sample), int(target > 0)))
    brier = mean([(p - y) ** 2 for p, y in holdout_pairs])
    calibration_prevalence = mean([float(target > 0) for _, target in calibration_pairs])
    baseline_brier = mean([(calibration_prevalence - y) ** 2 for _, y in holdout_pairs]) if calibration_prevalence is not None else None

    lookbacks = {"momentum_5": 5, "momentum_20": 20, "momentum_60": 60, "reversal_1": 1, "trend_sma20": 20, "realized_volatility_20": 20, "volume_z20": 20, "range_position_20": 20, "close_location": 1, "overnight_gap": 1, "volume_return_corr20": 20}
    selected_factor_rows = [{"factor_name": name, "family": family, "formula": formula, "lookback": lookbacks.get(name), "training_direction": "positive" if signs.get(name, 1) > 0 else "negative", "current_contribution": standardized[name][-1] * signs[name] if standardized[name][-1] is not None else None, "training_rank_ic": metrics["rank_ic"], "coverage": metrics["coverage"]} for _, name, family, formula, _, metrics in retained if name in standardized]
    positive, negative = [], []
    for item in selected_factor_rows:
        if item["current_contribution"] is None:
            continue
        group = positive if item["current_contribution"] >= 0 else negative
        group.append({key: item[key] for key in ("family", "factor_name", "formula", "training_rank_ic", "training_direction", "current_contribution")})
    diversity = len({item["family"] for item in selected_factor_rows})
    status = "success" if probability is not None and len(holdout_pairs) >= 10 else "partial" if probability is not None else "insufficient_data"
    if len(holdout_pairs) < 10:
        warnings.append("The untouched chronological holdout has fewer than 10 factor-calibration predictions.")
    validation = {"method": "training-segment factor selection + correlation pruning + later empirical bucket calibration", "selected_factors": selected_factor_rows, "factor_family_count": diversity, "factor_decay": decay, "calibration_observations": len(calibration_pairs), "calibration_bucket_counts": {key: len(value) for key, value in bucket_samples.items()}, "calibration_base_rate": calibration_prevalence, "holdout_predictions": len(holdout_pairs), "holdout_brier": brier, "holdout_base_rate_brier": baseline_brier, "holdout_start_session": bars[holdout_start]["date"] if holdout_start < n else None, "factor_correlation_threshold": 0.85, "leakage_audit": {"features_use_data_through_decision_close_only": True, "future_returns_used_only_as_labels": True, "selection_precedes_calibration_and_holdout": True, "calibration_labels_non_overlapping": True}}
    validation["diagnostics_label_end_exclusive"] = holdout_start
    validation["sample_feasibility"] = {"minimum_bucket_observations": 20, "minimum_holdout_predictions": 10, "calibration_available": len(calibration_pairs), "requested_window_preserved": True}
    return researcher_result(
        "factor", ticker, horizon_name, manifest["snapshot_id"], status=status,
        prob_up=probability, expected_return=expected,
        evidence={"positive": positive, "negative": negative}, validation=validation,
        data_used=["daily raw OHLCV"], warnings=warnings,
        probability_source="out_of_sample_factor_score_bucket_frequency" if probability is not None else None,
    )
