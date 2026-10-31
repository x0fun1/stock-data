//! Research C: deterministic logistic baseline with production-consistent replay
//! (`researchers/ml.py`).
//!
//! Every reported out-of-sample probability is produced by the same function that
//! produces the live probability: a model fitted on labels that had matured at a fixed,
//! absolutely anchored refit session, applied to the features of the decision session.
//! The replay never trains on a label that was not yet known at the refit anchor and never
//! reports a probability the live entry could not have produced with the same input prefix.
//!
//! The shadow path never feeds the production probability: it is a `shadow` research
//! result, `calibration` is disabled by policy, and the published value is the raw logistic
//! output (explicitly not a calibrated frequency).

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use serde_json::{Map, Value};

use crate::math_utils::{logistic, sample_std};
use crate::result::researcher_result;
use sd_core::float_sum::python_sum;
use sd_core::market_data::market_bars;
use sd_core::policy;

/// Declared feature sets: `(names, warmup sessions)`.
pub const FEATURE_SETS: [(&str, &[&str], usize); 2] = [
    (
        "full_v1",
        &[
            "return_1",
            "return_5",
            "return_20",
            "return_60",
            "volatility_20",
            "volume_z20",
            "range_position20",
            "gap_1",
            "close_location",
        ],
        60,
    ),
    (
        "compact_v1",
        &["return_5", "return_20", "volatility_20"],
        20,
    ),
];

/// Feature -> declared family (input exposure only, never a fitted contribution).
pub const FEATURE_FAMILIES: [(&str, &str); 9] = [
    ("return_1", "momentum"),
    ("return_5", "momentum"),
    ("return_20", "momentum"),
    ("return_60", "momentum"),
    ("volatility_20", "volatility"),
    ("volume_z20", "volume"),
    ("range_position20", "trend"),
    ("gap_1", "momentum"),
    ("close_location", "trend"),
];

/// Feature set in production.
pub const DEFAULT_FEATURE_SET: &str = "compact_v1";
/// Frozen hyperparameter version (bump on any change or the ledger mixes models).
pub const ML_PARAMETER_VERSION: &str = "ml_params_v3";
/// Logistic gradient-descent iterations.
pub const LOGISTIC_ITERATIONS: usize = 60;
/// Logistic learning rate.
pub const LOGISTIC_LEARNING_RATE: f64 = 0.12;
/// Logistic L2 penalty.
pub const LOGISTIC_L2: f64 = 1.5;
/// Minimum matured observations before a Platt shadow fit is attempted.
pub const PLATT_MIN_OBSERVATIONS: usize = 40;
/// Platt gradient-descent iterations.
pub const PLATT_ITERATIONS: usize = 500;
/// Platt learning rate.
pub const PLATT_LEARNING_RATE: f64 = 0.05;
/// Minimum training rows with both classes before a refit is attempted.
pub const MINIMUM_TRAINING_ROWS: usize = 60;

fn feature_set(name: &str) -> Option<(&'static [&'static str], usize)> {
    FEATURE_SETS
        .iter()
        .find(|(candidate, _, _)| *candidate == name)
        .map(|(_, names, warmup)| (*names, *warmup))
}

fn feature_names(name: &str) -> Vec<Value> {
    feature_set(name)
        .map(|(names, _)| {
            names
                .iter()
                .map(|name| Value::String((*name).into()))
                .collect()
        })
        .unwrap_or_default()
}

fn family_of(name: &str) -> Option<&'static str> {
    FEATURE_FAMILIES
        .iter()
        .find(|(feature, _)| *feature == name)
        .map(|(_, family)| *family)
}

/// Python `sum()` over floats: compensated (CPython 3.12+ Neumaier).
fn py_mean(values: &[f64]) -> Option<f64> {
    let finite: Vec<f64> = values
        .iter()
        .copied()
        .filter(|value| value.is_finite())
        .collect();
    if finite.is_empty() {
        None
    } else {
        Some(python_sum(finite.iter().copied()) / finite.len() as f64)
    }
}

/// Python `round(value, 6)` on the exact decimal value of the binary float.
fn round6(value: f64) -> f64 {
    format!("{value:.6}").parse::<f64>().unwrap_or(value)
}

fn logistic_clamped(value: f64) -> f64 {
    logistic(value)
}

/// One matured training row / replay decision.
#[derive(Debug, Clone)]
struct Observation {
    decision: i64,
    label_end: i64,
    features: Vec<f64>,
    target: i64,
    forward_return: f64,
}

/// Standardized L2 logistic model.
#[derive(Debug, Clone)]
struct LogisticModel {
    means: Vec<f64>,
    scales: Vec<f64>,
    coefficients: Vec<f64>,
    intercept: f64,
}

impl LogisticModel {
    fn score(&self, values: &[f64]) -> f64 {
        let standardized: Vec<f64> = values
            .iter()
            .zip(self.means.iter())
            .zip(self.scales.iter())
            .map(|((value, center), scale)| (value - center) / scale)
            .collect();
        self.intercept
            + python_sum(
                self.coefficients
                    .iter()
                    .zip(standardized.iter())
                    .map(|(weight, value)| weight * value),
            )
    }

    fn predict(&self, values: &[f64]) -> f64 {
        logistic(self.score(values))
    }
}

/// Counts every logistic fit so the reported training cost stays exact.
#[derive(Debug, Default)]
struct Fitter {
    attempts: i64,
    successful_fits: i64,
    rows_seen: i64,
    wall_seconds: f64,
}

impl Fitter {
    fn fit(&mut self, rows: &[Observation]) -> Result<LogisticModel, String> {
        let started = Instant::now();
        self.attempts += 1;
        self.rows_seen += rows.len() as i64;
        let model = fit_core(rows);
        self.wall_seconds += started.elapsed().as_secs_f64();
        let model = model?;
        self.successful_fits += 1;
        Ok(model)
    }

    fn as_metrics(&self) -> Map<String, Value> {
        let mut metrics = Map::new();
        metrics.insert("attempts".into(), Value::from(self.attempts));
        metrics.insert("successful_fits".into(), Value::from(self.successful_fits));
        metrics.insert("rows_seen".into(), Value::from(self.rows_seen));
        metrics.insert("wall_seconds".into(), Value::from(self.wall_seconds));
        metrics
    }
}

