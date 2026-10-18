"""Deterministic 20-session composite momentum feature (``momentum_20d_v1``).

The composite score is a *feature*, never a probability. It is deliberately kept
outside the historical-frequency estimator in :mod:`quant`: the plan that defines it
forbids using it as a new sample filter, and forbids renormalising weights when a
factor is missing. A missing or degenerate factor therefore yields ``None`` plus an
explicit status instead of a silently rescaled number.

Three factors, fixed nominal weights, no runtime tuning:

* risk-adjusted momentum ``A20 = L20 / (sigma60 * sqrt(20))``            weight 0.50
* regression trend momentum ``T20 = (20 * beta / (sigma60 * sqrt(20))) * R2`` weight 0.30
* relative strength momentum ``R20 = Excess20 / (TE60 * sqrt(20))``      weight 0.20

Each factor is compressed with ``tanh(x / scale)``; the first version uses
``scale = 1`` and is reported as uncalibrated. The absolute 20-session ROC direction is
reported separately and is *not* forced to agree with the composite score.
"""

from __future__ import annotations

import math
from typing import Any

MOMENTUM_MODEL_VERSION = "momentum_20d_v1"

# Explicitly defined degenerate-data thresholds. The plan requires a tested floor
# rather than a comparison against the machine minimum.
MIN_DAILY_VOLATILITY = 1e-6
MIN_TRACKING_ERROR = 1e-6
BENCHMARK_OVERLAP_RATIO = 0.10
FLAT_ROC_TOLERANCE = 1e-12

