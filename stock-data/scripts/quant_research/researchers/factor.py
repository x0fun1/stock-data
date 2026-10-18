"""Research B: fixed OHLCV factor diagnostics, redundancy control, and calibration."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import forward_returns, market_bars, non_overlapping
from ..math_utils import (
    benjamini_yekutieli,
    circular_block_bootstrap_spearman,
    circular_shift_spearman_p_value,
    mean,
    pearson,
    quantile,
    sample_std,
    spearman,
)
from ..reliability import ROUTE_ABSTAINED, probabilistic_scores, qualification, sample_diagnostics
from ..selection import multiple_testing_disclosure, overlapping_ic, shift_p_values, stability_verdict
from ..snapshot import load_snapshot

MINIMUM_FACTOR_BUCKET_OBSERVATIONS = 6
MINIMUM_SELECTION_LABELS = 10
SHRINKAGE_PRIOR_OBSERVATIONS = 12
SELECTION_FRACTION = 0.5
FEATURE_WARMUP_SESSIONS = 60
MINIMUM_ABS_SELECTION_IC = 0.02
MAX_FACTORS = 5
MAX_FACTORS_PER_FAMILY = 2
MAX_ABS_FACTOR_CORRELATION = 0.85
MINIMUM_SIGN_CONSISTENCY = 2 / 3
# "overlapping_block" estimates the Rank IC on every matured label of the segment and
# requires directional stability; it is implemented and evaluated, but the registered
# comparison could not separate it from the incumbent rule on a 52-week window, so the
# pre-existing rule stays in production (see references/quant-paths.md).
SELECTION_METRIC = "non_overlapping_point"


def _spaced_label_count(first: int, last: int, horizon: int) -> int:
    return (last - first) // (horizon + 1) + 1 if last >= first else 0


def _selection_label_capacity(bar_count: int, horizon: int) -> int:
    """Labels available to rank factors inside the fixed chronological selection segment."""
    selection_end = int(bar_count * SELECTION_FRACTION)
    return _spaced_label_count(FEATURE_WARMUP_SESSIONS, selection_end - horizon - 1, horizon)


def _matured_label_capacity(bar_count: int, horizon: int) -> int:
    """Labels whose outcome is already known by the final bar (usable for today's probability)."""
    return _spaced_label_count(FEATURE_WARMUP_SESSIONS, bar_count - 1 - horizon, horizon)


def _sample_feasibility(bar_count: int, horizon: int) -> dict[str, Any]:
    required = 3 * MINIMUM_FACTOR_BUCKET_OBSERVATIONS

    def ready(size: int) -> bool:
        return (
            _selection_label_capacity(size, horizon) >= MINIMUM_SELECTION_LABELS
            and _matured_label_capacity(size, horizon) >= required
        )

    low, high = 0, max(256, bar_count)
    while not ready(high):
        high *= 2
    while low < high:
        middle = (low + high) // 2
        if ready(middle):
            high = middle
        else:
            low = middle + 1
    capacity = _matured_label_capacity(bar_count, horizon)
    selection_capacity = _selection_label_capacity(bar_count, horizon)
    return {
        "status": "capacity_below_52_week_minimum" if not ready(bar_count) else "budget_possible_not_guaranteed_by_factor_scores",
        "window_standard": "52_weeks_of_daily_bars",
        "minimum_bucket_observations": MINIMUM_FACTOR_BUCKET_OBSERVATIONS,
        "required_calibration_observations_for_three_balanced_buckets": required,
        "minimum_selection_labels": MINIMUM_SELECTION_LABELS,
        "selection_label_capacity": selection_capacity,
        "max_matured_non_overlapping_label_capacity": capacity,
        "calibration_available": 0,
        "calibration_observations_with_complete_factor_scores": 0,
        "calibration_bucket_counts": {"low": 0, "mid": 0, "high": 0},
        "all_current_buckets_meet_minimum": False,
        "estimated_minimum_daily_bars": low,
        "estimated_minimum_calendar_weeks": round(low / 5.0, 1),
        "additional_daily_bars_at_current_history_length": max(0, low - bar_count),
        "estimate_assumptions": "52-week standard: fixed 50% chronological selection segment, 60-session feature warm-up, H+1-session label spacing, labels matured by the decision bar, and three balanced buckets of 6 observations; score ties and missing factors can increase the requirement",
        "requested_window_preserved": True,
    }


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
    add("close_location", "trend", "(2*close[t] - high[t] - low[t]) / (high[t] - low[t])", close_location)
    add("overnight_gap", "momentum", "open[t] / close[t-1] - 1", gap)
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
    low = [target for value, target in zip(x, y, strict=True) if q20 is not None and value <= q20]
    high = [target for value, target in zip(x, y, strict=True) if q80 is not None and value >= q80]
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


def _oos_factor_inference(
    factors: dict[str, tuple[str, str, list[float | None]]],
    labels: list[float | None],
    indices: list[int],
    bars: list[dict[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    """Evaluate every attempted factor on the untouched final window; never select from this output."""
    rows: dict[str, dict[str, Any]] = {}
    raw_p_values: dict[str, float | None] = {}
    for name, (family, formula, values) in factors.items():
        x, y, used = _valid_pairs(values, labels, indices)
        observations = len(used)
        block_length = max(horizon, round(observations ** (1 / 3))) if observations else horizon
        bootstrap = circular_block_bootstrap_spearman(
            x, y, block_length=block_length, replicates=300, seed=1701 + len(rows),
        )
        shift_test = circular_shift_spearman_p_value(x, y, block_length=block_length)
        split = len(used) // 2
        first_ic = spearman(x[:split], y[:split]) if split >= 10 else None
        second_ic = spearman(x[split:], y[split:]) if len(used) - split >= 10 else None
        raw_p_values[name] = shift_test.get("p_value") if isinstance(shift_test.get("p_value"), (int, float)) else None
        rows[name] = {
            "family": family,
            "formula": formula,
            "oos_time_series_ic": spearman(x, y),
            "observations": observations,
            "data_start": bars[used[0]]["date"] if used else None,
            "data_end": bars[used[-1] + horizon]["date"] if used else None,
            "block_bootstrap": bootstrap,
            "null_test": shift_test,
            "partwise_ic": {"first_half": first_ic, "second_half": second_ic},
            "direction_consistent_across_halves": (
                first_ic is not None and second_ic is not None
                and (first_ic >= 0) == (second_ic >= 0)
            ) if first_ic is not None and second_ic is not None else None,
            "p_value": raw_p_values[name],
            "p_value_adjusted": None,
        }
    adjusted = benjamini_yekutieli(raw_p_values, trials=len(factors))
    for name, value in adjusted.items():
        rows[name]["p_value_adjusted"] = value
    available_tests = sum(value is not None for value in raw_p_values.values())
    return {
        "status": "exploratory" if available_tests else "insufficient_samples",
        "universe_type": "single_security_time_series",
        "method": "OOS Spearman IC; circular moving-block bootstrap percentile interval; circular-shift null diagnostic; Benjamini-Yekutieli FDR",
        "p_value_method": "circular_shift_test_stationarity_assumption",
        "p_adjustment_method": "Benjamini-Yekutieli FDR; conservative under arbitrary test dependence",
        "trials": len(factors),
        "tests_with_p_value": available_tests,
        "sample_count": len(indices),
        "horizon_sessions": horizon,
        "data_start": bars[indices[0]]["date"] if indices else None,
        "data_end": bars[min(len(bars) - 1, indices[-1] + horizon)]["date"] if indices else None,
        "factor_selection_used_this_window": False,
        "stationarity_assumption_validated": False,
        "bootstrap_coverage_validated": False,
        "market_regime_stability": "not_assessed_no_frozen_regime_definition",
        "factors": rows,
    }


def _empty_oos_forecasts(
    indices: list[int], bars: list[dict[str, Any]], labels: list[float | None], horizon: int,
) -> list[dict[str, Any]]:
    target_definition = f"close[t+{horizon}] / close[t] - 1 > 0"
    return [{
        "decision_session": bars[index]["date"],
        "label_end_session": bars[index + horizon]["date"],
        "target": int(float(labels[index]) > 0),
        "probability": None,
        "training_label_end_session": None,
        "target_definition": target_definition,
    } for index in indices if labels[index] is not None]


def _bucket_for_score(score: float | None, low_cut: float | None, high_cut: float | None) -> str | None:
    if score is None or low_cut is None or high_cut is None:
        return None
    if score <= low_cut:
        return "low"
    if score >= high_cut:
        return "high"
    return "mid"


def _bucket_cuts(scores: list[float]) -> tuple[float | None, float | None]:
    """Causal terciles from matured scores; fall back to a narrow band when terciles collapse."""
    low_cut, high_cut = quantile(scores, 1 / 3), quantile(scores, 2 / 3)
    if low_cut is not None and high_cut is not None and low_cut < high_cut:
        return low_cut, high_cut
    center, scale = mean(scores), sample_std(scores)
    if center is None or not scale:
        return None, None
    return center - 0.25 * scale, center + 0.25 * scale


def _shrunk_probability(
    samples: list[float], base_rate: float | None, prior: int = SHRINKAGE_PRIOR_OBSERVATIONS,
) -> float | None:
    """Empirical bucket frequency shrunk toward the matured base rate for thin 52-week samples."""
    if not samples:
        return None
    raw = sum(value > 0 for value in samples) / len(samples)
    if base_rate is None or prior <= 0:
        return raw
    return (len(samples) * raw + prior * base_rate) / (len(samples) + prior)


def _shrunk_mean(
    samples: list[float], overall: float | None, prior: int = SHRINKAGE_PRIOR_OBSERVATIONS,
) -> float | None:
    if not samples:
        return None
    center = mean(samples)
    if center is None:
        return None
    if overall is None or prior <= 0:
        return center
    return (len(samples) * center + prior * overall) / (len(samples) + prior)


def run(
    snapshot_dir: Path,
    loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None,
    *,
    selection_override: str | None = None,
) -> dict[str, Any]:
    """Produce the Research B probability.

    ``selection_override`` exists for reproducible rule comparisons and tests; the live
    path always uses ``SELECTION_METRIC``. It never changes the evaluation boundary.
    """
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    requested_horizon = HORIZONS[horizon_name]
    bars = market_bars(market)
    close = [row["close"] for row in bars]
    factors = _factors(bars)
    n = len(bars)
    sample_feasibility = _sample_feasibility(n, requested_horizon)
    train_end = int(n * SELECTION_FRACTION)
    holdout_start = train_end
    holdout_indices = non_overlapping(list(range(holdout_start, n - requested_horizon)), requested_horizon)
    labels_by_horizon = {h: forward_returns(close, h) for h in (1, 5, 20)}
    select_labels = labels_by_horizon[requested_horizon]
    oos_factor_inference = _oos_factor_inference(factors, select_labels, holdout_indices, bars, requested_horizon)
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

    # Feature selection and sign are fixed using the first chronological segment, and only
    # labels that matured inside it. The legacy rule spaces labels by the horizon; the
    # dependency-aware rule keeps every matured label of the segment and measures overlap
    # with a moving-block bootstrap, because ten spaced labels cannot rank eleven candidates.
    selection_metric = str(selection_override or SELECTION_METRIC)
    selection_pool = [i for i in range(FEATURE_WARMUP_SESSIONS, train_end) if i + requested_horizon < train_end]
    select_indices = (
        non_overlapping(selection_pool, requested_horizon)
        if selection_metric == "non_overlapping_point"
        else selection_pool
    )
    selection_evidence: dict[str, dict[str, Any]] = {}
    selection_p: dict[str, float | None] = {}
    rejected: list[dict[str, Any]] = []
    ranked: list[tuple[float, str, str, str, list[float | None], dict[str, Any]]] = []
    for name, (family, formula, values) in factors.items():
        metrics = _factor_metrics(values, select_labels, select_indices)
        # The dependency-aware estimate is always computed: it is the diagnostic that makes
        # the incumbent rule's ten-observation basis visible, even when it is not the rule
        # that decides the factor set.
        evidence = overlapping_ic(values, select_labels, selection_pool, horizon=requested_horizon)
        stability = stability_verdict(
            evidence,
            min_abs_rank_ic=MINIMUM_ABS_SELECTION_IC,
            min_sign_consistency=MINIMUM_SIGN_CONSISTENCY,
        )
        selection_p[name] = shift_p_values(
            values, select_labels, selection_pool, horizon=requested_horizon,
        ).get("p_value")
        if selection_metric == "overlapping_block":
            selection_rank_ic = evidence.get("rank_ic")
            verdict = stability
        else:
            selection_rank_ic = metrics["rank_ic"]
            verdict = {
                "passed": selection_rank_ic is not None and abs(selection_rank_ic) >= MINIMUM_ABS_SELECTION_IC,
                "reason": "legacy_point_estimate_rule",
            }
        selection_evidence[name] = {
            **evidence,
            "applied_rule": selection_metric,
            "stability_would_pass": stability["passed"],
            "stability_reason": stability["reason"],
        }
        ranked.append((
            abs(float(selection_rank_ic)) if selection_rank_ic is not None else 0.0,
            name, family, formula, values,
            {**metrics, "selection": evidence, "verdict": verdict, "selection_rank_ic": selection_rank_ic},
        ))
    ranked.sort(key=lambda row: (-row[0], row[1]))

    retained: list[tuple[float, str, str, str, list[float | None], dict[str, Any]]] = []
    for candidate in ranked:
        _, name, family, _, values, metrics = candidate
        if not metrics["verdict"]["passed"]:
            rejected.append({"factor": name, "reason": metrics["verdict"]["reason"]})
            continue
        correlation = None
        for previous in retained:
            px, py, _ = _valid_pairs(values, previous[4], select_indices)
            corr = pearson(px, py)
            if corr is not None and abs(corr) >= MAX_ABS_FACTOR_CORRELATION:
                correlation = {"with": previous[1], "correlation": corr}
                break
        if correlation:
            rejected.append({"factor": name, "reason": "redundant", **correlation})
            continue
        if sum(item[2] == family for item in retained) >= MAX_FACTORS_PER_FAMILY:
            rejected.append({"factor": name, "reason": "family_limit", "family": family})
            continue
        retained.append(candidate)
        if len(retained) >= MAX_FACTORS:
            break

    warnings: list[str] = [
        "Returns are gross close-to-close observations; this factor path does not model execution, spreads, slippage or fees.",
        "Factor calibration uses only the 52-week daily-bar standard and never widens the window automatically to buy samples.",
    ]
    spaced_selection_labels = len(non_overlapping(selection_pool, requested_horizon))
    if spaced_selection_labels < MINIMUM_SELECTION_LABELS:
        warnings.append(
            f"Only {spaced_selection_labels} non-overlapping selection labels are available; at least "
            f"{MINIMUM_SELECTION_LABELS} are required under the 52-week standard, so factor ranks would be noise."
        )
        return researcher_result(
            "factor", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data",
            validation={
                "factor_decay": decay,
                "oos_factor_inference": oos_factor_inference,
                "candidate_factor_count": len(factors),
                "sample_feasibility": sample_feasibility,
                "selection_label_count": len(select_indices),
                "oos_forecasts": _empty_oos_forecasts(holdout_indices, bars, select_labels, requested_horizon),
                "target_definition": f"close[t+{requested_horizon}] / close[t] - 1 > 0",
            },
            data_used=["daily raw OHLCV"], warnings=warnings,
        )
    if not retained:
        warnings.append("No candidate factor passed the minimum absolute Rank IC screen in the training segment.")
        return researcher_result("factor", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", validation={"factor_decay": decay, "oos_factor_inference": oos_factor_inference, "candidate_factor_count": len(factors), "sample_feasibility": sample_feasibility, "oos_forecasts": _empty_oos_forecasts(holdout_indices, bars, select_labels, requested_horizon), "target_definition": f"close[t+{requested_horizon}] / close[t] - 1 > 0"}, data_used=["daily raw OHLCV"], warnings=warnings)

    # Standardize each selected factor using the selection segment only.
    standardized: dict[str, list[float | None]] = {}
    signs: dict[str, int] = {}
    for _, name, _, _, values, metrics in retained:
        train_values = [float(values[i]) for i in select_indices if values[i] is not None and math.isfinite(float(values[i]))]
        center, scale = mean(train_values), sample_std(train_values)
        if center is None or not scale:
            continue
        direction_ic = metrics["selection_rank_ic"] if metrics["selection_rank_ic"] is not None else metrics["rank_ic"]
        signs[name] = 1 if (direction_ic or 0) >= 0 else -1
        standardized[name] = [max(-3.0, min(3.0, (float(value) - center) / scale)) if value is not None else None for value in values]
    if not standardized:
        return researcher_result("factor", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", validation={"oos_factor_inference": oos_factor_inference, "candidate_factor_count": len(factors), "sample_feasibility": sample_feasibility, "oos_forecasts": _empty_oos_forecasts(holdout_indices, bars, select_labels, requested_horizon), "target_definition": f"close[t+{requested_horizon}] / close[t] - 1 > 0"}, warnings=["Selected factors had no stable scale in the training segment."])

    def composite(index: int) -> float | None:
        values = [standardized[name][index] * signs[name] for name in standardized if standardized[name][index] is not None]
        return sum(values) / len(values) if len(values) == len(standardized) and values else None

    # Every label matured by the final bar forms the causal calibration ledger (52-week standard).
    calibration_indices = non_overlapping(
        list(range(FEATURE_WARMUP_SESSIONS, n - requested_horizon)), requested_horizon,
    )
    calibration_pairs = [(float(composite(i)), float(select_labels[i])) for i in calibration_indices if composite(i) is not None]
    # Bucket cut points are recomputed from matured scores only, so thin 52-week ledgers stay balanced
    # and neither the cuts nor the frequencies can see a label that has not matured yet.
    low_cut, high_cut = _bucket_cuts([score for score, _ in calibration_pairs])
    bucket_samples: dict[str, list[float]] = {"low": [], "mid": [], "high": []}
    for score, target in calibration_pairs:
        bucket = _bucket_for_score(score, low_cut, high_cut)
        if bucket is not None:
            bucket_samples[bucket].append(target)
    calibration_prevalence = mean([float(target > 0) for _, target in calibration_pairs])
    overall_forward_return = mean([target for _, target in calibration_pairs])
    sample_feasibility["calibration_observations_with_complete_factor_scores"] = len(calibration_pairs)
    sample_feasibility["calibration_available"] = len(calibration_pairs)
    sample_feasibility["calibration_bucket_counts"] = {key: len(value) for key, value in bucket_samples.items()}
    sample_feasibility["all_current_buckets_meet_minimum"] = all(
        len(values) >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS for values in bucket_samples.values()
    )
    current_score = composite(n - 1)
    current_bucket = _bucket_for_score(current_score, low_cut, high_cut)
    selected_returns = bucket_samples.get(current_bucket, [])
    probability = (
        _shrunk_probability(selected_returns, calibration_prevalence)
        if current_bucket is not None and len(selected_returns) >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS
        else None
    )
    expected = _shrunk_mean(selected_returns, overall_forward_return) if probability is not None else None
    if probability is None:
        if current_bucket is None:
            warnings.append("The latest OHLCV row did not produce a complete selected-factor score.")
        else:
            warnings.append(
                f"Calibration bucket {current_bucket!r} has {len(selected_returns)} matured observations; at least "
                f"{MINIMUM_FACTOR_BUCKET_OBSERVATIONS} are required for a probability."
            )
    elif min(len(values) for values in bucket_samples.values()) < 2 * MINIMUM_FACTOR_BUCKET_OBSERVATIONS:
        warnings.append(
            "At least one 52-week calibration bucket is thin; probabilities are shrunk toward the matured base rate "
            f"with a prior weight of {SHRINKAGE_PRIOR_OBSERVATIONS} observations."
        )

    holdout_pairs: list[tuple[float, int, float | None]] = []
    oos_forecasts: list[dict[str, Any]] = []
    for i in holdout_indices:
        score = composite(i)
        target = select_labels[i]
        if target is None:
            continue
        # Rolling origin: only labels whose outcome was known at decision session i may train this forecast.
        matured = [j for j in calibration_indices if j + requested_horizon <= i]
        matured_pairs = [
            (float(composite(j)), float(select_labels[j]))
            for j in matured
            if composite(j) is not None and select_labels[j] is not None
        ]
        matured_base_rate = mean([float(value > 0) for _, value in matured_pairs])
        matured_low_cut, matured_high_cut = _bucket_cuts([value for value, _ in matured_pairs])
        matured_buckets: dict[str, list[float]] = {"low": [], "mid": [], "high": []}
        for matured_score, matured_target in matured_pairs:
            bucket = _bucket_for_score(matured_score, matured_low_cut, matured_high_cut)
            if bucket is not None:
                matured_buckets[bucket].append(matured_target)
        training_end = max((j + requested_horizon for j in matured), default=None)
        bucket = _bucket_for_score(score, matured_low_cut, matured_high_cut)
        sample = matured_buckets.get(bucket, [])
        forecast_probability = (
            _shrunk_probability(sample, matured_base_rate)
            if bucket is not None and len(sample) >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS
            else None
        )
        oos_forecasts.append({
            "decision_session": bars[i]["date"],
            "label_end_session": bars[i + requested_horizon]["date"],
            "target": int(target > 0),
            "probability": forecast_probability,
            "training_label_end_session": bars[training_end]["date"] if training_end is not None else None,
            "training_sample_count": len(sample),
            "training_observation_count": len(matured_pairs),
            "target_definition": f"close[t+{requested_horizon}] / close[t] - 1 > 0",
        })
        if forecast_probability is not None:
            holdout_pairs.append((forecast_probability, int(target > 0), matured_base_rate))
    brier = mean([(p - y) ** 2 for p, y, _ in holdout_pairs])
    baseline_brier = mean([(base - y) ** 2 for _, y, base in holdout_pairs if base is not None])

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
    if probability is not None and len(holdout_pairs) < 10:
        warnings.append("The rolling-origin evaluation has fewer than 10 matured-label factor predictions.")
    validation = {"method": "52-week standard: training-segment factor selection + correlation pruning + rolling-origin matured-label bucket calibration with causal terciles", "selected_factors": selected_factor_rows, "factor_family_count": diversity, "factor_decay": decay, "oos_factor_inference": oos_factor_inference, "oos_forecasts": oos_forecasts, "oos_evaluable_samples": len(oos_forecasts), "oos_predictions": len(holdout_pairs), "oos_coverage": len(holdout_pairs) / len(oos_forecasts) if oos_forecasts else 0.0, "oos_abstain_rate": 1 - len(holdout_pairs) / len(oos_forecasts) if oos_forecasts else None, "target_definition": f"close[t+{requested_horizon}] / close[t] - 1 > 0", "candidate_factor_count": len(factors), "selection_label_count": len(select_indices), "evaluation_mode": "rolling_origin_matured_labels", "evaluation_predictions": len(holdout_indices), "calibration_label_capacity": len(calibration_indices), "calibration_observations": len(calibration_pairs), "calibration_bucket_counts": {key: len(value) for key, value in bucket_samples.items()}, "calibration_bucket_raw_probabilities": {key: (sum(value > 0 for value in values) / len(values) if values else None) for key, values in bucket_samples.items()}, "calibration_bucket_cut_points": {"low": low_cut, "high": high_cut}, "bucket_cuts_recomputed_from_matured_scores": True, "calibration_base_rate": calibration_prevalence, "probability_shrinkage": {"prior_observations": SHRINKAGE_PRIOR_OBSERVATIONS, "bucket_weight": len(selected_returns) / (len(selected_returns) + SHRINKAGE_PRIOR_OBSERVATIONS) if probability is not None else None, "base_rate": calibration_prevalence}, "holdout_predictions": len(holdout_pairs), "holdout_brier": brier, "holdout_base_rate_brier": baseline_brier, "holdout_start_session": bars[holdout_start]["date"] if holdout_start < n else None, "factor_correlation_threshold": 0.85, "leakage_audit": {"features_use_data_through_decision_close_only": True, "future_returns_used_only_as_labels": True, "selection_precedes_calibration_and_holdout": True, "calibration_labels_non_overlapping": True, "calibration_labels_matured_before_each_decision": True, "oos_inference_did_not_select_or_modify_probability": True}}
    validation["diagnostics_label_end_exclusive"] = holdout_start
    sample_feasibility["minimum_holdout_predictions"] = 10
    validation["sample_feasibility"] = sample_feasibility
    predictions = [
        (position, forecast, base, outcome)
        for position, (forecast, outcome, base) in enumerate(holdout_pairs)
    ]
    validation["probabilistic_scores"] = probabilistic_scores(predictions, horizon=requested_horizon)
    validation["sample_diagnostics"] = sample_diagnostics(
        bar_count=n, warmup=FEATURE_WARMUP_SESSIONS, horizon=requested_horizon,
    )
    validation["selection"] = {
        "metric": selection_metric,
        "pool_observations": len(selection_pool),
        "spaced_observations": spaced_selection_labels,
        "minimum_abs_rank_ic": MINIMUM_ABS_SELECTION_IC,
        "minimum_sign_consistency": MINIMUM_SIGN_CONSISTENCY,
        "max_factors": MAX_FACTORS,
        "max_factors_per_family": MAX_FACTORS_PER_FAMILY,
        "max_abs_correlation": MAX_ABS_FACTOR_CORRELATION,
        "multiple_testing": multiple_testing_disclosure(selection_p),
        "candidates": selection_evidence,
        "rejected": rejected,
        "overlap_treated_as_dependence": selection_metric == "overlapping_block",
    }
    route = qualification(
        nonoverlap_matured=len(calibration_indices),
        bucket_observations=len(selected_returns),
        buckets_available=sum(1 for values in bucket_samples.values() if values),
        validation_predictions=len(holdout_pairs),
    )
    if probability is None:
        route["route"] = ROUTE_ABSTAINED
        route["route_reason"] = "probability_unavailable_for_current_session"
        route["legacy_probability_available"] = False
    else:
        route["legacy_probability_available"] = True
    validation["qualification"] = route
    return researcher_result(
        "factor", ticker, horizon_name, manifest["snapshot_id"], status=status,
        prob_up=probability, expected_return=expected,
        evidence={"positive": positive, "negative": negative}, validation=validation,
        data_used=["daily raw OHLCV"], warnings=warnings,
        probability_source="out_of_sample_factor_score_bucket_frequency" if probability is not None else None,
    )