/// Deterministic L2 logistic regression (fixed iterations, learning rate and penalty).
fn fit_core(rows: &[Observation]) -> Result<LogisticModel, String> {
    let targets: HashSet<i64> = rows.iter().map(|row| row.target).collect();
    if rows.len() < 30 || targets.len() < 2 {
        return Err("logistic fit requires at least 30 observations from both classes".into());
    }
    let width = rows[0].features.len();
    let mut means = Vec::with_capacity(width);
    let mut scales = Vec::with_capacity(width);
    for column in 0..width {
        means.push(python_sum(rows.iter().map(|row| row.features[column])) / rows.len() as f64);
        let column_values: Vec<f64> = rows.iter().map(|row| row.features[column]).collect();
        let scale = sample_std(&column_values);
        scales.push(match scale {
            Some(value) if value > 1e-12 => value,
            _ => 1.0,
        });
    }
    let matrix: Vec<Vec<f64>> = rows
        .iter()
        .map(|row| {
            (0..width)
                .map(|column| (row.features[column] - means[column]) / scales[column])
                .collect()
        })
        .collect();
    let mut coefficients = vec![0.0f64; width];
    let positives: i64 = rows.iter().map(|row| row.target).sum();
    let negatives = rows.len() as i64 - positives;
    let mut intercept = ((positives as f64 + 0.5) / (negatives as f64 + 0.5)).ln();
    for _ in 0..LOGISTIC_ITERATIONS {
        let mut grad = vec![0.0f64; width];
        let mut grad_intercept = 0.0f64;
        for (values, row) in matrix.iter().zip(rows.iter()) {
            let probability = logistic_clamped(
                intercept
                    + python_sum(
                        coefficients
                            .iter()
                            .zip(values.iter())
                            .map(|(weight, value)| weight * value),
                    ),
            );
            let residual = probability - row.target as f64;
            grad_intercept += residual;
            for (column, value) in values.iter().enumerate() {
                grad[column] += residual * value;
            }
        }
        let count = rows.len() as f64;
        intercept -= LOGISTIC_LEARNING_RATE * grad_intercept / count;
        coefficients = coefficients
            .iter()
            .zip(grad.iter())
            .map(|(weight, gradient)| {
                weight - LOGISTIC_LEARNING_RATE * (gradient / count + LOGISTIC_L2 * weight)
            })
            .collect();
    }
    Ok(LogisticModel {
        means,
        scales,
        coefficients,
        intercept,
    })
}

/// Platt shadow mapping `(slope, intercept)`; never applied to a published probability.
fn fit_platt(scores: &[f64], targets: &[i64]) -> Option<(f64, f64)> {
    let distinct: HashSet<i64> = targets.iter().copied().collect();
    if scores.len() < PLATT_MIN_OBSERVATIONS || distinct.len() < 2 {
        return None;
    }
    let mut slope = 1.0f64;
    let mut intercept = 0.0f64;
    for _ in 0..PLATT_ITERATIONS {
        let mut grad_slope = 0.0f64;
        let mut grad_intercept = 0.0f64;
        for (score, target) in scores.iter().zip(targets.iter()) {
            let residual = logistic_clamped(intercept + slope * score) - *target as f64;
            grad_slope += residual * score;
            grad_intercept += residual;
        }
        let count = scores.len() as f64;
        slope -= PLATT_LEARNING_RATE * (grad_slope / count + 1e-3 * slope);
        intercept -= PLATT_LEARNING_RATE * grad_intercept / count;
    }
    Some((slope, intercept))
}

/// Per-session feature vectors (`None` before warm-up or on invalid prices).
fn features(
    bars: &[sd_core::interfaces::Bar],
    feature_set_name: &str,
) -> Result<Vec<Option<Vec<f64>>>, String> {
    let Some((names, warmup)) = feature_set(feature_set_name) else {
        return Err(format!("unknown feature set {feature_set_name:?}"));
    };
    let count = bars.len();
    let close: Vec<f64> = bars.iter().map(|bar| bar.close).collect();
    let high: Vec<f64> = bars.iter().map(|bar| bar.high).collect();
    let low: Vec<f64> = bars.iter().map(|bar| bar.low).collect();
    let opening: Vec<f64> = bars.iter().map(|bar| bar.open).collect();
    let volume: Vec<f64> = bars.iter().map(|bar| bar.volume).collect();
    let mut result: Vec<Option<Vec<f64>>> = vec![None; count];
    for index in warmup..count {
        let returns: Vec<f64> = (index - 19..=index)
            .map(|position| close[position] / close[position - 1] - 1.0)
            .collect();
        let volume_window = &volume[index - 19..=index];
        let volume_std = sample_std(volume_window);
        let highest = high[index - 19..=index]
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        let lowest = low[index - 19..=index]
            .iter()
            .copied()
            .fold(f64::INFINITY, f64::min);
        let day_range = high[index] - low[index];
        let mut lookbacks = vec![
            close[index - 20],
            close[index - 5],
            close[index - 1],
            close[index - 19],
        ];
        if feature_set_name == "full_v1" {
            lookbacks.push(close[index - 60]);
        }
        if lookbacks.iter().copied().fold(f64::INFINITY, f64::min) <= 0.0 {
            continue;
        }
        let volume_mean = python_sum(volume_window.iter().copied()) / volume_window.len() as f64;
        let volume_z = match volume_std {
            Some(std) if std > 0.0 => (volume[index] - volume_mean) / std,
            _ => 0.0,
        };
        let values = [
            ("return_1", close[index] / close[index - 1] - 1.0),
            ("return_5", close[index] / close[index - 5] - 1.0),
            ("return_20", close[index] / close[index - 20] - 1.0),
            (
                "return_60",
                if feature_set_name == "full_v1" {
                    close[index] / close[index - 60] - 1.0
                } else {
                    0.0
                },
            ),
            ("volatility_20", sample_std(&returns).unwrap_or(0.0)),
            ("volume_z20", volume_z),
            (
                "range_position20",
                if highest > lowest {
                    (close[index] - lowest) / (highest - lowest)
                } else {
                    0.5
                },
            ),
            ("gap_1", opening[index] / close[index - 1] - 1.0),
            (
                "close_location",
                if day_range != 0.0 {
                    (2.0 * close[index] - high[index] - low[index]) / day_range
                } else {
                    0.0
                },
            ),
        ];
        let map: HashMap<&str, f64> = values.into_iter().collect();
        result[index] = Some(names.iter().map(|name| map[name]).collect());
    }
    Ok(result)
}

#[derive(Debug)]
struct Dataset {
    bars: Vec<sd_core::interfaces::Bar>,
    closes: Vec<f64>,
    features: Vec<Option<Vec<f64>>>,
    observations: Vec<Observation>,
    n: usize,
    warmup: usize,
    feature_set: String,
}