DEFAULT_CONFIG: dict[str, Any] = {
    "version": MOMENTUM_MODEL_VERSION,
    "windows": {
        "momentum_days": 20,
        "volatility_days": 60,
        "regression_price_points": 21,
        "tracking_error_days": 60,
        "annual_trading_days": 252,
    },
    "weights": {"risk_adjusted": 0.50, "regression_trend": 0.30, "relative_strength": 0.20},
    "normalization": {
        "method": "tanh",
        "scale_A": 1.0,
        "scale_T": 1.0,
        "scale_R": 1.0,
        "calibration_status": "uncalibrated",
    },
    "validation": {
        "minimum_daily_returns": 60,
        "std_ddof": 1,
        "reject_nonfinite": True,
        "reject_invalid_prices": True,
        "reject_near_zero_denominators": True,
        "require_all_components": True,
        "allow_silent_weight_redistribution": False,
    },
    "output": {
        "score_min": -100.0,
        "score_max": 100.0,
        "display_decimals": 2,
        "include_component_details": True,
        "include_roc_direction": True,
    },
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _stdev(values: list[float], ddof: int = 1) -> float | None:
    """Sample standard deviation with an explicit ``ddof``; ``None`` when undefined."""
    count = len(values)
    if count - ddof <= 0:
        return None
    average = math.fsum(values) / count
    variance = math.fsum((value - average) ** 2 for value in values) / (count - ddof)
    # Floating point can return a tiny negative value for a constant series.
    variance = max(variance, 0.0)
    return math.sqrt(variance)


def roc(values: list[float], index: int, window: int = 20) -> float | None:
    """Absolute ``window``-session return, identical to the pre-existing baseline."""
    if index < window or index >= len(values) or values[index - window] <= 0:
        return None
    return values[index] / values[index - window] - 1.0


def _ols_slope_r2(series: list[float]) -> tuple[float | None, float | None, bool]:
    """OLS of ``series[k]`` on ``k = 0..n-1``; returns ``(slope, r_squared, r2_defined)``."""
    count = len(series)
    if count < 3:
        return None, None, False
    mean_k = (count - 1) / 2.0
    mean_y = math.fsum(series) / count
    sxx = math.fsum((k - mean_k) ** 2 for k in range(count))
    if sxx <= 0:
        return None, None, False
    sxy = math.fsum((k - mean_k) * (value - mean_y) for k, value in enumerate(series))
    syy = math.fsum((value - mean_y) ** 2 for value in series)
    slope = sxy / sxx
    if syy <= 0:
        # A perfectly flat window has slope zero by construction; R2 is undefined
        # because there is no variance to explain. Report it as zero, flagged.
        return 0.0, 0.0, False
    r_squared = (slope * sxy) / syy
    r_squared = min(max(r_squared, 0.0), 1.0)
    return slope, r_squared, True


def _component(weight: float) -> dict[str, Any]:
    return {
        "raw_value": None,
        "normalized_value": None,
        "weight": weight,
        "contribution": None,
        "status": "unavailable",
        "reason": None,
    }


def _fail(component: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    component["status"] = status
    component["reason"] = reason
    return component


def _normalize(raw: float, scale: float) -> float:
    return math.tanh(raw / scale)


def _benchmark_series(benchmark: Any) -> dict[str, float] | None:
    """Accept a date -> adjusted close mapping; reject anything unparsable."""
    if not isinstance(benchmark, dict):
        return None
    series: dict[str, float] = {}
    for date, value in benchmark.items():
        if not isinstance(date, str) or not _finite(value) or float(value) <= 0:
            return None
        series[str(date)[:10]] = float(value)
    return series or None


def _evaluate(
    closes: list[float],
    dates: list[str],
    benchmark: dict[str, float] | None,
    index: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate every factor at ``index`` using only ``closes[: index + 1]``."""
    windows = config["windows"]
    weights = config["weights"]
    scales = config["normalization"]
    momentum_days = int(windows["momentum_days"])
    volatility_days = int(windows["volatility_days"])
    regression_points = int(windows["regression_price_points"])
    tracking_days = int(windows["tracking_error_days"])

    components = {
        "risk_adjusted": _component(weights["risk_adjusted"]),
        "regression_trend": _component(weights["regression_trend"]),
        "relative_strength": _component(weights["relative_strength"]),
    }
    components["regression_trend"]["beta"] = None
    components["regression_trend"]["r_squared"] = None
    components["regression_trend"]["r_squared_defined"] = False
    result: dict[str, Any] = {
        "return_20d": None,
        "momentum_direction": None,
        "components": components,
        "warnings": [],
        "benchmark_aligned_sessions": 0,
    }

    # --- absolute direction -------------------------------------------------------
    absolute_return = roc(closes, index, momentum_days)
    if absolute_return is None:
        result["warnings"].append("absolute_return_unavailable")
    else:
        result["return_20d"] = absolute_return
        if abs(absolute_return) <= FLAT_ROC_TOLERANCE:
            result["momentum_direction"] = "flat"
        else:
            result["momentum_direction"] = "positive" if absolute_return > 0 else "negative"

    # --- shared volatility --------------------------------------------------------
    needed = max(volatility_days, momentum_days, regression_points - 1)
    if index < needed:
        for component in components.values():
            _fail(component, "unavailable", "insufficient_history")
        return result

    window_prices = closes[index - volatility_days : index + 1]
    if any(not _finite(value) or value <= 0 for value in window_prices):
        for component in components.values():
            _fail(component, "invalid", "invalid_price")
        return result

    returns = [math.log(window_prices[k] / window_prices[k - 1]) for k in range(1, len(window_prices))]
    sigma = _stdev(returns, ddof=int(config["validation"]["std_ddof"]))
    result["daily_volatility_60d"] = sigma
    result["annual_volatility_60d"] = None if sigma is None else sigma * math.sqrt(int(windows["annual_trading_days"]))

    risk_adjusted_ok = sigma is not None and sigma > MIN_DAILY_VOLATILITY
    if not risk_adjusted_ok:
        reason = "degenerate_volatility" if sigma is not None else "insufficient_history"
        _fail(components["risk_adjusted"], "invalid" if sigma is not None else "unavailable", reason)
        _fail(components["regression_trend"], "invalid" if sigma is not None else "unavailable", reason)
    else:
        log_20 = math.log(closes[index] / closes[index - momentum_days])
        raw_a = log_20 / (sigma * math.sqrt(momentum_days))
        component = components["risk_adjusted"]
        component["raw_value"] = raw_a
        component["normalized_value"] = _normalize(raw_a, float(scales["scale_A"]))
        component["contribution"] = 100.0 * component["weight"] * component["normalized_value"]
        component["status"] = "valid"

        # --- regression trend -----------------------------------------------------
        regression_prices = closes[index - (regression_points - 1) : index + 1]
        if any(not _finite(value) or value <= 0 for value in regression_prices):
            _fail(components["regression_trend"], "invalid", "invalid_price")
        else:
            slope, r_squared, r2_defined = _ols_slope_r2([math.log(value) for value in regression_prices])
            trend = components["regression_trend"]
            trend["beta"] = slope
            trend["r_squared"] = r_squared
            trend["r_squared_defined"] = r2_defined
            if slope is None or r_squared is None:
                _fail(trend, "invalid", "degenerate_regression")
            else:
                if not r2_defined:
                    result["warnings"].append("flat_regression_window_r2_undefined")
                raw_t = (momentum_days * slope / (sigma * math.sqrt(momentum_days))) * r_squared
                trend["raw_value"] = raw_t
                trend["normalized_value"] = _normalize(raw_t, float(scales["scale_T"]))
                trend["contribution"] = 100.0 * trend["weight"] * trend["normalized_value"]
                trend["status"] = "valid"

    # --- relative strength --------------------------------------------------------
    relative = components["relative_strength"]
    if benchmark is None:
        _fail(relative, "unavailable", "benchmark_unavailable")
    else:
        span_dates = dates[index - tracking_days : index + 1]
        missing = [date for date in span_dates if date not in benchmark]
        result["benchmark_aligned_sessions"] = len(span_dates) - len(missing)
        if missing:
            _fail(relative, "unavailable", "benchmark_alignment_incomplete")
        else:
            # span_dates is the contiguous tail of the session axis, so the target
            # prices are the same slice of ``closes``; no positional lookup needed.
            stock_prices = closes[index - tracking_days : index + 1]
            benchmark_prices = [benchmark[date] for date in span_dates]
            excess = [
                math.log(stock_prices[k] / stock_prices[k - 1]) - math.log(benchmark_prices[k] / benchmark_prices[k - 1])
                for k in range(1, len(span_dates))
            ]
            tracking_error = _stdev(excess, ddof=int(config["validation"]["std_ddof"]))
            if tracking_error is None or tracking_error <= MIN_TRACKING_ERROR:
                _fail(relative, "invalid", "degenerate_tracking_error")
            else:
                excess_20 = math.log(closes[index] / closes[index - momentum_days]) - math.log(
                    benchmark[dates[index]] / benchmark[dates[index - momentum_days]]
                )
                raw_r = excess_20 / (tracking_error * math.sqrt(momentum_days))
                relative["raw_value"] = raw_r
                relative["normalized_value"] = _normalize(raw_r, float(scales["scale_R"]))
                relative["contribution"] = 100.0 * relative["weight"] * relative["normalized_value"]
                relative["status"] = "valid"
                if sigma and tracking_error < BENCHMARK_OVERLAP_RATIO * sigma:
                    result["warnings"].append("benchmark_near_identical_low_economic_signal")
    return result


def _assemble(
    evaluation: dict[str, Any], config: dict[str, Any]
) -> tuple[float | None, str, list[str]]:
    """Resolve component statuses into a score, its status, and the reason trail.

    Shared by the report path and the offline point entry so both apply the same
    all-or-nothing rule: a partial score is never produced.
    """
    components = evaluation["components"]
    reasons: list[str] = []
    for name, component in components.items():
        if component["status"] != "valid":
            reasons.append(f"{name}:{component['reason']}")

    statuses = {component["status"] for component in components.values()}
    if "invalid" in statuses:
        score_status = "invalid"
    elif "unavailable" in statuses:
        score_status = "unavailable"
    else:
        score_status = "valid"

    contributions = [component["contribution"] for component in components.values()]
    score: float | None = None
    if score_status == "valid":
        if any(value is None for value in contributions):
            # Defensive: require_all_components forbids partial scores.
            score_status = "invalid"
            reasons.append("incomplete_contributions")
        else:
            score = math.fsum(float(value) for value in contributions)
            low, high = float(config["output"]["score_min"]), float(config["output"]["score_max"])
            if not (low < score < high):  # tanh keeps this strictly inside the band
                score = max(min(score, math.nextafter(high, 0.0)), math.nextafter(low, 0.0))
                reasons.append("score_clamped_to_bounds")
    return score, score_status, reasons


def score_at(
    closes: list[float],
    dates: list[str],
    benchmark: dict[str, float] | None,
    index: int,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single-point factor evaluation for offline studies.

    ``compute`` is the report path (it appends a diagnostic block); this is the
    named entry research code should reuse instead of re-deriving the factor
    math or reaching into ``_evaluate``. No aggregation and no fitting happens
    here: the returned score is the frozen formula applied at ``index``.
    """
    active = DEFAULT_CONFIG if config is None else config
    evaluation = _evaluate(closes, dates, _benchmark_series(benchmark), index, active)
    score, status, reasons = _assemble(evaluation, active)
    return {
        "as_of": dates[index] if dates else None,
        "score": score,
        "status": status,
        "status_reasons": reasons,
        "momentum_direction": evaluation["momentum_direction"],
        "return_20d": evaluation["return_20d"],
        "components": evaluation["components"],
        "warnings": evaluation["warnings"],
    }


def compute(
    closes: list[float],
    dates: list[str],
    benchmark: dict[str, float] | None = None,
    *,
    symbol: str,
    benchmark_symbol: str | None = None,
    benchmark_selection: str = "declared",
    price_field: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the frozen composite momentum block for the final session."""
    active = config or DEFAULT_CONFIG
    index = len(closes) - 1
    evaluation = _evaluate(closes, dates, _benchmark_series(benchmark), index, active)
    components = evaluation["components"]
    score, score_status, reasons = _assemble(evaluation, active)

    weights = [float(active["weights"][key]) for key in ("risk_adjusted", "regression_trend", "relative_strength")]
    if abs(math.fsum(weights) - 1.0) > 1e-12:
        raise ValueError("momentum weights must sum to one")

    return {
        "model_version": active["version"],
        "as_of": dates[index] if dates else None,
        "symbol": symbol,
        "benchmark": {
            "symbol": benchmark_symbol,
            "selection": benchmark_selection,
            "alignment": "exact_date_intersection_no_forward_fill",
            "aligned_sessions": evaluation["benchmark_aligned_sessions"],
        },
        "price_field": price_field,
        "momentum_direction": evaluation["momentum_direction"],
        "return_20d": evaluation["return_20d"],
        "momentum_score_20d": score,
        "score_status": score_status,
        "status_reasons": reasons,
        "calibration_status": active["normalization"]["calibration_status"],
        "interpretation": "feature_not_probability",
        "weights": dict(active["weights"]),
        "windows": dict(active["windows"]),
        "normalization": dict(active["normalization"]),
        "daily_volatility_60d": evaluation.get("daily_volatility_60d"),
        "annual_volatility_60d": evaluation.get("annual_volatility_60d"),
        "components": components,
        "warnings": evaluation["warnings"],
    }


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda position: values[position])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for cursor in range(position, end + 1):
            ranks[order[cursor]] = average
        position = end + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mean_x, mean_y = math.fsum(rx) / len(rx), math.fsum(ry) / len(ry)
    sxy = math.fsum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    sxx = math.fsum((a - mean_x) ** 2 for a in rx)
    syy = math.fsum((b - mean_y) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def rank_ic_diagnostic(
    closes: list[float],
    dates: list[str],
    benchmark: dict[str, float] | None,
    *,
    horizon: int,
    config: dict[str, Any] | None = None,
    minimum_samples: int = 20,
) -> dict[str, Any]:
    """Descriptive rank IC of the composite score and of plain ROC20.

    Every feature at ``t`` uses ``closes[: t + 1]`` only and every label uses a session
    that has already matured at the decision close, so the diagnostic is causal. It is
    still *in-sample and descriptive*: it is not out-of-sample evidence that the
    composite feature predicts better than the baseline.
    """
    active = config or DEFAULT_CONFIG
    benchmark_series = _benchmark_series(benchmark)
    window = int(active["windows"]["momentum_days"])
    rows: list[tuple[int, float | None, float, float]] = []
    for index in range(window, len(closes) - horizon):
        if closes[index + horizon] <= 0 or closes[index] <= 0:
            continue
        label = closes[index + horizon] / closes[index] - 1.0
        baseline = roc(closes, index, window)
        if baseline is None:
            continue
        evaluation = _evaluate(closes, dates, benchmark_series, index, active)
        components = evaluation["components"]
        score = None
        if all(component["status"] == "valid" for component in components.values()):
            score = math.fsum(float(component["contribution"]) for component in components.values())
        rows.append((index, score, baseline, label))

    def _pair(selector: Any) -> tuple[float | None, int]:
        pairs = [(selector(row), row[3]) for row in rows]
        usable = [(a, b) for a, b in pairs if a is not None]
        if len(usable) < minimum_samples:
            return None, len(usable)
        value = _spearman([a for a, _ in usable], [b for _, b in usable])
        return value, len(usable)

    midpoint = len(rows) // 2

    def _split(selector: Any, low: int, high: int) -> float | None:
        pairs = [(selector(row), row[3]) for row in rows[low:high]]
        usable = [(a, b) for a, b in pairs if a is not None]
        if len(usable) < max(3, minimum_samples // 4):
            return None
        return _spearman([a for a, _ in usable], [b for _, b in usable])

    score_ic, score_n = _pair(lambda row: row[1])
    roc_ic, roc_n = _pair(lambda row: row[2])
    return {
        "horizon_sessions": horizon,
        "label": f"forward_return_{horizon}d",
        "scope": "in_sample_descriptive_no_hyperparameter_fitting",
        "incremental_value_claim": "not_established",
        "samples": len(rows),
        "composite_score": {
            "rank_ic": score_ic,
            "samples": score_n,
            "rank_ic_first_half": _split(lambda row: row[1], 0, midpoint),
            "rank_ic_second_half": _split(lambda row: row[1], midpoint, len(rows)),
        },
        "baseline_roc20": {
            "rank_ic": roc_ic,
            "samples": roc_n,
            "rank_ic_first_half": _split(lambda row: row[2], 0, midpoint),
            "rank_ic_second_half": _split(lambda row: row[2], midpoint, len(rows)),
        },
        "warnings": [
            "in_sample_descriptive_only",
            "no_out_of_sample_evidence_of_superiority",
        ],
    }
