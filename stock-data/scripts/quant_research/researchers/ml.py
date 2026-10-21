"""Research C: deterministic logistic baseline with purging and walk-forward evidence."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import HORIZONS, researcher_result
from ..market_data import market_bars, non_overlapping
from ..math_utils import logistic, mean, sample_std
from ..snapshot import load_snapshot
from ..validation.purged_cv import (
    SampleInterval,
    causal_walk_forward,
    count_interval_overlaps,
    purged_kfold,
)

FEATURE_NAMES = ("return_1", "return_5", "return_20", "return_60", "volatility_20", "volume_z20", "range_position20", "gap_1", "close_location")
FEATURE_FAMILIES = {
    "return_1": "momentum", "return_5": "momentum", "return_20": "momentum", "return_60": "momentum",
    "volatility_20": "volatility", "volume_z20": "volume", "range_position20": "trend",
    "gap_1": "momentum", "close_location": "trend",
}

# Absolute walk-forward anchoring: test blocks step by a fixed stride so that a shared history
# prefix always produces the same folds — and the same as-of predictions — when the book of
# history grows or its tail is truncated. 72 sessions ≈ 8 blocks per two-year post-warmup span.
LEDGER_TEST_BLOCK_SESSIONS = 72

# The frozen hyperparameters of the native logistic baseline. They are recorded in
# ``validation.model_hyperparameters`` under a version id: changing any value without
# bumping the version would silently mix incompatible predictions in the forward ledger.
ML_PARAMETER_VERSION = "ml_params_v1"
LOGISTIC_ITERATIONS = 500
LOGISTIC_LEARNING_RATE = 0.12
LOGISTIC_L2 = 1e-3
PLATT_MIN_OBSERVATIONS = 40
PLATT_ITERATIONS = 500
PLATT_LEARNING_RATE = 0.05
WALK_FORWARD_SPLITS = 8
WALK_FORWARD_MINIMUM_TRAIN = 30


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


def _fit(rows: list[Observation], *, iterations: int = LOGISTIC_ITERATIONS,
         learning_rate: float = LOGISTIC_LEARNING_RATE, l2: float = LOGISTIC_L2) -> LogisticModel:
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


@dataclass(frozen=True)
class _Dataset:
    """The frozen bars plus every walk-forward sample the later stages read."""

    bars: list[dict[str, Any]]
    closes: list[float]
    features: list[tuple[float, ...] | None]
    observations: list[Observation]
    train_rows: list[Observation]
    holdout_rows: list[Observation]
    holdout_start: int
    n: int


@dataclass(frozen=True)
class _PurgedCv:
    predictions: list[tuple[float, int]]
    folds: int
    purged: int
    embargoed: int
    overlaps: int


@dataclass(frozen=True)
class _WalkForward:
    predictions: list[tuple[Observation, float, float]]
    boundaries: dict[int, dict[str, Any]]
    folds: int
    purged: int
    gap: int


@dataclass(frozen=True)
class _FrozenCalibration:
    mapping: tuple[float, float] | None
    fit_rows: list[tuple[Observation, float, float]]
    oos_predictions: list[dict[str, Any]]
    wall_seconds: float


@dataclass(frozen=True)
class _Holdout:
    model: LogisticModel
    pairs: list[tuple[float, int]]
    forecasts: list[dict[str, Any]]


@dataclass(frozen=True)
class _PerOriginLedger:
    records: list[dict[str, Any]]
    refits: int
    wall_seconds: float


@dataclass(frozen=True)
class _CurrentEstimate:
    model: LogisticModel
    rows: list[Observation]
    raw_probability: float | None
    probability: float | None
    status: str
    warnings: list[str]


class _Fitter:
    """Times and counts every logistic fit so the reported training cost stays exact.

    Fits happen in a fixed order (purged folds, walk-forward folds, holdout, current),
    and `attempts`/`rows_seen` are part of the frozen evidence, so every stage must
    call this object rather than `_fit` directly.
    """

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


def _dataset(market: dict[str, Any], horizon: int) -> _Dataset:
    """Split the frozen bars into the pre-holdout training block and the holdout."""
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
    return _Dataset(bars=bars, closes=closes, features=features, observations=observations,
                    train_rows=train_rows, holdout_rows=holdout_rows, holdout_start=holdout_start, n=n)


def _purged_cv(dataset: _Dataset, horizon: int, fit: _Fitter) -> _PurgedCv:
    """Purging/embargo evidence on the pre-holdout block, scored non-overlapping."""
    train_rows = dataset.train_rows
    intervals = [SampleInterval(row.decision, row.information_start, row.label_end) for row in train_rows]
    folds = purged_kfold(intervals, n_splits=5, embargo_sessions=horizon)
    scored: list[tuple[Observation, float]] = []
    purged = embargoed = overlaps = 0
    for fold in folds:
        train = [train_rows[i] for i in fold.train]
        test = [train_rows[i] for i in fold.test]
        purged += fold.purged
        embargoed += fold.embargoed
        overlaps += count_interval_overlaps(intervals, fold.train, fold.test)
        if len(train) < 50 or len({row.target for row in train}) < 2:
            continue
        try:
            model = fit(train)
        except ValueError:
            continue
        score_decisions = set(non_overlapping([row.decision for row in test], horizon))
        scored.extend((row, model.predict(row)) for row in test if row.decision in score_decisions)
    scored.sort(key=lambda pair: pair[0].decision)
    cv_decisions = set(non_overlapping([row.decision for row, _ in scored], horizon))
    predictions = [(prediction, row.target) for row, prediction in scored if row.decision in cv_decisions]
    return _PurgedCv(predictions=predictions, folds=len(folds), purged=purged,
                     embargoed=embargoed, overlaps=overlaps)


def _walk_forward(dataset: _Dataset, horizon: int, fit: _Fitter) -> _WalkForward:
    """Full-history daily causal walk-forward ledger with absolute fold anchors.

    Expanding folds refit on matured rows only, predicting every session of their test
    block; fold anchors are absolute (fixed stride) so as-of predictions are stable
    under history growth and tail truncation. Adjacent horizon labels overlap;
    conservative non-overlapping counts and block-based inference treat the ledger
    accordingly. The minimum training size is pinned to the smallest purge-consistent
    fold the one-year default research window can support (a matured pre-fold sample
    of ~30).
    """
    bars, observations = dataset.bars, dataset.observations
    intervals = [SampleInterval(row.decision, row.information_start, row.label_end) for row in observations]
    folds = causal_walk_forward(intervals, n_splits=WALK_FORWARD_SPLITS, minimum_train=WALK_FORWARD_MINIMUM_TRAIN,
                                pre_test_gap_sessions=horizon, block_sessions=LEDGER_TEST_BLOCK_SESSIONS)
    predictions: list[tuple[Observation, float, float]] = []
    boundaries: dict[int, dict[str, Any]] = {}
    purged = gap = 0
    for fold in folds:
        train = [observations[i] for i in fold.train]
        test = [observations[i] for i in fold.test]
        purged += fold.purged
        gap += fold.embargoed
        if len({row.target for row in train}) < 2:
            continue
        try:
            model = fit(train)
        except ValueError:
            continue
        boundary = {
            "training_rows": len(train),
            "training_decision_start_session": bars[min(row.decision for row in train)]["date"],
            "training_decision_end_session": bars[max(row.decision for row in train)]["date"],
            "training_label_end_session": bars[max(row.label_end for row in train)]["date"],
        }
        for row in test:
            predictions.append((row, model.predict(row), model.score(row)))
            boundaries[row.decision] = boundary
    predictions.sort(key=lambda item: item[0].decision)
    return _WalkForward(predictions=predictions, boundaries=boundaries, folds=len(folds),
                        purged=purged, gap=gap)


def _frozen_calibration(dataset: _Dataset, horizon: int, ledger: _WalkForward) -> _FrozenCalibration:
    """The frozen Platt mapping: non-overlapping pre-holdout ledger logits only.

    Its training rows still end before the final holdout. It is used only for the
    one-time holdout transfer check and the current prediction; it is never applied to
    earlier ledger rows, which are calibrated at their own origin.
    """
    bars, holdout_start = dataset.bars, dataset.holdout_start
    pre_holdout = [(row, prediction, score) for row, prediction, score in ledger.predictions
                   if row.decision < holdout_start and row.label_end < max(0, holdout_start - 60)]
    pre_holdout_decisions = set(non_overlapping([row.decision for row, _, _ in pre_holdout], horizon))
    fit_rows = [(row, prediction, score) for row, prediction, score in pre_holdout
                if row.decision in pre_holdout_decisions]
    oos_predictions = [
        {"decision_session": bars[row.decision]["date"], "label_end_session": bars[row.label_end]["date"],
         "target": row.target, "raw_probability": prediction,
         **ledger.boundaries[row.decision]}
        for row, prediction, _ in fit_rows if row.decision in ledger.boundaries
    ]
    started = time.perf_counter()
    mapping = _fit_platt(
        [math.log(max(1e-6, min(1 - 1e-6, p)) / (1 - max(1e-6, min(1 - 1e-6, p)))) for _, p, _ in fit_rows],
        [row.target for row, _, _ in fit_rows])
    return _FrozenCalibration(mapping=mapping, fit_rows=fit_rows, oos_predictions=oos_predictions,
                              wall_seconds=time.perf_counter() - started)


def _holdout_evidence(dataset: _Dataset, horizon: int, calibration: _FrozenCalibration,
                      fit: _Fitter) -> _Holdout:
    """The one-time chronological holdout, scored by the frozen baseline and the mapping."""
    bars, train_rows, holdout_rows = dataset.bars, dataset.train_rows, dataset.holdout_rows
    model = fit(train_rows)
    pairs: list[tuple[float, int]] = []
    forecasts: list[dict[str, Any]] = []
    holdout_decisions = set(non_overlapping([row.decision for row in holdout_rows], horizon))
    for row in holdout_rows:
        if row.decision not in holdout_decisions:
            continue
        raw_probability = model.predict(row)
        probability = (logistic(calibration.mapping[1] + calibration.mapping[0] * model.score(row))
                       if calibration.mapping else raw_probability)
        pairs.append((probability, row.target))
        forecasts.append({
            "decision_session": bars[row.decision]["date"],
            "label_end_session": bars[row.label_end]["date"],
            "target": row.target,
            "probability": probability,
            "raw_probability": raw_probability,
            "training_rows": len(train_rows),
            "training_decision_start_session": bars[min(item.decision for item in train_rows)]["date"],
            "training_decision_end_session": bars[max(item.decision for item in train_rows)]["date"],
            "training_label_end_session": bars[max(item.label_end for item in train_rows)]["date"],
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
        })
    return _Holdout(model=model, pairs=pairs, forecasts=forecasts)


def _per_origin_calibration(dataset: _Dataset, horizon: int, ledger: _WalkForward) -> _PerOriginLedger:
    """Per-origin causal calibration of every ledger row.

    A row's probability is mapped by a Platt calibrator refitted at that origin from
    walk-forward OOS logits whose labels had matured strictly before the row's decision;
    no mapping fitted on later evidence is ever applied to an earlier row. Rows without
    at least 40 matured observations from both classes keep the raw walk-forward output
    and are recorded as uncalibrated.
    """
    bars = dataset.bars
    records: list[dict[str, Any]] = []
    refits = 0
    wall_seconds = 0.0
    matured_cursor = 0
    matured: list[tuple[Observation, float, float]] = []
    for row, raw_probability, raw_score in ledger.predictions:
        while (matured_cursor < len(ledger.predictions)
               and ledger.predictions[matured_cursor][0].decision < row.decision - horizon):
            matured.append(ledger.predictions[matured_cursor])
            matured_cursor += 1
        refit_started = time.perf_counter()
        causal_calibration = (_fit_platt([score for _, _, score in matured], [item[0].target for item in matured])
                              if len(matured) >= PLATT_MIN_OBSERVATIONS else None)
        wall_seconds += time.perf_counter() - refit_started
        if causal_calibration is not None:
            refits += 1
            row_probability = logistic(causal_calibration[1] + causal_calibration[0] * raw_score)
            calibration_applied = True
            calibration_label_end_session = bars[max(item[0].label_end for item in matured)]["date"]
            calibration_slope = float(causal_calibration[0])
            calibration_intercept = float(causal_calibration[1])
        else:
            row_probability = raw_probability
            calibration_applied = False
            calibration_label_end_session = None
            calibration_slope = None
            calibration_intercept = None
        records.append({
            "decision_session": bars[row.decision]["date"],
            "label_end_session": bars[row.label_end]["date"],
            "target": row.target,
            "probability": row_probability,
            "raw_probability": raw_probability,
            "calibration_applied": calibration_applied,
            "calibration_fit_observations": len(matured),
            "calibration_label_end_session": calibration_label_end_session,
            "calibration_slope": calibration_slope,
            "calibration_intercept": calibration_intercept,
            **ledger.boundaries[row.decision],
            "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
        })
    return _PerOriginLedger(records=records, refits=refits, wall_seconds=wall_seconds)


def _current_estimate(dataset: _Dataset, calibration: _FrozenCalibration, cv: _PurgedCv,
                      holdout: _Holdout, fit: _Fitter) -> _CurrentEstimate:
    """Today's probability: refit on every matured label once the holdout report is fixed.

    No final-holdout metric is used to select features or hyperparameters.
    """
    current_rows = [row for row in dataset.observations if row.label_end <= dataset.n - 1]
    current_model = (fit(current_rows)
                     if len(current_rows) >= 60 and len({row.target for row in current_rows}) == 2
                     else holdout.model)
    current_vector = dataset.features[-1] if dataset.features else None
    raw_current = current_model.predict(current_vector) if current_vector is not None else None
    if raw_current is None:
        probability: float | None = None
        status = "insufficient_data"
        warnings = ["The latest OHLCV row does not have a complete feature vector."]
    elif calibration.mapping:
        probability = logistic(calibration.mapping[1] + calibration.mapping[0] * current_model.score(current_vector))
        status = "success" if cv.predictions and calibration.fit_rows and dataset.holdout_rows else "partial"
        warnings = []
    else:
        probability = raw_current
        status = "partial"
        warnings = ["Too few valid causal walk-forward predictions were available for Platt calibration; probability is the raw logistic predict_proba output."]
    return _CurrentEstimate(model=current_model, rows=current_rows, raw_probability=raw_current,
                            probability=probability, status=status, warnings=warnings)


def _raw_holdout_brier(holdout: _Holdout) -> float | None:
    """Holdout Brier of the raw logistic output: the baseline the mapping must beat."""
    if not holdout.forecasts:
        return None
    return mean([(float(row["raw_probability"]) - int(row["target"])) ** 2
                 for row in holdout.forecasts if row.get("raw_probability") is not None])


def _calibration_transfer(calibration: _FrozenCalibration, holdout: _Holdout,
                          holdout_metrics: dict[str, Any]) -> dict[str, Any]:
    """How the migrated mapping compares with raw logits on data it never saw."""
    raw_holdout_brier = _raw_holdout_brier(holdout)
    calibrated_holdout_brier = holdout_metrics.get("brier")
    return {
        "status": "evaluated_on_chronological_holdout" if calibration.mapping and holdout.pairs else "not_assessed",
        "method": "the migrated Platt mapping is compared against raw logits on the untouched chronological holdout",
        "holdout_raw_brier": raw_holdout_brier,
        "holdout_calibrated_brier": calibrated_holdout_brier,
        "holdout_brier_delta_calibrated_minus_raw": (calibrated_holdout_brier - raw_holdout_brier
                                                     if isinstance(calibrated_holdout_brier, (int, float))
                                                     and isinstance(raw_holdout_brier, (int, float)) else None),
        "separation": "the holdout evaluates the frozen mapping fitted from non-overlapping pre-holdout walk-forward logits; the mapping's training rows end before the final holdout",
        "current_model_migration": "the frozen mapping is also applied to the current refit model, which has seen every matured label; that application is disclosed and cannot be re-validated against held-out data",
        "reason": None if calibration.mapping else "no validated Platt mapping was fitted; the raw logistic output is used",
    }


def _calibration_training_report(calibration: _FrozenCalibration, bars: list[dict[str, Any]],
                                 train_rows: list[Observation], holdout_start: int) -> dict[str, Any]:
    """The rows the frozen mapping was fitted on, with their label boundaries."""
    rows = calibration.fit_rows
    return {
        "input": "strict causal walk-forward OOS predictions from the pre-holdout training range (non-overlapping subset of the full-history ledger)",
        "observations": len(rows),
        "prediction_start_session": bars[min((row.decision for row, _, _ in rows))]["date"] if rows else None,
        "prediction_end_session": bars[max((row.decision for row, _, _ in rows))]["date"] if rows else None,
        "latest_label_end_session": bars[max((row.label_end for row, _, _ in rows))]["date"] if rows else None,
        "final_holdout_used": False,
        "training_row_boundary": bars[max((row.label_end for row in train_rows), default=holdout_start)]["date"],
        "status": "calibrated" if calibration.mapping else "uncalibrated",
        "prediction_records": calibration.oos_predictions,
    }


def _ledger_report(per_origin: _PerOriginLedger, horizon: int, non_overlapping_count: int,
                   ledger_metrics: dict[str, Any]) -> dict[str, Any]:
    """The historical as-of ledger summary."""
    return {
        "scope": "full-history daily causal walk-forward ledger from the frozen snapshot; adjacent labels overlap",
        "method": "expanding causal walk-forward folds with absolute anchors; each row predicts from fold models fitted only on matured rows before their block; every row's probability is calibrated at its own origin by a Platt refit that sees only walk-forward OOS logits whose labels had matured strictly before the row's decision (fewer than 40 matured observations or a single class keeps the raw output, marked uncalibrated); no mapping fitted on later evidence is applied to an earlier row",
        "observations": len(per_origin.records),
        "prediction_days": len(per_origin.records),
        "non_overlapping_prediction_count": non_overlapping_count,
        "effective_sample_size_estimate": round(len(per_origin.records) / (horizon + 1)),
        "calibrated_prediction_count": per_origin.refits,
        "uncalibrated_prediction_count": len(per_origin.records) - per_origin.refits,
        "labels_overlap": True,
        "brier": ledger_metrics.get("brier"),
        "base_rate_brier": ledger_metrics.get("base_rate_brier"),
    }


def _holdout_report(dataset: _Dataset, horizon: int, holdout: _Holdout,
                    holdout_metrics: dict[str, Any]) -> dict[str, Any]:
    """The one-time chronological holdout: metrics, forecasts and their boundaries."""
    bars, train_rows = dataset.bars, dataset.train_rows
    return {
        **holdout_metrics,
        "oos_forecasts": holdout.forecasts,
        "oos_evaluable_samples": len(holdout.forecasts),
        "all_oos_label_observations": len(dataset.holdout_rows),
        "oos_coverage": 1.0 if holdout.forecasts else 0.0,
        "oos_abstain_rate": 0.0 if holdout.forecasts else None,
        "target_definition": f"close[t+{horizon}] / close[t] - 1 > 0",
        "training_rows": len(train_rows),
        "training_decision_start_session": bars[min(row.decision for row in train_rows)]["date"],
        "training_decision_end_session": bars[max(row.decision for row in train_rows)]["date"],
        "training_label_end_session": bars[max(row.label_end for row in train_rows)]["date"],
        "start_session": bars[dataset.holdout_start]["date"] if dataset.holdout_start < dataset.n else None,
        "one_time_chronological_holdout": True,
    }


def _validation_block(
    dataset: _Dataset, horizon: int, fit: _Fitter, cv: _PurgedCv, ledger: _WalkForward,
    calibration: _FrozenCalibration, holdout: _Holdout, per_origin: _PerOriginLedger,
    current: _CurrentEstimate,
) -> dict[str, Any]:
    """The frozen validation payload: every diagnostic the audit and report read back.

    Key order is part of the on-disk contract (`json_payload` preserves insertion
    order), so sections are computed by helpers but inserted here in their original
    order.
    """
    bars, train_rows = dataset.bars, dataset.train_rows
    target_definition = f"close[t+{horizon}] / close[t] - 1 > 0"
    train_prevalence = mean([float(row.target) for row in train_rows])
    holdout_metrics = {**_metrics(holdout.pairs),
                       "training_base_rate_probability": train_prevalence,
                       "base_rate_brier": mean([(train_prevalence - y) ** 2 for _, y in holdout.pairs]) if train_prevalence is not None else None}
    ledger_pairs = [(float(record["probability"]), int(record["target"])) for record in per_origin.records]
    ledger_metrics = _metrics(ledger_pairs)
    ledger_metrics["base_rate_brier"] = mean([(train_prevalence - y) ** 2 for _, y in ledger_pairs]) if train_prevalence is not None else None
    ledger_non_overlapping = len(non_overlapping([row.decision for row, _, _ in ledger.predictions], horizon))
    return {
        "model": "native deterministic L2 logistic regression",
        "model_hyperparameters": {
            "version": ML_PARAMETER_VERSION,
            "iterations": LOGISTIC_ITERATIONS,
            "learning_rate": LOGISTIC_LEARNING_RATE,
            "l2": LOGISTIC_L2,
            "platt_min_observations": PLATT_MIN_OBSERVATIONS,
            "platt_iterations": PLATT_ITERATIONS,
            "platt_learning_rate": PLATT_LEARNING_RATE,
            "walk_forward_splits": WALK_FORWARD_SPLITS,
            "walk_forward_minimum_train": WALK_FORWARD_MINIMUM_TRAIN,
        },
        "features": list(FEATURE_NAMES),
        "feature_families": dict(FEATURE_FAMILIES),
        "random_seed": 0,
        "feature_availability": "close[t] and OHLCV through decision session t; target begins after t",
        "leakage_audit": {"features_use_data_through_decision_close_only": True, "target_excluded_from_features": True, "fold_local_scaling": True, "purge_uses_closed_information_intervals": True, "causal_walk_forward": True, "chronological_holdout": True, "historical_ledger_labels_overlap": True, "asof_calibration_per_origin": True, "frozen_mapping_not_retrofitted": True},
        "label": target_definition,
        "purged_kfold": {**_metrics(cv.predictions), "folds": cv.folds, "purged_train_rows": cv.purged, "embargoed_train_rows": cv.embargoed, "retained_interval_overlaps": cv.overlaps},
        "causal_walk_forward": {**_metrics([(prediction, row.target) for row, prediction, _ in ledger.predictions]), "folds": ledger.folds, "purged_train_rows": ledger.purged, "pre_test_gap_rows": ledger.gap, "scope": "full-history daily causal walk-forward ledger (overlapping labels)", "non_overlapping_predictions": ledger_non_overlapping},
        "probability_calibration": {"method": "Platt scaling on causal walk-forward logits (non-overlapping pre-holdout subset of the ledger); applies only to the one-time holdout transfer check and the current prediction, never to historical ledger rows" if calibration.mapping else "none", "calibrated": bool(calibration.mapping), "calibration_observations": len(calibration.fit_rows)},
        "calibration_transfer": _calibration_transfer(calibration, holdout, holdout_metrics),
        "calibration_training": _calibration_training_report(calibration, bars, train_rows, dataset.holdout_start),
        "historical_asof_ledger": _ledger_report(per_origin, horizon, ledger_non_overlapping, ledger_metrics),
        "final_holdout": _holdout_report(dataset, horizon, holdout, holdout_metrics),
        "oos_forecasts": per_origin.records,
        "oos_evaluable_samples": len(per_origin.records),
        "oos_predictions": len(per_origin.records),
        "prediction_days": len(per_origin.records),
        "non_overlapping_prediction_count": ledger_non_overlapping,
        "effective_sample_size_estimate": round(len(per_origin.records) / (horizon + 1)),
        "oos_scope": "full-history daily walk-forward ledger; adjacent labels overlap; non-overlapping count and effective size are the conservative references for thresholds; skill intervals use block resampling",
        "oos_coverage": len(per_origin.records) / len(dataset.observations) if dataset.observations else 0.0,
        "oos_abstain_rate": 1 - len(per_origin.records) / len(dataset.observations) if dataset.observations else None,
        "all_oos_label_observations": len(dataset.observations),
        "target_definition": target_definition,
        "model_training_performance": {**fit.as_metrics(), "calibration_fit_calls": int(bool(calibration.mapping)), "per_origin_calibration_refits": per_origin.refits, "calibration_wall_seconds": calibration.wall_seconds + per_origin.wall_seconds},
        "model_selection": {"configured_default": "native deterministic L2 logistic regression", "xgboost_status": "not_implemented_not_loaded", "lightgbm_status": "not_implemented_not_loaded", "boosted_models_enabled": False},
        "current_prediction_model_fit_rows": len(current.rows),
        "current_prediction_model_training_boundary": {
            "training_decision_start_session": bars[min(row.decision for row in current.rows)]["date"] if current.rows else None,
            "training_decision_end_session": bars[max(row.decision for row in current.rows)]["date"] if current.rows else None,
            "training_label_end_session": bars[max(row.label_end for row in current.rows)]["date"] if current.rows else None,
            "latest_prediction_session": bars[-1]["date"] if bars else None,
        },
        "accelerated_models": {"xgboost": "not_implemented_not_loaded", "lightgbm": "not_implemented_not_loaded"},
    }


def run(snapshot_dir: Path, loaded_snapshot: tuple[dict[str, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest, market = loaded_snapshot if loaded_snapshot is not None else load_snapshot(snapshot_dir)
    ticker, horizon_name = manifest["ticker"], manifest["horizon"]
    horizon = HORIZONS[horizon_name]
    dataset = _dataset(market, horizon)
    if len(dataset.train_rows) < 60 or len({row.target for row in dataset.train_rows}) < 2:
        return researcher_result("ml", ticker, horizon_name, manifest["snapshot_id"], status="insufficient_data", warnings=[f"The pre-holdout training sample has {len(dataset.train_rows)} matured rows; the native logistic baseline requires at least 60 and both labels."])

    fit = _Fitter()
    cv = _purged_cv(dataset, horizon, fit)
    ledger = _walk_forward(dataset, horizon, fit)
    calibration = _frozen_calibration(dataset, horizon, ledger)
    holdout = _holdout_evidence(dataset, horizon, calibration, fit)
    per_origin = _per_origin_calibration(dataset, horizon, ledger)
    current = _current_estimate(dataset, calibration, cv, holdout, fit)

    probability = current.probability
    status = "partial" if current.status == "success" else current.status  # the planned boosted-model comparison is not implemented in this MVP
    warnings = [*current.warnings,
                "XGBoost/LightGBM are not part of this native baseline; the ML path currently uses deterministic L2 logistic regression only.",
                "Returns are gross close-to-close observations; this model path does not model execution, spreads, slippage or fees.",
                "The full-history walk-forward ledger records daily overlapping predictions; effective independent samples are fewer than prediction days and skill intervals use block resampling."]
    if len(holdout.pairs) < 20:
        warnings.append("The chronological holdout has fewer than 20 predictions; its metrics are highly uncertain.")
    if cv.overlaps != 0:
        status = "invalid"
        probability = None
        warnings.append("Purged CV retained overlapping train/test information intervals; ML result vetoed.")

    validation = _validation_block(dataset, horizon, fit, cv, ledger, calibration, holdout, per_origin, current)
    predicted_return = mean([row.forward_return for row in current.rows if row.target == 1])
    down_return = mean([row.forward_return for row in current.rows if row.target == 0])
    expected_return = probability * predicted_return + (1 - probability) * down_return if probability is not None and predicted_return is not None and down_return is not None else None
    model_evidence = {
        "hypothesis": "L2 logistic prediction from multiple OHLCV input feature families",
        "features": list(FEATURE_NAMES), "model_probability": probability,
        "family_scope": "input_exposure; not fitted contribution or statistical independence",
        "factors": [{"family": family, "features": [name for name in FEATURE_NAMES if FEATURE_FAMILIES[name] == family]}
                    for family in sorted(set(FEATURE_FAMILIES.values()))],
    }
    evidence = {"positive": [model_evidence] if probability is not None and probability >= 0.5 else [],
                "negative": [model_evidence] if probability is not None and probability < 0.5 else []}
    return researcher_result("ml", ticker, horizon_name, manifest["snapshot_id"], status=status, prob_up=probability, expected_return=expected_return, evidence=evidence, validation=validation, data_used=["daily raw OHLCV"], warnings=warnings, probability_source="logistic_predict_proba_platt_calibrated" if calibration.mapping and probability is not None else "logistic_predict_proba_uncalibrated" if probability is not None else None, raw_probability=current.raw_probability if probability is not None else None, calibrated_probability=probability if calibration.mapping and probability is not None else None)