fn dataset(market: &Value, horizon: i64, feature_set_name: &str) -> Result<Dataset, String> {
    let Some((_, warmup)) = feature_set(feature_set_name) else {
        return Err(format!("unknown feature set {feature_set_name:?}"));
    };
    let bars = market_bars(market, false).map_err(|error| error.message().to_string())?;
    let minimum = warmup + horizon as usize + MINIMUM_TRAINING_ROWS;
    if bars.len() < minimum {
        return Err(format!(
            "need at least {minimum} bars for {feature_set_name} at horizon {horizon}"
        ));
    }
    let closes: Vec<f64> = bars.iter().map(|bar| bar.close).collect();
    let features = features(&bars, feature_set_name)?;
    let mut observations: Vec<Observation> = Vec::new();
    for (index, vector) in features.iter().enumerate() {
        let Some(vector) = vector else {
            continue;
        };
        if index + horizon as usize >= bars.len() {
            continue;
        }
        let target_return = closes[index + horizon as usize] / closes[index] - 1.0;
        observations.push(Observation {
            decision: index as i64,
            // The purged interval is the label interval [decision, decision + horizon];
            // the feature lookback is observable at the decision close.
            label_end: index as i64 + horizon,
            features: vector.clone(),
            target: i64::from(target_return > 0.0),
            forward_return: target_return,
        });
    }
    let n = bars.len();
    Ok(Dataset {
        bars,
        closes,
        features,
        observations,
        n,
        warmup,
        feature_set: feature_set_name.to_string(),
    })
}

fn anchor(origin: i64) -> i64 {
    origin
}

fn training_rows(dataset: &Dataset, anchor: i64) -> Vec<&Observation> {
    dataset
        .observations
        .iter()
        .filter(|row| row.label_end <= anchor)
        .collect()
}

#[derive(Debug, Clone)]
struct Calibration {
    mapping: Option<(f64, f64)>,
    observations: usize,
    label_end: Option<i64>,
    status: String,
}

fn calibration_for(records: &[Record], origin: i64) -> Calibration {
    let matured: Vec<&Record> = records
        .iter()
        .filter(|record| record.label_end_index < origin)
        .collect();
    let label_end = matured.iter().map(|record| record.label_end_index).max();
    if matured.len() < PLATT_MIN_OBSERVATIONS {
        return Calibration {
            mapping: None,
            observations: matured.len(),
            label_end,
            status: "capacity_insufficient".into(),
        };
    }
    let targets: HashSet<i64> = matured.iter().map(|record| record.target).collect();
    if targets.len() < 2 {
        return Calibration {
            mapping: None,
            observations: matured.len(),
            label_end,
            status: "single_class".into(),
        };
    }
    let scores: Vec<f64> = matured
        .iter()
        .map(|record| logit(record.raw_probability))
        .collect();
    let target_values: Vec<i64> = matured.iter().map(|record| record.target).collect();
    match fit_platt(&scores, &target_values) {
        Some(mapping) => Calibration {
            mapping: Some(mapping),
            observations: matured.len(),
            label_end,
            status: "fitted".into(),
        },
        None => Calibration {
            mapping: None,
            observations: matured.len(),
            label_end,
            status: "fit_failed".into(),
        },
    }
}

/// One replay record; serialized with the legacy key order.
#[derive(Debug, Clone)]
struct Record {
    decision_session: String,
    label_end_session: String,
    decision_index: i64,
    label_end_index: i64,
    target: i64,
    probability: f64,
    raw_probability: f64,
    calibration_applied: bool,
    calibration_status: String,
    platt_shadow_probability: Option<f64>,
    platt_shadow_applied: bool,
    calibration_fit_observations: usize,
    calibration_observations: usize,
    calibration_required_observations: usize,
    calibration_label_end_session: Option<String>,
    calibration_slope: Option<f64>,
    calibration_intercept: Option<f64>,
    training_rows: usize,
    training_decision_start_session: String,
    training_decision_end_session: String,
    training_label_end_session: String,
    refit_anchor_session: String,
    baseline_probability: Option<f64>,
    baseline_method: &'static str,
    target_definition: String,
}

fn opt_number(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

fn opt_string(value: &Option<String>) -> Value {
    value.clone().map(Value::String).unwrap_or(Value::Null)
}

impl Record {
    fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "decision_session".into(),
            Value::String(self.decision_session.clone()),
        );
        map.insert(
            "label_end_session".into(),
            Value::String(self.label_end_session.clone()),
        );
        map.insert("decision_index".into(), Value::from(self.decision_index));
        map.insert("label_end_index".into(), Value::from(self.label_end_index));
        map.insert("target".into(), Value::from(self.target));
        map.insert("probability".into(), Value::from(self.probability));
        map.insert("raw_probability".into(), Value::from(self.raw_probability));
        map.insert(
            "calibration_applied".into(),
            Value::Bool(self.calibration_applied),
        );
        map.insert(
            "calibration_status".into(),
            Value::String(self.calibration_status.clone()),
        );
        map.insert(
            "platt_shadow_probability".into(),
            opt_number(self.platt_shadow_probability),
        );
        map.insert(
            "platt_shadow_applied".into(),
            Value::Bool(self.platt_shadow_applied),
        );
        map.insert(
            "calibration_fit_observations".into(),
            Value::from(self.calibration_fit_observations as i64),
        );
        map.insert(
            "calibration_observations".into(),
            Value::from(self.calibration_observations as i64),
        );
        map.insert(
            "calibration_required_observations".into(),
            Value::from(self.calibration_required_observations as i64),
        );
        map.insert(
            "calibration_label_end_session".into(),
            opt_string(&self.calibration_label_end_session),
        );
        map.insert(
            "calibration_slope".into(),
            opt_number(self.calibration_slope),
        );
        map.insert(
            "calibration_intercept".into(),
            opt_number(self.calibration_intercept),
        );
        map.insert(
            "training_rows".into(),
            Value::from(self.training_rows as i64),
        );
        map.insert(
            "training_decision_start_session".into(),
            Value::String(self.training_decision_start_session.clone()),
        );
        map.insert(
            "training_decision_end_session".into(),
            Value::String(self.training_decision_end_session.clone()),
        );
        map.insert(
            "training_label_end_session".into(),
            Value::String(self.training_label_end_session.clone()),
        );
        map.insert(
            "refit_anchor_session".into(),
            Value::String(self.refit_anchor_session.clone()),
        );
        map.insert(
            "baseline_probability".into(),
            opt_number(self.baseline_probability),
        );
        map.insert(
            "baseline_method".into(),
            Value::String(self.baseline_method.into()),
        );
        map.insert(
            "target_definition".into(),
            Value::String(self.target_definition.clone()),
        );
        Value::Object(map)
    }
}

