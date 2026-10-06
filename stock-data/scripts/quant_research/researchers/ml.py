"""Research C: deterministic logistic baseline with purging and walk-forward evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import market_bars, non_overlapping
from ..math_utils import logistic, mean, sample_std
from ..snapshot import load_snapshot
from ..validation.purged_cv import SampleInterval, causal_walk_forward, count_interval_overlaps, purged_kfold

FEATURE_NAMES = ("return_1", "return_5", "return_20", "return_60", "volatility_20", "volume_z20", "range_position20", "gap_1", "close_location")


@dataclass(frozen=True)
class Observation:
    decision: int
    information_start: int
    label_end: int
    features: tuple[float, ...]
    target: int
    forward_return: float


@dataclass
class LogisticModel:
    means: list[float]
    scales: list[float]
    coefficients: list[float]
    intercept: float

    def score(self, row: Observation | tuple[float, ...]) -> float:
        values = row.features if isinstance(row, Observation) else row
        standardized = [(float(value) - center) / scale for value, center, scale in zip(values, self.means, self.scales)]
        return self.intercept + sum(weight * value for weight, value in zip(self.coefficients, standardized))

    def predict(self, row: Observation | tuple[float, ...]) -> float:
        return logistic(self.score(row))


def _features(bars: list[dict[str, Any]]) -> list[tuple[float, ...] | None]:
    close = [row["close"] for row in bars]
    high = [row["high"] for row in bars]
    low = [row["low"] for row in bars]
    opening = [row["open"] for row in bars]
    volume = [row["volume"] for row in bars]
    result: list[tuple[float, ...] | None] = [None] * len(bars)
    for i in range(60, len(bars)):
        returns = [close[j] / close[j - 1] - 1 for j in range(i - 19, i + 1)]
        v20 = volume[i - 19:i + 1]
        vstd = sample_std(v20)
        highest, lowest = max(high[i - 19:i + 1]), min(low[i - 19:i + 1])
        day_range = high[i] - low[i]
        if min(close[i - 60], close[i - 20], close[i - 5], close[i - 1], close[i - 19]) <= 0:
            continue
        result[i] = (
            close[i] / close[i - 1] - 1,
            close[i] / close[i - 5] - 1,
            close[i] / close[i - 20] - 1,
            close[i] / close[i - 60] - 1,
            sample_std(returns) or 0.0,
            (volume[i] - sum(v20) / len(v20)) / vstd if vstd else 0.0,
            (close[i] - lowest) / (highest - lowest) if highest > lowest else 0.5,
            opening[i] / close[i - 1] - 1,
            (2 * close[i] - high[i] - low[i]) / day_range if day_range else 0.0,
        )
    return result


def _fit(rows: list[Observation], *, iterations: int = 500, learning_rate: float = 0.12, l2: float = 1e-3) -> LogisticModel:
    if len(rows) < 30 or len({row.target for row in rows}) < 2:
        raise ValueError("logistic fit requires at least 30 observations from both classes")
    width = len(rows[0].features)
    means = [sum(row.features[j] for row in rows) / len(rows) for j in range(width)]
    scales = []
    for j in range(width):
        value = sample_std([row.features[j] for row in rows])
        scales.append(value if value and value > 1e-12 else 1.0)
    matrix = [[(row.features[j] - means[j]) / scales[j] for j in range(width)] for row in rows]
    coefficients = [0.0] * width
    intercept = math.log((sum(row.target for row in rows) + 0.5) / (len(rows) - sum(row.target for row in rows) + 0.5))
    for _ in range(iterations):
        grad = [0.0] * width
        grad_intercept = 0.0
        for values, row in zip(matrix, rows):
            probability = logistic(intercept + sum(w * x for w, x in zip(coefficients, values)))
            residual = probability - row.target
            grad_intercept += residual
            for j, value in enumerate(values):
                grad[j] += residual * value
        count = len(rows)
        intercept -= learning_rate * grad_intercept / count
        coefficients = [weight - learning_rate * (grad[j] / count + l2 * weight) for j, weight in enumerate(coefficients)]
    return LogisticModel(means, scales, coefficients, intercept)


def _fit_platt(scores: list[float], targets: list[int]) -> tuple[float, float] | None:
    if len(scores) < 40 or len(set(targets)) < 2:
        return None
    slope, intercept = 1.0, 0.0
    for _ in range(500):
        grad_slope = grad_intercept = 0.0
        for score, target in zip(scores, targets):
            residual = logistic(intercept + slope * score) - target
            grad_slope += residual * score
            grad_intercept += residual
        n = len(scores)
        slope -= 0.05 * (grad_slope / n + 1e-3 * slope)
        intercept -= 0.05 * grad_intercept / n
    return slope, intercept


def _auc(pairs: list[tuple[float, int]]) -> float | None:
    positives = [probability for probability, target in pairs if target == 1]
    negatives = [probability for probability, target in pairs if target == 0]
    if not positives or not negatives:
        return None
    favorable = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in positives for n in negatives)
    return favorable / (len(positives) * len(negatives))


def _metrics(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    return {
        "observations": len(pairs),
        "brier": mean([(p - y) ** 2 for p, y in pairs]),
        "auc": _auc(pairs),
        "observed_positive_rate": mean([float(y) for _, y in pairs]),
    }


def run(snapshot_dir: Path) -> dict[str, Any]:
    manifest, market = load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = HORIZONS[horizon_name]
    bars = market_bars(market)
    closes = [row["close"] for row in bars]
    features = _features(bars)
    n = len(bars)
    holdout_start = int(n * 0.8)
    observations: list[Observation] = []
    for i, vector in enumerate(features):
        if vector is None or i + horizon >= n:
            continue
        target_return = closes[i + horizon] / closes[i] - 1
        observations.append(Observation(i, max(0, i - 60), i + horizon, vector, int(target_return > 0), target_return))

    train_rows = [row for row in observations if row.decision < holdout_start and row.label_end < max(0, holdout_start - 60)]
    holdout_rows = [row for row in observations if row.decision >= holdout_start]
    if len(train_rows) < 160 or len({row.target for row in train_rows}) < 2:
        return researcher_result("ml", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", warnings=[f"The pre-holdout training sample has {len(train_rows)} matured rows; the native logistic baseline requires at least 160 and both labels."])

    intervals = [SampleInterval(row.decision, row.information_start, row.label_end) for row in train_rows]
    purged_folds = purged_kfold(intervals, n_splits=5, embargo_sessions=horizon)
    cv_prediction_rows: list[tuple[Observation, float]] = []
    purged_count, embargo_count, overlap_count = 0, 0, 0
    for fold in purged_folds:
        train = [train_rows[i] for i in fold.train]
        test = [train_rows[i] for i in fold.test]
        purged_count += fold.purged
        embargo_count += fold.embargoed
        overlap_count += count_interval_overlaps(intervals, fold.train, fold.test)
        if len(train) < 100 or len({row.target for row in train}) < 2:
            continue
        try:
            model = _fit(train)
        except ValueError:
            continue
        score_decisions = set(non_overlapping([row.decision for row in test], horizon))
        cv_prediction_rows.extend((row, model.predict(row)) for row in test if row.decision in score_decisions)

    cv_prediction_rows.sort(key=lambda pair: pair[0].decision)
    cv_decisions = set(non_overlapping([row.decision for row, _ in cv_prediction_rows], horizon))
    cv_predictions = [(prediction, row.target) for row, prediction in cv_prediction_rows if row.decision in cv_decisions]

    wf_folds = causal_walk_forward(intervals, n_splits=4, minimum_train=100, pre_test_gap_sessions=horizon)
    wf_predictions: list[tuple[Observation, float]] = []
    wf_purged, wf_gap = 0, 0
    for fold in wf_folds:
        train = [train_rows[i] for i in fold.train]
        test = [train_rows[i] for i in fold.test]
        wf_purged += fold.purged
        wf_gap += fold.embargoed
        if len({row.target for row in train}) < 2:
            continue
        try:
            model = _fit(train)
        except ValueError:
            continue
        wf_predictions.extend((row, model.predict(row)) for row in test)

    wf_predictions.sort(key=lambda pair: pair[0].decision)
    wf_decisions = set(non_overlapping([row.decision for row, _ in wf_predictions], horizon))
    wf_predictions = [(row, prediction) for row, prediction in wf_predictions if row.decision in wf_decisions]
    calibration = _fit_platt([math.log(max(1e-6, min(1 - 1e-6, p)) / (1 - max(1e-6, min(1 - 1e-6, p)))) for _, p in wf_predictions], [row.target for row, _ in wf_predictions])
    holdout_model = _fit(train_rows)
    holdout_pairs: list[tuple[float, int]] = []
    holdout_decisions = set(non_overlapping([row.decision for row in holdout_rows], horizon))
    for row in holdout_rows:
        if row.decision not in holdout_decisions:
            continue
        raw_probability = holdout_model.predict(row)
        probability = logistic(calibration[1] + calibration[0] * holdout_model.score(row)) if calibration else raw_probability
        holdout_pairs.append((probability, row.target))

    # No final-holdout metric is used to select features or hyperparameters. Once its
    # one-time report is fixed, refit the frozen baseline on all matured labels for today.
    current_rows = [row for row in observations if row.label_end <= n - 1]
    current_model = _fit(current_rows) if len(current_rows) >= 160 and len({row.target for row in current_rows}) == 2 else holdout_model
    current_vector = features[-1] if features else None
    raw_current = current_model.predict(current_vector) if current_vector is not None else None
    if raw_current is None:
        status = "insufficient_data"
        probability = None
        warnings = ["The latest OHLCV row does not have a complete feature vector."]
    elif calibration:
        probability = logistic(calibration[1] + calibration[0] * current_model.score(current_vector))
        status = "success" if cv_predictions and wf_predictions and holdout_rows else "partial"
        warnings = []
    else:
        probability = raw_current
        status = "partial"
        warnings = ["Too few valid causal walk-forward predictions were available for Platt calibration; probability is the raw logistic predict_proba output."]
    warnings.append("XGBoost/LightGBM are not part of this native baseline; the ML path currently uses deterministic L2 logistic regression only.")
    warnings.append("Returns are gross close-to-close observations; this model path does not model execution, spreads, slippage or fees.")
    if status == "success":
        status = "partial"  # the planned boosted-model comparison is not implemented in this MVP
    if len(holdout_pairs) < 20:
        warnings.append("The chronological holdout has fewer than 20 predictions; its metrics are highly uncertain.")
    if overlap_count != 0:
        status = "invalid"
        probability = None
        warnings.append("Purged CV retained overlapping train/test information intervals; ML result vetoed.")

    holdout_metrics = _metrics(holdout_pairs)
    train_prevalence = mean([float(row.target) for row in train_rows])
    holdout_metrics["training_base_rate_probability"] = train_prevalence
    holdout_metrics["base_rate_brier"] = mean([(train_prevalence - y) ** 2 for _, y in holdout_pairs]) if train_prevalence is not None else None
    validation = {
        "model": "native deterministic L2 logistic regression",
        "features": list(FEATURE_NAMES),
        "random_seed": 0,
        "feature_availability": "close[t] and OHLCV through decision session t; target begins after t",
        "leakage_audit": {"features_use_data_through_decision_close_only": True, "target_excluded_from_features": True, "fold_local_scaling": True, "purge_uses_closed_information_intervals": True, "causal_walk_forward": True, "chronological_holdout": True},
        "label": f"close[t+{horizon}] / close[t] - 1 > 0",
        "purged_kfold": {**_metrics(cv_predictions), "folds": len(purged_folds), "purged_train_rows": purged_count, "embargoed_train_rows": embargo_count, "retained_interval_overlaps": overlap_count},
        "causal_walk_forward": {**_metrics([(p, row.target) for row, p in wf_predictions]), "folds": len(wf_folds), "purged_train_rows": wf_purged, "pre_test_gap_rows": wf_gap},
        "probability_calibration": {"method": "Platt scaling on causal walk-forward logits" if calibration else "none", "calibrated": bool(calibration), "calibration_observations": len(wf_predictions)},
        "final_holdout": {**holdout_metrics, "start_session": bars[holdout_start]["date"] if holdout_start < n else None, "one_time_chronological_holdout": True},
        "current_prediction_model_fit_rows": len(current_rows),
        "accelerated_models": {"xgboost": "unavailable", "lightgbm": "unavailable"},
    }
    predicted_return = mean([row.forward_return for row in current_rows if row.target == 1])
    down_return = mean([row.forward_return for row in current_rows if row.target == 0])
    expected_return = probability * predicted_return + (1 - probability) * down_return if probability is not None and predicted_return is not None and down_return is not None else None
    evidence = {"positive": [{"family": "momentum", "features": list(FEATURE_NAMES), "model_probability": probability}] if probability is not None and probability >= 0.5 else [], "negative": [{"family": "momentum", "features": list(FEATURE_NAMES), "model_probability": probability}] if probability is not None and probability < 0.5 else []}
    return researcher_result("ml", ticker, horizon_name, manifest["snapshot_id"], status=status, prob_up=probability, expected_return=expected_return, evidence=evidence, validation=validation, data_used=["daily raw OHLCV"], warnings=warnings, probability_source="logistic_predict_proba_platt_calibrated" if calibration and probability is not None else "logistic_predict_proba_uncalibrated" if probability is not None else None)
