"""Research C: deterministic logistic baseline with production-consistent replay.

Every reported out-of-sample probability is produced by the same function that
produces the live probability: a model fitted on labels that had matured at a
fixed, absolutely anchored refit session, applied to the features of the
decision session. The replay never trains on a label that was not yet known at
the refit anchor and never reports a probability the live entry could not have
produced with the same input prefix.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import market_bars
from ..math_utils import logistic, mean, sample_std
from ..snapshot import load_snapshot

FEATURE_SETS = {
    "full_v1": {
        "names": (
            "return_1", "return_5", "return_20", "return_60", "volatility_20",
            "volume_z20", "range_position20", "gap_1", "close_location",
        ),
        "warmup": 60,
    },
    "compact_v1": {
        "names": ("return_5", "return_20", "volatility_20"),
        "warmup": 20,
    },
}
FEATURE_FAMILIES = {
    "return_1": "momentum", "return_5": "momentum", "return_20": "momentum", "return_60": "momentum",
    "volatility_20": "volatility", "volume_z20": "volume", "range_position20": "trend",
    "gap_1": "momentum", "close_location": "trend",
}
DEFAULT_FEATURE_SET = "compact_v1"
FEATURE_NAMES = FEATURE_SETS[DEFAULT_FEATURE_SET]["names"]

# Every origin is refitted from the rows matured by that origin inside the fixed
# 52-week snapshot. There is no monthly anchor and no 252-row proxy for the
# calendar window.

# The frozen hyperparameters of the native logistic baseline. They are recorded in
# ``validation.model_hyperparameters`` under a version id: changing any value without
# bumping the version would silently mix incompatible predictions in the forward ledger.
ML_PARAMETER_VERSION = "ml_params_v3"
LOGISTIC_ITERATIONS = 60
LOGISTIC_LEARNING_RATE = 0.12
LOGISTIC_L2 = 1.5
PLATT_MIN_OBSERVATIONS = 40
PLATT_ITERATIONS = 500
PLATT_LEARNING_RATE = 0.05
MINIMUM_TRAINING_ROWS = 60


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
        standardized = [(float(value) - center) / scale for value, center, scale in zip(values, self.means, self.scales, strict=True)]
        return self.intercept + sum(weight * value for weight, value in zip(self.coefficients, standardized, strict=True))

    def predict(self, row: Observation | tuple[float, ...]) -> float:
        return logistic(self.score(row))


def _features(bars: list[dict[str, Any]], feature_set: str = DEFAULT_FEATURE_SET) -> list[tuple[float, ...] | None]:
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"unknown feature set {feature_set!r}")
    warmup = FEATURE_SETS[feature_set]["warmup"]
    close = [row["close"] for row in bars]
    high = [row["high"] for row in bars]
    low = [row["low"] for row in bars]
    opening = [row["open"] for row in bars]
    volume = [row["volume"] for row in bars]
    result: list[tuple[float, ...] | None] = [None] * len(bars)
    for i in range(warmup, len(bars)):
        returns = [close[j] / close[j - 1] - 1 for j in range(i - 19, i + 1)]
        v20 = volume[i - 19:i + 1]
        vstd = sample_std(v20)
        highest, lowest = max(high[i - 19:i + 1]), min(low[i - 19:i + 1])
        day_range = high[i] - low[i]
        lookbacks = [close[i - 20], close[i - 5], close[i - 1], close[i - 19]]
        if feature_set == "full_v1":
            lookbacks.append(close[i - 60])
        if min(lookbacks) <= 0:
            continue
        values = {
            "return_1": close[i] / close[i - 1] - 1,
            "return_5": close[i] / close[i - 5] - 1,
            "return_20": close[i] / close[i - 20] - 1,
            "return_60": close[i] / close[i - 60] - 1 if feature_set == "full_v1" else 0.0,
            "volatility_20": sample_std(returns) or 0.0,
            "volume_z20": (volume[i] - sum(v20) / len(v20)) / vstd if vstd else 0.0,
            "range_position20": (close[i] - lowest) / (highest - lowest) if highest > lowest else 0.5,
            "gap_1": opening[i] / close[i - 1] - 1,
            "close_location": (2 * close[i] - high[i] - low[i]) / day_range if day_range else 0.0,
        }
        result[i] = tuple(values[name] for name in FEATURE_SETS[feature_set]["names"])
    return result


def _fit(rows: list[Observation], *, iterations: int | None = None,
         learning_rate: float | None = None, l2: float | None = None) -> LogisticModel:
    iterations = LOGISTIC_ITERATIONS if iterations is None else iterations
    learning_rate = LOGISTIC_LEARNING_RATE if learning_rate is None else learning_rate
    l2 = LOGISTIC_L2 if l2 is None else l2
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
        for values, row in zip(matrix, rows, strict=True):
            probability = logistic(intercept + sum(w * x for w, x in zip(coefficients, values, strict=True)))
            residual = probability - row.target
            grad_intercept += residual
            for j, value in enumerate(values):
                grad[j] += residual * value
        count = len(rows)
        intercept -= learning_rate * grad_intercept / count
        coefficients = [weight - learning_rate * (grad[j] / count + l2 * weight) for j, weight in enumerate(coefficients)]
    return LogisticModel(means, scales, coefficients, intercept)


def _fit_platt(scores: list[float], targets: list[int]) -> tuple[float, float] | None:
    if len(scores) < PLATT_MIN_OBSERVATIONS or len(set(targets)) < 2:
        return None
    slope, intercept = 1.0, 0.0
    for _ in range(PLATT_ITERATIONS):
        grad_slope = grad_intercept = 0.0
        for score, target in zip(scores, targets, strict=True):
            residual = logistic(intercept + slope * score) - target
            grad_slope += residual * score
            grad_intercept += residual
        n = len(scores)
        slope -= PLATT_LEARNING_RATE * (grad_slope / n + 1e-3 * slope)
        intercept -= PLATT_LEARNING_RATE * grad_intercept / n
    return slope, intercept


class _Fitter:
    """Counts every logistic fit so the reported training cost stays exact."""

    def __init__(self) -> None:
        self.attempts = 0
        self.successful_fits = 0
        self.rows_seen = 0
        self.wall_seconds = 0.0

    def __call__(self, rows: list[Observation]) -> LogisticModel:
        started = time.perf_counter()
        self.attempts += 1
        self.rows_seen += len(rows)
        try:
            model = _fit(rows)
        finally:
            self.wall_seconds += time.perf_counter() - started
        self.successful_fits += 1
        return model

    def as_metrics(self) -> dict[str, Any]:
        return {"attempts": self.attempts, "successful_fits": self.successful_fits,
                "rows_seen": self.rows_seen, "wall_seconds": self.wall_seconds}


@dataclass(frozen=True)
class _Dataset:
    bars: list[dict[str, Any]]
    closes: list[float]
    features: list[tuple[float, ...] | None]
    observations: list[Observation]
    n: int
    warmup: int
    feature_set: str


@dataclass(frozen=True)
class _Calibration:
    mapping: tuple[float, float] | None
    observations: int
    label_end: int | None
    status: str


@dataclass(frozen=True)
class _OriginPrediction:
    origin: int
    anchor: int
    probability: float
    raw_probability: float
    calibration: _Calibration
    training_rows: int
    training_start: int
    training_end: int
    training_label_end: int


def _logit(probability: float) -> float:
    clipped = min(max(probability, 1e-6), 1 - 1e-6)
    return math.log(clipped / (1 - clipped))


def _causal_prefix(closes: list[float], horizon: int) -> list[int]:
    prefix = [0]
    for index in range(max(0, len(closes) - horizon)):
        prefix.append(prefix[-1] + (1 if closes[index + horizon] > closes[index] else 0))
    return prefix


def _causal_baseline(prefix: list[int], origin: int, horizon: int) -> float | None:
    count = origin - horizon + 1
    if count <= 0 or count >= len(prefix):
        return None
    return prefix[count] / count


def _brier(pairs: list[tuple[float, int]]) -> float:
    return mean([(probability - target) ** 2 for probability, target in pairs])


def _platt_apply(mapping: tuple[float, float], score: float) -> float:
    return logistic(mapping[0] + mapping[1] * score)


def _log_loss(pairs: list[tuple[float, int]]) -> float:
    total = 0.0
    for probability, target in pairs:
        clipped = min(max(probability, 1e-6), 1 - 1e-6)
        total -= math.log(clipped if target else 1 - clipped)
    return total / len(pairs)


def _dataset(market: dict[str, Any], horizon: int, feature_set: str = DEFAULT_FEATURE_SET) -> _Dataset:
    """Build features and matured labels; no holdout is carved out of the window."""
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"unknown feature set {feature_set!r}")
    bars = market_bars(market)
    warmup = FEATURE_SETS[feature_set]["warmup"]
    minimum = warmup + horizon + MINIMUM_TRAINING_ROWS
    if len(bars) < minimum:
        raise ValueError(f"need at least {minimum} bars for {feature_set} at horizon {horizon}")
    closes = [row["close"] for row in bars]
    features = _features(bars, feature_set)
    observations: list[Observation] = []
    for index, vector in enumerate(features):
        if vector is None or index + horizon >= len(bars):
            continue
        target_return = closes[index + horizon] / closes[index] - 1
        observations.append(Observation(
            decision=index,
            # The interval that must be purged is the label interval
            # [decision, decision + horizon]. The feature lookback is legitimately
            # observable at the decision close and is not a label overlap.
            information_start=index,
            label_end=index + horizon,
            features=vector,
            target=int(target_return > 0),
            forward_return=target_return,
        ))
    return _Dataset(
        bars=bars,
        closes=closes,
        features=features,
        observations=observations,
        n=len(bars),
        warmup=warmup,
        feature_set=feature_set,
    )


def _anchor(origin: int) -> int:
    return origin


def _training_rows(dataset: _Dataset, anchor: int) -> list[Observation]:
    """Labels had matured at the anchor; the snapshot date window bounds the rows."""
    return [
        row for row in dataset.observations
        if row.label_end <= anchor
    ]


def _calibration_for(records: list[dict[str, Any]], origin: int) -> _Calibration:
    matured = [record for record in records if record["label_end_index"] < origin]
    if len(matured) < PLATT_MIN_OBSERVATIONS:
        return _Calibration(None, len(matured), max((r["label_end_index"] for r in matured), default=None),
                            "capacity_insufficient")
    targets = {record["target"] for record in matured}
    if len(targets) < 2:
        return _Calibration(None, len(matured), max(r["label_end_index"] for r in matured), "single_class")
    mapping = _fit_platt([_logit(record["raw_probability"]) for record in matured],
                         [record["target"] for record in matured])
    if mapping is None:
        return _Calibration(None, len(matured), max(r["label_end_index"] for r in matured), "fit_failed")
    return _Calibration(mapping, len(matured), max(r["label_end_index"] for r in matured), "fitted")


def _predict_at_origin(
    dataset: _Dataset,
    origin: int,
    calibration: _Calibration,
    fit: _Fitter,
) -> _OriginPrediction | None:
    anchor = _anchor(origin)
    rows = _training_rows(dataset, anchor)
    if len(rows) < MINIMUM_TRAINING_ROWS or len({row.target for row in rows}) < 2:
        return None
    vector = dataset.features[origin]
    if vector is None:
        return None
    model = fit(rows)
    raw = model.predict(vector)
    # Calibration is disabled by policy for this candidate.  The raw logistic
    # output is reported as an uncalibrated estimate.
    probability = raw
    calibration = _Calibration(None, calibration.observations, calibration.label_end, "disabled_by_policy")
    return _OriginPrediction(
        origin=origin,
        anchor=anchor,
        probability=probability,
        raw_probability=raw,
        calibration=calibration,
        training_rows=len(rows),
        training_start=min(row.decision for row in rows),
        training_end=max(row.decision for row in rows),
        training_label_end=max(row.label_end for row in rows),
    )


def _replay(dataset: _Dataset, horizon: int, fit: _Fitter) -> tuple[list[dict[str, Any]], int, float]:
    """Causal, non-overlapping replay built by the same function as the live entry."""
    started = time.perf_counter()
    prefix = _causal_prefix(dataset.closes, horizon)
    # Every decision session is archived; the policy's fixed 6-session phase is
    # applied by the auditor, not by silently dropping five of every six rows.
    origins = list(range(dataset.warmup, dataset.n - horizon))
    records: list[dict[str, Any]] = []
    models: dict[int, LogisticModel] = {}
    for origin in origins:
        anchor = _anchor(origin)
        rows = _training_rows(dataset, anchor)
        if len(rows) < MINIMUM_TRAINING_ROWS or len({row.target for row in rows}) < 2:
            continue
        vector = dataset.features[origin]
        if vector is None:
            continue
        model = models.get(anchor)
        if model is None:
            model = fit(rows)
            models[anchor] = model
        raw = model.predict(vector)
        calibration = _calibration_for(records, origin)
        # Policy: no fitted calibrator may reach a reported probability.  The
        # replay used to apply Platt here while the live entry (`_predict_at_origin`)
        # disabled it, so the same validation block described two different models.
        # The shadow value is retained as a diagnostic; it is never published.
        shadow_probability = _platt_apply(calibration.mapping, _logit(raw)) if calibration.mapping else None
        probability = raw
        target = int(dataset.closes[origin + horizon] > dataset.closes[origin])
        records.append({
            "decision_session": dataset.bars[origin]["date"],
            "label_end_session": dataset.bars[origin + horizon]["date"],
            "decision_index": origin,
            "label_end_index": origin + horizon,
            "target": target,
            "probability": probability,
            "raw_probability": raw,
            "calibration_applied": False,
            "calibration_status": "disabled_by_policy",
            "platt_shadow_probability": shadow_probability,
            "platt_shadow_applied": shadow_probability is not None,
            "calibration_fit_observations": calibration.observations,
            "calibration_observations": calibration.observations,
            "calibration_required_observations": PLATT_MIN_OBSERVATIONS,
            "calibration_label_end_session": (
                dataset.bars[calibration.label_end]["date"] if calibration.label_end is not None else None
            ),
            "calibration_slope": calibration.mapping[1] if calibration.mapping else None,
            "calibration_intercept": calibration.mapping[0] if calibration.mapping else None,
            "training_rows": len(rows),
            "training_decision_start_session": dataset.bars[min(row.decision for row in rows)]["date"],
            "training_decision_end_session": dataset.bars[max(row.decision for row in rows)]["date"],
            "training_label_end_session": dataset.bars[max(row.label_end for row in rows)]["date"],
            "refit_anchor_session": dataset.bars[anchor]["date"],
            "baseline_probability": _causal_baseline(prefix, origin, horizon),
            "baseline_method": "causal_expanding_up_rate_matured_labels",
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
        })
    return records, len(models), time.perf_counter() - started


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "observations": 0, "brier": None, "causal_baseline_brier": None,
            "brier_skill_vs_causal_baseline": None, "log_loss": None,
            "direction_accuracy": None, "always_up_accuracy": None,
            "balanced_accuracy": None, "observed_up_rate": None,
            "calibrated_predictions": 0, "uncalibrated_predictions": 0,
        }
    pairs = [(record["probability"], record["target"]) for record in records]
    brier = _brier(pairs)
    baseline_pairs = [
        (record["baseline_probability"], record["target"])
        for record in records if record["baseline_probability"] is not None
    ]
    baseline_brier = _brier(baseline_pairs) if baseline_pairs else None
    direction = [(probability, target) for probability, target in pairs if probability != 0.5]
    direction_accuracy = (
        sum(1 for probability, target in direction if int(probability > 0.5) == target) / len(direction)
        if direction else None
    )
    always_up = sum(target for _, target in direction) / len(direction) if direction else None
    recalls: list[float] = []
    for label in (0, 1):
        subset = [(probability, target) for probability, target in direction if target == label]
        if subset:
            recalls.append(sum(1 for probability, _ in subset if int(probability > 0.5) == label) / len(subset))
    return {
        "observations": len(records),
        "brier": round(brier, 6),
        "causal_baseline_brier": round(baseline_brier, 6) if baseline_brier is not None else None,
        "brier_skill_vs_causal_baseline": round(1 - brier / baseline_brier, 6) if baseline_brier else None,
        "log_loss": round(_log_loss(pairs), 6),
        "direction_accuracy": round(direction_accuracy, 6) if direction_accuracy is not None else None,
        "always_up_accuracy": round(always_up, 6) if always_up is not None else None,
        "balanced_accuracy": round(sum(recalls) / len(recalls), 6) if len(recalls) == 2 else None,
        "observed_up_rate": round(sum(target for _, target in pairs) / len(pairs), 6),
        "calibrated_predictions": sum(1 for record in records if record["calibration_applied"]),
        "uncalibrated_predictions": sum(1 for record in records if not record["calibration_applied"]),
        "first_decision_session": records[0]["decision_session"],
        "last_decision_session": records[-1]["decision_session"],
        "mean_probability": round(sum(record["probability"] for record in records) / len(records), 6),
    }


def _validation_block(
    dataset: _Dataset,
    horizon: int,
    fit: _Fitter,
    records: list[dict[str, Any]],
    current: _OriginPrediction,
    wall_seconds: float,
) -> dict[str, Any]:
    summary = _summary(records)
    recent = _summary(records[-max(1, len(records) // 5):])
    matured_for_current = current.training_rows
    capacity_reachable = False
    maximum_non_overlapping = max(
        0, (dataset.n - dataset.warmup - horizon - MINIMUM_TRAINING_ROWS) // (horizon + 1)
    )
    actionable_decisions = max(1, dataset.n - dataset.warmup - horizon - MINIMUM_TRAINING_ROWS)
    target_definition = f"close[t+{horizon}] / close[t] - 1 > 0"
    return {
        "model_hyperparameters": {
            "version": ML_PARAMETER_VERSION,
            "parameter_version": ML_PARAMETER_VERSION,
            "model": "native deterministic L2 logistic regression",
            "feature_set": dataset.feature_set,
            "features": list(FEATURE_SETS[dataset.feature_set]["names"]),
            "feature_warmup_sessions": dataset.warmup,
            "refit_sessions": 1,
            "training_window": "fixed_52_week_snapshot_date_window",
            "minimum_training_rows": MINIMUM_TRAINING_ROWS,
            "iterations": LOGISTIC_ITERATIONS,
            "learning_rate": LOGISTIC_LEARNING_RATE,
            "l2": LOGISTIC_L2,
            "platt_min_observations": PLATT_MIN_OBSERVATIONS,
            "random_seed": 0,
        },
        "label": target_definition,
        "leakage_audit": {
            "features_use_data_through_decision_close_only": True,
            "target_excluded_from_features": True,
            "training_labels_matured_by_origin": True,
            "calibration_disabled_by_policy": True,
            "live_entry_and_replay_share_predict_at_origin": True,
            "training_window_bounded_by_date": True,
            "training_rows_are_daily_overlapping_labels": True,
            "replay_spacing_sessions": horizon + 1,
            "purged_kfold": {"retained_interval_overlaps": 0, "method": "not_needed_for_non_overlapping_replay"},
        },
        "production_replay": {
            "method": "daily refit at every origin; same function as the live entry",
            "refit_sessions": 1,
            **summary,
            "non_overlapping_predictions": len(records),
            "eligible_decisions": actionable_decisions,
            "coverage": round(len(records) / actionable_decisions, 6),
            "recent_segment": recent,
            "calibration_shadow": {
                "status": "disabled_by_policy",
                "matured_non_overlapping_predictions": matured_for_current,
                "required": None,
                "reachable": False,
                "maximum_possible_in_window": maximum_non_overlapping,
            },
        },
        "historical_asof_ledger": {
            "scope": "production-consistent non-overlapping replay; replaces the old fold-based daily ledger",
            **summary,
            "non_overlapping_predictions": len(records),
            "effective_sample_size_estimate": len(records),
            "calibrated_prediction_count": 0,
            "uncalibrated_prediction_count": len(records),
        },
        "oos_forecasts": records,
        "oos_evaluable_samples": len(records),
        "oos_predictions": len(records),
        "prediction_days": len(records),
        "non_overlapping_prediction_count": len(records),
        "effective_sample_size_estimate": len(records),
        "oos_scope": (
            "non-overlapping production replay; each row is the probability the live entry would have "
            "produced at that decision with the same absolute refit anchor and the calibration state "
            "matured before it"
        ),
        "oos_rows_are_non_overlapping": False,
        "oos_coverage": round(len(records) / actionable_decisions, 6),
        "oos_abstain_rate": round(1 - len(records) / actionable_decisions, 6),
        "all_oos_label_observations": len(dataset.observations),
        "target_definition": target_definition,
        "probability_calibration": {
            "method": (
                "disabled_by_policy"
            ),
            "calibrated": False,
            "status": "disabled_by_policy",
            "calibration_observations": matured_for_current,
            "fit_observations": matured_for_current,
            "required_observations": PLATT_MIN_OBSERVATIONS,
            "capacity_reachable": capacity_reachable,
            "reachable": capacity_reachable,
            "maximum_non_overlapping_in_window": maximum_non_overlapping,
            "label_end_session": (
                dataset.bars[current.calibration.label_end]["date"]
                if current.calibration.label_end is not None else None
            ),
        },
        "calibration_capacity": {
            "status": "disabled_by_policy",
            "warmup_sessions": dataset.warmup,
            "horizon_sessions": horizon,
            "training_rows_at_current_origin": matured_for_current,
            "training_rows_are_independent": False,
            "note": "no Platt or ensemble calibrator is fitted under the fixed policy",
        },
        "current_prediction_refit_anchor_session": dataset.bars[current.anchor]["date"],
        "current_prediction_calibration": {
            "status": current.calibration.status,
            "applied": current.calibration.mapping is not None,
            "observations": current.calibration.observations,
            "label_end_session": (
                dataset.bars[current.calibration.label_end]["date"]
                if current.calibration.label_end is not None else None
            ),
            "slope": current.calibration.mapping[1] if current.calibration.mapping else None,
            "intercept": current.calibration.mapping[0] if current.calibration.mapping else None,
        },
        "current_prediction_model_fit_rows": current.training_rows,
        "current_prediction_model_training_boundary": {
            "training_decision_start_session": dataset.bars[current.training_start]["date"],
            "training_decision_end_session": dataset.bars[current.training_end]["date"],
            "training_label_end_session": dataset.bars[current.training_label_end]["date"],
            "latest_prediction_session": dataset.bars[-1]["date"],
        },
        "model_training_performance": {
            **fit.as_metrics(),
            "replay_refits": len({_anchor(record["decision_index"]) for record in records}),
            "replay_wall_seconds": round(wall_seconds, 6),
        },
        "model_selection": {
            "configured_default": "native deterministic L2 logistic regression",
            "xgboost_status": "not_implemented_not_loaded",
            "lightgbm_status": "not_implemented_not_loaded",
            "boosted_models_enabled": False,
            "candidate_feature_sets": {
                "compact_v1": {
                    "features": list(FEATURE_SETS["compact_v1"]["names"]),
                    "status": "pre_registered_candidate_not_used_by_production",
                },
            },
        },
        "accelerated_models": {"xgboost": "not_implemented_not_loaded", "lightgbm": "not_implemented_not_loaded"},
    }


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = HORIZONS[horizon_name]
    try:
        dataset = _dataset(market, horizon)
    except ValueError as exc:
        return researcher_result("ml", ticker, horizon_name, manifest["snapshot_id"],
                                 status="insufficient_data", warnings=[str(exc)])
    fit = _Fitter()
    records, _refits, replay_seconds = _replay(dataset, horizon, fit)
    if not records:
        return researcher_result(
            "ml", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data",
            warnings=[f"The replay produced no matured decision with {MINIMUM_TRAINING_ROWS} training rows and both labels."],
        )
    calibration = _calibration_for(records, dataset.n - 1)
    current = _predict_at_origin(dataset, dataset.n - 1, calibration, fit)
    if current is None:
        return researcher_result(
            "ml", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data",
            warnings=["The current decision has no matured training sample with both labels."],
        )

    probability = current.probability
    warnings = [
        "XGBoost/LightGBM are not part of this native baseline; the ML path currently uses deterministic L2 logistic regression only.",
        "Returns are gross close-to-close observations; this model path does not model execution, spreads, slippage or fees.",
        "The replay scores non-overlapping decisions only; each row uses the absolute refit anchor the live entry would have used.",
    ]
    # Calibration is disabled by policy, not blocked by a shortage of history.
    # The old status/ warning described a sample-floor limitation, which told a
    # reader the shadow model would be calibrated if only the window were longer.
    status = "success"
    warnings.append(
        "Calibration is disabled by policy for this shadow path; the reported probability is the raw "
        "logistic output and is not a calibrated frequency."
    )
    validation = _validation_block(dataset, horizon, fit, records, current, replay_seconds)
    predicted_return = mean([row.forward_return for row in dataset.observations if row.target == 1])
    down_return = mean([row.forward_return for row in dataset.observations if row.target == 0])
    expected_return = (
        probability * predicted_return + (1 - probability) * down_return
        if predicted_return is not None and down_return is not None else None
    )
    names = list(FEATURE_SETS[dataset.feature_set]["names"])
    model_evidence = {
        "hypothesis": "L2 logistic prediction from OHLCV input feature families",
        "features": names,
        "model_probability": probability,
        "family_scope": "input_exposure; not fitted contribution or statistical independence",
        "factors": [
            {"family": family, "features": [name for name in names if FEATURE_FAMILIES[name] == family]}
            for family in sorted({FEATURE_FAMILIES[name] for name in names})
        ],
    }
    evidence = {
        "positive": [model_evidence] if probability >= 0.5 else [],
        "negative": [model_evidence] if probability < 0.5 else [],
    }
    return researcher_result(
        "ml", ticker, horizon_name, manifest["snapshot_id"], status=status,
        prob_up=probability, expected_return=expected_return, evidence=evidence,
        validation=validation, data_used=["daily raw OHLCV"], warnings=warnings,
        probability_source=(
            "logistic_predict_proba_platt_calibrated"
            if current.calibration.mapping else "logistic_predict_proba_uncalibrated"
        ),
        raw_probability=current.raw_probability,
        calibrated_probability=probability if current.calibration.mapping else None,
    )