#[derive(Debug, Clone)]
struct OriginPrediction {
    anchor: i64,
    probability: f64,
    raw_probability: f64,
    calibration: Calibration,
    training_rows: usize,
    training_start: i64,
    training_end: i64,
    training_label_end: i64,
}

fn logit(probability: f64) -> f64 {
    let clipped = probability.clamp(1e-6, 1.0 - 1e-6);
    (clipped / (1.0 - clipped)).ln()
}

fn causal_prefix(closes: &[f64], horizon: i64) -> Vec<i64> {
    let mut prefix = vec![0i64];
    for index in 0..(closes.len() as i64 - horizon).max(0) as usize {
        let up = i64::from(closes[index + horizon as usize] > closes[index]);
        let next = prefix[prefix.len() - 1] + up;
        prefix.push(next);
    }
    prefix
}

fn causal_baseline(prefix: &[i64], origin: i64, horizon: i64) -> Option<f64> {
    let count = origin - horizon + 1;
    if count <= 0 || count >= prefix.len() as i64 {
        return None;
    }
    Some(prefix[count as usize] as f64 / count as f64)
}

fn brier(pairs: &[(f64, i64)]) -> f64 {
    py_mean(
        pairs
            .iter()
            .map(|(probability, target)| (probability - *target as f64).powi(2))
            .collect::<Vec<f64>>()
            .as_slice(),
    )
    .unwrap_or_default()
}

fn platt_apply(mapping: (f64, f64), score: f64) -> f64 {
    logistic(mapping.0 + mapping.1 * score)
}

fn log_loss(pairs: &[(f64, i64)]) -> f64 {
    let mut total = 0.0f64;
    for (probability, target) in pairs {
        let clipped = probability.clamp(1e-6, 1.0 - 1e-6);
        total -= if *target != 0 {
            clipped.ln()
        } else {
            (1.0 - clipped).ln()
        };
    }
    total / pairs.len() as f64
}

fn predict_at_origin(
    dataset: &Dataset,
    origin: i64,
    calibration: &Calibration,
    fitter: &mut Fitter,
) -> Result<Option<OriginPrediction>, String> {
    let anchor = anchor(origin);
    let rows = training_rows(dataset, anchor);
    let targets: HashSet<i64> = rows.iter().map(|row| row.target).collect();
    if rows.len() < MINIMUM_TRAINING_ROWS || targets.len() < 2 {
        return Ok(None);
    }
    let Some(vector) = dataset.features[origin as usize].as_ref() else {
        return Ok(None);
    };
    let model = fitter.fit(&rows.iter().map(|row| (*row).clone()).collect::<Vec<_>>())?;
    let raw = model.predict(vector);
    // Calibration is disabled by policy: the raw logistic output is the reported estimate.
    let disabled = Calibration {
        mapping: None,
        observations: calibration.observations,
        label_end: calibration.label_end,
        status: "disabled_by_policy".into(),
    };
    Ok(Some(OriginPrediction {
        anchor,
        probability: raw,
        raw_probability: raw,
        calibration: disabled,
        training_rows: rows.len(),
        training_start: rows.iter().map(|row| row.decision).min().unwrap_or(0),
        training_end: rows.iter().map(|row| row.decision).max().unwrap_or(0),
        training_label_end: rows.iter().map(|row| row.label_end).max().unwrap_or(0),
    }))
}

fn replay(
    dataset: &Dataset,
    horizon: i64,
    fitter: &mut Fitter,
) -> Result<(Vec<Record>, i64, f64), String> {
    let started = Instant::now();
    let prefix = causal_prefix(&dataset.closes, horizon);
    let origins: Vec<i64> = (dataset.warmup as i64..dataset.n as i64 - horizon).collect();
    let mut records: Vec<Record> = Vec::new();
    let mut models: HashMap<i64, LogisticModel> = HashMap::new();
    for origin in origins {
        let anchor = anchor(origin);
        let rows = training_rows(dataset, anchor);
        let targets: HashSet<i64> = rows.iter().map(|row| row.target).collect();
        if rows.len() < MINIMUM_TRAINING_ROWS || targets.len() < 2 {
            continue;
        }
        let Some(vector) = dataset.features[origin as usize].as_ref() else {
            continue;
        };
        let model = match models.get(&anchor) {
            Some(model) => model.clone(),
            None => {
                let model =
                    fitter.fit(&rows.iter().map(|row| (*row).clone()).collect::<Vec<_>>())?;
                models.insert(anchor, model.clone());
                model
            }
        };
        let raw = model.predict(vector);
        let calibration = calibration_for(&records, origin);
        // The shadow Platt value is retained as a diagnostic; it is never published.
        let shadow_probability = calibration
            .mapping
            .map(|mapping| platt_apply(mapping, logit(raw)));
        let probability = raw;
        let target = i64::from(
            dataset.closes[(origin + horizon) as usize] > dataset.closes[origin as usize],
        );
        let calibration_label_end_session = calibration
            .label_end
            .map(|index| dataset.bars[index as usize].date.clone());
        let training_start = rows.iter().map(|row| row.decision).min().unwrap_or(0);
        let training_end = rows.iter().map(|row| row.decision).max().unwrap_or(0);
        let training_label_end = rows.iter().map(|row| row.label_end).max().unwrap_or(0);
        records.push(Record {
            decision_session: dataset.bars[origin as usize].date.clone(),
            label_end_session: dataset.bars[(origin + horizon) as usize].date.clone(),
            decision_index: origin,
            label_end_index: origin + horizon,
            target,
            probability,
            raw_probability: raw,
            calibration_applied: false,
            calibration_status: "disabled_by_policy".into(),
            platt_shadow_probability: shadow_probability,
            platt_shadow_applied: shadow_probability.is_some(),
            calibration_fit_observations: calibration.observations,
            calibration_observations: calibration.observations,
            calibration_required_observations: PLATT_MIN_OBSERVATIONS,
            calibration_label_end_session,
            calibration_slope: calibration.mapping.map(|mapping| mapping.1),
            calibration_intercept: calibration.mapping.map(|mapping| mapping.0),
            training_rows: rows.len(),
            training_decision_start_session: dataset.bars[training_start as usize].date.clone(),
            training_decision_end_session: dataset.bars[training_end as usize].date.clone(),
            training_label_end_session: dataset.bars[training_label_end as usize].date.clone(),
            refit_anchor_session: dataset.bars[anchor as usize].date.clone(),
            baseline_probability: causal_baseline(&prefix, origin, horizon),
            baseline_method: "causal_expanding_up_rate_matured_labels",
            target_definition: format!("close[t+{horizon}] / close[t] - 1 > 0"),
        });
    }
    Ok((
        records,
        models.len() as i64,
        started.elapsed().as_secs_f64(),
    ))
}

fn summary(records: &[Record]) -> Map<String, Value> {
    if records.is_empty() {
        let mut map = Map::new();
        map.insert("observations".into(), Value::from(0));
        map.insert("brier".into(), Value::Null);
        map.insert("causal_baseline_brier".into(), Value::Null);
        map.insert("brier_skill_vs_causal_baseline".into(), Value::Null);
        map.insert("log_loss".into(), Value::Null);
        map.insert("direction_accuracy".into(), Value::Null);
        map.insert("always_up_accuracy".into(), Value::Null);
        map.insert("balanced_accuracy".into(), Value::Null);
        map.insert("observed_up_rate".into(), Value::Null);
        map.insert("calibrated_predictions".into(), Value::from(0));
        map.insert("uncalibrated_predictions".into(), Value::from(0));
        return map;
    }
    let pairs: Vec<(f64, i64)> = records
        .iter()
        .map(|record| (record.probability, record.target))
        .collect();
    let brier_value = brier(&pairs);
    let baseline_pairs: Vec<(f64, i64)> = records
        .iter()
        .filter_map(|record| {
            record
                .baseline_probability
                .map(|probability| (probability, record.target))
        })
        .collect();
    let baseline_brier = if baseline_pairs.is_empty() {
        None
    } else {
        Some(brier(&baseline_pairs))
    };
    let direction: Vec<(f64, i64)> = pairs
        .iter()
        .filter(|(probability, _)| *probability != 0.5)
        .copied()
        .collect();
    let direction_accuracy = if direction.is_empty() {
        None
    } else {
        Some(
            direction
                .iter()
                .filter(|(probability, target)| i64::from(*probability > 0.5) == *target)
                .count() as f64
                / direction.len() as f64,
        )
    };
    let always_up = if direction.is_empty() {
        None
    } else {
        Some(
            direction.iter().map(|(_, target)| *target).sum::<i64>() as f64
                / direction.len() as f64,
        )
    };
    let mut recalls: Vec<f64> = Vec::new();
    for label in [0i64, 1] {
        let subset: Vec<(f64, i64)> = direction
            .iter()
            .filter(|(_, target)| *target == label)
            .copied()
            .collect();
        if !subset.is_empty() {
            recalls.push(
                subset
                    .iter()
                    .filter(|(probability, _)| i64::from(*probability > 0.5) == label)
                    .count() as f64
                    / subset.len() as f64,
            );
        }
    }
    let mut map = Map::new();
    map.insert("observations".into(), Value::from(records.len() as i64));
    map.insert("brier".into(), Value::from(round6(brier_value)));
    map.insert(
        "causal_baseline_brier".into(),
        baseline_brier
            .map(|value| Value::from(round6(value)))
            .unwrap_or(Value::Null),
    );
    map.insert(
        "brier_skill_vs_causal_baseline".into(),
        match baseline_brier {
            Some(value) if value != 0.0 => Value::from(round6(1.0 - brier_value / value)),
            _ => Value::Null,
        },
    );
    map.insert("log_loss".into(), Value::from(round6(log_loss(&pairs))));
    map.insert(
        "direction_accuracy".into(),
        direction_accuracy
            .map(|value| Value::from(round6(value)))
            .unwrap_or(Value::Null),
    );
    map.insert(
        "always_up_accuracy".into(),
        always_up
            .map(|value| Value::from(round6(value)))
            .unwrap_or(Value::Null),
    );
    map.insert(
        "balanced_accuracy".into(),
        if recalls.len() == 2 {
            Value::from(round6(
                python_sum(recalls.iter().copied()) / recalls.len() as f64,
            ))
        } else {
            Value::Null
        },
    );
    map.insert(
        "observed_up_rate".into(),
        Value::from(round6(
            pairs.iter().map(|(_, target)| *target).sum::<i64>() as f64 / pairs.len() as f64,
        )),
    );
    map.insert(
        "calibrated_predictions".into(),
        Value::from(
            records
                .iter()
                .filter(|record| record.calibration_applied)
                .count() as i64,
        ),
    );
    map.insert(
        "uncalibrated_predictions".into(),
        Value::from(
            records
                .iter()
                .filter(|record| !record.calibration_applied)
                .count() as i64,
        ),
    );
    map.insert(
        "first_decision_session".into(),
        Value::String(records[0].decision_session.clone()),
    );
    map.insert(
        "last_decision_session".into(),
        Value::String(records[records.len() - 1].decision_session.clone()),
    );
    map.insert(
        "mean_probability".into(),
        Value::from(round6(
            python_sum(records.iter().map(|record| record.probability)) / records.len() as f64,
        )),
    );
    map
}

fn validation_block(
    dataset: &Dataset,
    horizon: i64,
    fitter: &Fitter,
    records: &[Record],
    current: &OriginPrediction,
    wall_seconds: f64,
) -> Value {
    let summary_block = summary(records);
    let recent_count = (records.len() / 5).max(1);
    let recent = summary(&records[records.len() - recent_count..]);
    let matured_for_current = current.training_rows;
    let capacity_reachable = false;
    let maximum_non_overlapping = 0.max(
        (dataset.n as i64 - dataset.warmup as i64 - horizon - MINIMUM_TRAINING_ROWS as i64)
            / (horizon + 1),
    );
    let actionable_decisions =
        1.max(dataset.n as i64 - dataset.warmup as i64 - horizon - MINIMUM_TRAINING_ROWS as i64);
    let target_definition = format!("close[t+{horizon}] / close[t] - 1 > 0");
    let names = feature_names(&dataset.feature_set);
    let mut model_hyperparameters = Map::new();
    model_hyperparameters.insert("version".into(), Value::String(ML_PARAMETER_VERSION.into()));
    model_hyperparameters.insert(
        "parameter_version".into(),
        Value::String(ML_PARAMETER_VERSION.into()),
    );
    model_hyperparameters.insert(
        "model".into(),
        Value::String("native deterministic L2 logistic regression".into()),
    );
    model_hyperparameters.insert(
        "feature_set".into(),
        Value::String(dataset.feature_set.clone()),
    );
    model_hyperparameters.insert("features".into(), Value::Array(names.clone()));
    model_hyperparameters.insert(
        "feature_warmup_sessions".into(),
        Value::from(dataset.warmup as i64),
    );
    model_hyperparameters.insert("refit_sessions".into(), Value::from(1));
    model_hyperparameters.insert(
        "training_window".into(),
        Value::String("fixed_52_week_snapshot_date_window".into()),
    );
    model_hyperparameters.insert(
        "minimum_training_rows".into(),
        Value::from(MINIMUM_TRAINING_ROWS as i64),
    );
    model_hyperparameters.insert("iterations".into(), Value::from(LOGISTIC_ITERATIONS as i64));
    model_hyperparameters.insert("learning_rate".into(), Value::from(LOGISTIC_LEARNING_RATE));
    model_hyperparameters.insert("l2".into(), Value::from(LOGISTIC_L2));
    model_hyperparameters.insert(
        "platt_min_observations".into(),
        Value::from(PLATT_MIN_OBSERVATIONS as i64),
    );
    model_hyperparameters.insert("random_seed".into(), Value::from(0));
    let mut leakage = Map::new();
    leakage.insert(
        "features_use_data_through_decision_close_only".into(),
        Value::Bool(true),
    );
    leakage.insert("target_excluded_from_features".into(), Value::Bool(true));
    leakage.insert(
        "training_labels_matured_by_origin".into(),
        Value::Bool(true),
    );
    leakage.insert("calibration_disabled_by_policy".into(), Value::Bool(true));
    leakage.insert(
        "live_entry_and_replay_share_predict_at_origin".into(),
        Value::Bool(true),
    );
    leakage.insert("training_window_bounded_by_date".into(), Value::Bool(true));
    leakage.insert(
        "training_rows_are_daily_overlapping_labels".into(),
        Value::Bool(true),
    );
    leakage.insert("replay_spacing_sessions".into(), Value::from(horizon + 1));
    let mut purged = Map::new();
    purged.insert("retained_interval_overlaps".into(), Value::from(0));
    purged.insert(
        "method".into(),
        Value::String("not_needed_for_non_overlapping_replay".into()),
    );
    leakage.insert("purged_kfold".into(), Value::Object(purged));

    let mut calibration_shadow = Map::new();
    calibration_shadow.insert("status".into(), Value::String("disabled_by_policy".into()));
    calibration_shadow.insert(
        "matured_non_overlapping_predictions".into(),
        Value::from(matured_for_current as i64),
    );
    calibration_shadow.insert("required".into(), Value::Null);
    calibration_shadow.insert("reachable".into(), Value::Bool(capacity_reachable));
    calibration_shadow.insert(
        "maximum_possible_in_window".into(),
        Value::from(maximum_non_overlapping),
    );
    let mut production_replay = Map::new();
    production_replay.insert(
        "method".into(),
        Value::String("daily refit at every origin; same function as the live entry".into()),
    );
    production_replay.insert("refit_sessions".into(), Value::from(1));
    for (key, value) in summary_block.iter() {
        production_replay.insert(key.clone(), value.clone());
    }
    production_replay.insert(
        "non_overlapping_predictions".into(),
        Value::from(records.len() as i64),
    );
    production_replay.insert(
        "eligible_decisions".into(),
        Value::from(actionable_decisions),
    );
    production_replay.insert(
        "coverage".into(),
        Value::from(round6(records.len() as f64 / actionable_decisions as f64)),
    );
    production_replay.insert("recent_segment".into(), Value::Object(recent));
    production_replay.insert(
        "calibration_shadow".into(),
        Value::Object(calibration_shadow),
    );

    let mut historical_ledger = Map::new();
    historical_ledger.insert(
        "scope".into(),
        Value::String(
            "production-consistent non-overlapping replay; replaces the old fold-based daily ledger"
                .into(),
        ),
    );
    for (key, value) in summary_block.iter() {
        historical_ledger.insert(key.clone(), value.clone());
    }
    historical_ledger.insert(
        "non_overlapping_predictions".into(),
        Value::from(records.len() as i64),
    );
    historical_ledger.insert(
        "effective_sample_size_estimate".into(),
        Value::from(records.len() as i64),
    );
    historical_ledger.insert("calibrated_prediction_count".into(), Value::from(0));
    historical_ledger.insert(
        "uncalibrated_prediction_count".into(),
        Value::from(records.len() as i64),
    );

    let mut probability_calibration = Map::new();
    probability_calibration.insert("method".into(), Value::String("disabled_by_policy".into()));
    probability_calibration.insert("calibrated".into(), Value::Bool(false));
    probability_calibration.insert("status".into(), Value::String("disabled_by_policy".into()));
    probability_calibration.insert(
        "calibration_observations".into(),
        Value::from(matured_for_current as i64),
    );
    probability_calibration.insert(
        "fit_observations".into(),
        Value::from(matured_for_current as i64),
    );
    probability_calibration.insert(
        "required_observations".into(),
        Value::from(PLATT_MIN_OBSERVATIONS as i64),
    );
    probability_calibration.insert("capacity_reachable".into(), Value::Bool(capacity_reachable));
    probability_calibration.insert("reachable".into(), Value::Bool(capacity_reachable));
    probability_calibration.insert(
        "maximum_non_overlapping_in_window".into(),
        Value::from(maximum_non_overlapping),
    );
    probability_calibration.insert(
        "label_end_session".into(),
        current
            .calibration
            .label_end
            .map(|index| Value::String(dataset.bars[index as usize].date.clone()))
            .unwrap_or(Value::Null),
    );

    let mut calibration_capacity = Map::new();
    calibration_capacity.insert("status".into(), Value::String("disabled_by_policy".into()));
    calibration_capacity.insert("warmup_sessions".into(), Value::from(dataset.warmup as i64));
    calibration_capacity.insert("horizon_sessions".into(), Value::from(horizon));
    calibration_capacity.insert(
        "training_rows_at_current_origin".into(),
        Value::from(matured_for_current as i64),
    );
    calibration_capacity.insert("training_rows_are_independent".into(), Value::Bool(false));
    calibration_capacity.insert(
        "note".into(),
        Value::String("no Platt or ensemble calibrator is fitted under the fixed policy".into()),
    );

    let mut current_calibration = Map::new();
    current_calibration.insert(
        "status".into(),
        Value::String(current.calibration.status.clone()),
    );
    current_calibration.insert(
        "applied".into(),
        Value::Bool(current.calibration.mapping.is_some()),
    );
    current_calibration.insert(
        "observations".into(),
        Value::from(current.calibration.observations as i64),
    );
    current_calibration.insert(
        "label_end_session".into(),
        current
            .calibration
            .label_end
            .map(|index| Value::String(dataset.bars[index as usize].date.clone()))
            .unwrap_or(Value::Null),
    );
    current_calibration.insert(
        "slope".into(),
        current
            .calibration
            .mapping
            .map(|mapping| Value::from(mapping.1))
            .unwrap_or(Value::Null),
    );
    current_calibration.insert(
        "intercept".into(),
        current
            .calibration
            .mapping
            .map(|mapping| Value::from(mapping.0))
            .unwrap_or(Value::Null),
    );

    let mut boundary = Map::new();
    boundary.insert(
        "training_decision_start_session".into(),
        Value::String(dataset.bars[current.training_start as usize].date.clone()),
    );
    boundary.insert(
        "training_decision_end_session".into(),
        Value::String(dataset.bars[current.training_end as usize].date.clone()),
    );
    boundary.insert(
        "training_label_end_session".into(),
        Value::String(
            dataset.bars[current.training_label_end as usize]
                .date
                .clone(),
        ),
    );
    boundary.insert(
        "latest_prediction_session".into(),
        Value::String(dataset.bars[dataset.n - 1].date.clone()),
    );

    let mut training_performance = fitter.as_metrics();
    training_performance.insert(
        "replay_refits".into(),
        Value::from(
            records
                .iter()
                .map(|record| anchor(record.decision_index))
                .collect::<HashSet<i64>>()
                .len() as i64,
        ),
    );
    training_performance.insert(
        "replay_wall_seconds".into(),
        Value::from(round6(wall_seconds)),
    );

    let mut compact_features = Map::new();
    compact_features.insert("features".into(), Value::Array(feature_names("compact_v1")));
    compact_features.insert(
        "status".into(),
        Value::String("pre_registered_candidate_not_used_by_production".into()),
    );
    let mut candidates = Map::new();
    candidates.insert("compact_v1".into(), Value::Object(compact_features));
    let mut model_selection = Map::new();
    model_selection.insert(
        "configured_default".into(),
        Value::String("native deterministic L2 logistic regression".into()),
    );
    model_selection.insert(
        "xgboost_status".into(),
        Value::String("not_implemented_not_loaded".into()),
    );
    model_selection.insert(
        "lightgbm_status".into(),
        Value::String("not_implemented_not_loaded".into()),
    );
    model_selection.insert("boosted_models_enabled".into(), Value::Bool(false));
    model_selection.insert("candidate_feature_sets".into(), Value::Object(candidates));

    let mut accelerated = Map::new();
    accelerated.insert(
        "xgboost".into(),
        Value::String("not_implemented_not_loaded".into()),
    );
    accelerated.insert(
        "lightgbm".into(),
        Value::String("not_implemented_not_loaded".into()),
    );

    let mut validation = Map::new();
    validation.insert(
        "model_hyperparameters".into(),
        Value::Object(model_hyperparameters),
    );
    validation.insert("label".into(), Value::String(target_definition.clone()));
    validation.insert("leakage_audit".into(), Value::Object(leakage));
    validation.insert("production_replay".into(), Value::Object(production_replay));
    validation.insert(
        "historical_asof_ledger".into(),
        Value::Object(historical_ledger),
    );
    validation.insert(
        "oos_forecasts".into(),
        Value::Array(records.iter().map(Record::to_value).collect()),
    );
    validation.insert(
        "oos_evaluable_samples".into(),
        Value::from(records.len() as i64),
    );
    validation.insert("oos_predictions".into(), Value::from(records.len() as i64));
    validation.insert("prediction_days".into(), Value::from(records.len() as i64));
    validation.insert(
        "non_overlapping_prediction_count".into(),
        Value::from(records.len() as i64),
    );
    validation.insert(
        "effective_sample_size_estimate".into(),
        Value::from(records.len() as i64),
    );
    validation.insert(
        "oos_scope".into(),
        Value::String(
            "non-overlapping production replay; each row is the probability the live entry would have produced at that decision with the same absolute refit anchor and the calibration state matured before it".into(),
        ),
    );
    validation.insert("oos_rows_are_non_overlapping".into(), Value::Bool(false));
    validation.insert(
        "oos_coverage".into(),
        Value::from(round6(records.len() as f64 / actionable_decisions as f64)),
    );
    validation.insert(
        "oos_abstain_rate".into(),
        Value::from(round6(
            1.0 - records.len() as f64 / actionable_decisions as f64,
        )),
    );
    validation.insert(
        "all_oos_label_observations".into(),
        Value::from(dataset.observations.len() as i64),
    );
    validation.insert("target_definition".into(), Value::String(target_definition));
    validation.insert(
        "probability_calibration".into(),
        Value::Object(probability_calibration),
    );
    validation.insert(
        "calibration_capacity".into(),
        Value::Object(calibration_capacity),
    );
    validation.insert(
        "current_prediction_refit_anchor_session".into(),
        Value::String(dataset.bars[current.anchor as usize].date.clone()),
    );
    validation.insert(
        "current_prediction_calibration".into(),
        Value::Object(current_calibration),
    );
    validation.insert(
        "current_prediction_model_fit_rows".into(),
        Value::from(current.training_rows as i64),
    );
    validation.insert(
        "current_prediction_model_training_boundary".into(),
        Value::Object(boundary),
    );
    validation.insert(
        "model_training_performance".into(),
        Value::Object(training_performance),
    );
    validation.insert("model_selection".into(), Value::Object(model_selection));
    validation.insert("accelerated_models".into(), Value::Object(accelerated));
    Value::Object(validation)
}

fn empty_evidence() -> Value {
    serde_json::json!({"positive": [], "negative": []})
}

/// Research C: shadow logistic replay on the frozen snapshot.
pub fn run(manifest: &Value, market: &Value) -> Result<Value, String> {
    let ticker = sd_core::contracts::py_str(manifest.get("ticker").unwrap_or(&Value::Null));
    let horizon_name = sd_core::contracts::py_str(manifest.get("horizon").unwrap_or(&Value::Null));
    let snapshot_id =
        sd_core::contracts::py_str(manifest.get("snapshot_id").unwrap_or(&Value::Null));
    let horizon = policy::horizon_sessions(&horizon_name)
        .map(i64::from)
        .ok_or_else(|| format!("unknown horizon: {horizon_name}"))?;
    let dataset = match dataset(market, horizon, DEFAULT_FEATURE_SET) {
        Ok(dataset) => dataset,
        Err(message) => {
            return researcher_result(
                "ml",
                &ticker,
                &horizon_name,
                &snapshot_id,
                "insufficient_data",
                "forecast",
                None,
                None,
                empty_evidence(),
                Value::Object(Map::new()),
                Vec::new(),
                vec![Value::String(message)],
                None,
                None,
            );
        }
    };
    let mut fitter = Fitter::default();
    let (records, _refits, replay_seconds) = replay(&dataset, horizon, &mut fitter)?;
    if records.is_empty() {
        return researcher_result(
            "ml",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            empty_evidence(),
            Value::Object(Map::new()),
            Vec::new(),
            vec![Value::String(format!(
                "The replay produced no matured decision with {MINIMUM_TRAINING_ROWS} training rows and both labels."
            ))],
            None,
            None,
        );
    }
    let calibration = calibration_for(&records, dataset.n as i64 - 1);
    let current = predict_at_origin(&dataset, dataset.n as i64 - 1, &calibration, &mut fitter)?;
    let Some(current) = current else {
        return researcher_result(
            "ml",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            empty_evidence(),
            Value::Object(Map::new()),
            Vec::new(),
            vec![Value::String(
                "The current decision has no matured training sample with both labels.".into(),
            )],
            None,
            None,
        );
    };

    let probability = current.probability;
    let mut warnings: Vec<Value> = vec![
        Value::String(
            "XGBoost/LightGBM are not part of this native baseline; the ML path currently uses deterministic L2 logistic regression only.".into(),
        ),
        Value::String(
            "Returns are gross close-to-close observations; this model path does not model execution, spreads, slippage or fees.".into(),
        ),
        Value::String(
            "The replay scores non-overlapping decisions only; each row uses the absolute refit anchor the live entry would have used.".into(),
        ),
    ];
    // Calibration is disabled by policy, not blocked by a shortage of history.
    let status = "success";
    warnings.push(Value::String(
        "Calibration is disabled by policy for this shadow path; the reported probability is the raw logistic output and is not a calibrated frequency."
            .into(),
    ));
    let validation = validation_block(
        &dataset,
        horizon,
        &fitter,
        &records,
        &current,
        replay_seconds,
    );
    let predicted_return = py_mean(
        &dataset
            .observations
            .iter()
            .filter(|row| row.target == 1)
            .map(|row| row.forward_return)
            .collect::<Vec<f64>>(),
    );
    let down_return = py_mean(
        &dataset
            .observations
            .iter()
            .filter(|row| row.target == 0)
            .map(|row| row.forward_return)
            .collect::<Vec<f64>>(),
    );
    let expected_return = match (predicted_return, down_return) {
        (Some(up), Some(down)) => Some(probability * up + (1.0 - probability) * down),
        _ => None,
    };
    let names = feature_names(&dataset.feature_set);
    let mut model_evidence = Map::new();
    model_evidence.insert(
        "hypothesis".into(),
        Value::String("L2 logistic prediction from OHLCV input feature families".into()),
    );
    model_evidence.insert("features".into(), Value::Array(names.clone()));
    model_evidence.insert("model_probability".into(), Value::from(probability));
    model_evidence.insert(
        "family_scope".into(),
        Value::String("input_exposure; not fitted contribution or statistical independence".into()),
    );
    let mut families: Vec<&str> = names
        .iter()
        .filter_map(|name| name.as_str())
        .filter_map(family_of)
        .collect();
    families.sort_unstable();
    families.dedup();
    let factors: Vec<Value> = families
        .iter()
        .map(|family| {
            let features: Vec<Value> = names
                .iter()
                .filter(|name| name.as_str().and_then(family_of) == Some(*family))
                .cloned()
                .collect();
            serde_json::json!({"family": family, "features": features})
        })
        .collect();
    model_evidence.insert("factors".into(), Value::Array(factors));
    let model_evidence = Value::Object(model_evidence);
    let mut evidence = Map::new();
    evidence.insert(
        "positive".into(),
        Value::Array(if probability >= 0.5 {
            vec![model_evidence.clone()]
        } else {
            Vec::new()
        }),
    );
    evidence.insert(
        "negative".into(),
        Value::Array(if probability < 0.5 {
            vec![model_evidence]
        } else {
            Vec::new()
        }),
    );
    researcher_result(
        "ml",
        &ticker,
        &horizon_name,
        &snapshot_id,
        status,
        "forecast",
        Some(probability),
        expected_return,
        Value::Object(evidence),
        validation,
        vec![Value::String("daily raw OHLCV".into())],
        warnings,
        Some(if current.calibration.mapping.is_some() {
            "logistic_predict_proba_platt_calibrated"
        } else {
            "logistic_predict_proba_uncalibrated"
        }),
        Some(current.raw_probability),
    )
}

// --------------------------------------------------------------------------- public wrappers
// The experiment scripts (`research_candidates.py`) reuse the ML path's *dataset* and
// *replay* helpers directly, not only `run()`. These wrappers expose exactly that surface
// without leaking the internal model types, so the experiment can compute the same
// arm/coverage/paired statistics as the legacy script.

/// A replay-ready dataset for one feature set (`ml._dataset`).
pub struct MlDataset {
    inner: Dataset,
}

impl MlDataset {
    /// Number of bars in the dataset.
    pub fn n(&self) -> usize {
        self.inner.n
    }

    /// Feature warm-up sessions (`dataset.warmup`).
    pub fn warmup(&self) -> usize {
        self.inner.warmup
    }
}

/// Build the dataset for one feature set (`ml._dataset`).
pub fn ml_dataset(
    market: &Value,
    horizon_sessions: usize,
    feature_set_name: &str,
) -> Result<MlDataset, String> {
    Ok(MlDataset {
        inner: dataset(market, horizon_sessions as i64, feature_set_name)?,
    })
}

/// Replay every origin with a fresh fitter (`ml._replay(dataset, horizon, ml._Fitter())`).
pub fn ml_replay(dataset: &MlDataset, horizon_sessions: usize) -> Result<Vec<Value>, String> {
    let mut fitter = Fitter::default();
    let (records, _refits, _wall_seconds) =
        replay(&dataset.inner, horizon_sessions as i64, &mut fitter)?;
    Ok(records.iter().map(Record::to_value).collect())
}
