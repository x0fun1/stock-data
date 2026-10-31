//! Reliability accounting for the Research B path.
//!
//! Port of `quant_research/reliability.py`. Two jobs, both about refusing to overstate
//! evidence: bookkeeping (raw vs matured vs independent counts) and grading (paired
//! probability scores against the same-date base rate, with a block-bootstrap interval
//! where the sample allows one). Thresholds live in module constants on purpose: a caller
//! cannot lower them per symbol.

use serde_json::{Map, Value};

use crate::float_sum::python_sum;
use crate::market_data::{anchored_non_overlapping, non_overlapping};
use crate::random::PythonRandom;

/// Legacy-only outputs kept for external callers; the runtime does not route on them.
pub const LEGACY_UNUSED: [&str; 2] = ["qualification", "probabilistic_scores"];
/// Route name for the legacy bucket model.
pub const ROUTE_LEGACY: &str = "legacy_bucket";
/// Route name when the pre-declared floors are not met.
pub const ROUTE_ABSTAINED: &str = "abstained";
/// Probability clip used by log loss and the logit diagnostic.
pub const PROBABILITY_CLIP: f64 = 1e-6;

/// Declared before any evaluation and not adjustable per symbol or per request.
pub fn qualification_thresholds() -> Value {
    let mut logistic = Map::new();
    logistic.insert("train_nonoverlap".into(), Value::from(60));
    logistic.insert("calibration_nonoverlap".into(), Value::from(30));
    logistic.insert("validation_nonoverlap".into(), Value::from(30));
    let mut legacy = Map::new();
    legacy.insert("matured_nonoverlap".into(), Value::from(12));
    legacy.insert("bucket_observations".into(), Value::from(6));
    legacy.insert("validation_predictions".into(), Value::from(8));
    let mut thresholds = Map::new();
    thresholds.insert("logistic".into(), Value::Object(logistic));
    thresholds.insert("legacy_bucket".into(), Value::Object(legacy));
    Value::Object(thresholds)
}

fn logistic_stages() -> [(String, i64); 3] {
    [
        ("train_nonoverlap".to_string(), 60),
        ("calibration_nonoverlap".to_string(), 30),
        ("validation_nonoverlap".to_string(), 30),
    ]
}

fn logistic_shortfall_value() -> Map<String, Value> {
    let mut stages = Map::new();
    let mut required = 0i64;
    for (name, value) in logistic_stages() {
        stages.insert(name, Value::from(value));
        required += value;
    }
    let mut result = Map::new();
    result.insert("stage_requirements".into(), Value::Object(stages));
    result.insert(
        "required_non_overlapping_labels".into(),
        Value::from(required),
    );
    result
}

/// Report how far a study is from hosting a fitted probability model.
pub fn logistic_shortfall(nonoverlap_matured: i64) -> Value {
    let required = logistic_stages()
        .iter()
        .map(|(_, value)| *value)
        .sum::<i64>();
    let available = nonoverlap_matured.max(0);
    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String(
            if available < required {
                "not_applicable_insufficient_samples"
            } else {
                "sample_threshold_met"
            }
            .into(),
        ),
    );
    for (key, value) in logistic_shortfall_value() {
        result.insert(key, value);
    }
    result.insert(
        "available_non_overlapping_labels".into(),
        Value::from(available),
    );
    result.insert(
        "shortfall".into(),
        Value::from((required - available).max(0)),
    );
    result.insert("stages_may_share_observations".into(), Value::Bool(false));
    result.insert(
        "implementation_policy".into(),
        Value::String("deferred_by_fixed_52_week_design".into()),
    );
    result.insert(
        "activation_target".into(),
        Value::String("none_in_current_research_b_architecture".into()),
    );
    result.insert(
        "alternative".into(),
        Value::String("existing_research_c_logistic_path".into()),
    );
    result.insert(
        "activation_note".into(),
        Value::String(
            "A fitted L2 logistic path would need the training, calibration and validation labels separately and at once. A 52-week window cannot supply that at any horizon, so this is not a threshold awaiting more data but a design boundary; the fitted model lives in Research C. This field is retained so historical results stay traceable."
                .into(),
        ),
    );
    Value::Object(result)
}

/// Separate raw, matured, independent and estimated-effective sample counts.
pub fn sample_diagnostics(
    bar_count: i64,
    warmup: i64,
    horizon: i64,
    dates: Option<&[String]>,
) -> Value {
    let decision_dates = (bar_count - warmup).max(0);
    let matured = (decision_dates - horizon).max(0);
    let start = warmup.max(0);
    let end = (bar_count - horizon).max(start);
    let indices: Vec<i64> = (start..end).collect();
    let mut independent_source = "legacy_spacing";
    let independent = match dates {
        Some(dates) if dates.len() as i64 >= bar_count => {
            let (_, provenance) = anchored_non_overlapping(dates, &indices, horizon, 0);
            independent_source = "anchored_phase";
            provenance.get("kept").and_then(Value::as_i64).unwrap_or(0)
        }
        _ => non_overlapping(&indices, horizon).len() as i64,
    };
    let approximation = if horizon != 0 {
        matured as f64 / horizon as f64
    } else {
        matured as f64
    };
    let mut result = Map::new();
    result.insert("bar_count".into(), Value::from(bar_count));
    result.insert("feature_warmup_sessions".into(), Value::from(warmup));
    result.insert("horizon_sessions".into(), Value::from(horizon));
    result.insert("decision_dates".into(), Value::from(decision_dates));
    result.insert("raw_sample_count".into(), Value::from(matured));
    result.insert("matured_sample_count".into(), Value::from(matured));
    result.insert("nonoverlap_sample_count".into(), Value::from(independent));
    result.insert(
        "nonoverlap_count_basis".into(),
        Value::String(independent_source.into()),
    );
    result.insert(
        "effective_sample_size_estimate".into(),
        Value::from(approximation),
    );
    result.insert(
        "effective_sample_size_kind".into(),
        Value::String("approximation_only_raw_over_horizon".into()),
    );
    result.insert(
        "sample_coverage".into(),
        Value::from(if decision_dates != 0 {
            matured as f64 / decision_dates as f64
        } else {
            0.0
        }),
    );
    result.insert(
        "overlapping_observations_are_independent".into(),
        Value::Bool(false),
    );
    result.insert(
        "note".into(),
        Value::String(
            "The non-overlapping count is the conservative figure and is never larger than the effective estimate; raw divided by horizon approximates dependence but is not a measured effective sample size."
                .into(),
        ),
    );
    Value::Object(result)
}

fn clip(probability: f64) -> f64 {
    (1.0 - PROBABILITY_CLIP).min(PROBABILITY_CLIP.max(probability))
}

fn logit(probability: f64) -> f64 {
    let value = clip(probability);
    (value / (1.0 - value)).ln()
}

fn brier(pairs: &[(f64, i64)]) -> Option<f64> {
    if pairs.is_empty() {
        return None;
    }
    let squares: Vec<f64> = pairs
        .iter()
        .map(|(probability, outcome)| {
            (probability - *outcome as f64) * (probability - *outcome as f64)
        })
        .collect();
    Some(python_sum(squares.iter().copied()) / squares.len() as f64)
}

fn log_loss(pairs: &[(f64, i64)]) -> Option<f64> {
    if pairs.is_empty() {
        return None;
    }
    // The legacy loop accumulates sequentially, not through `sum()`; keep that order.
    let mut total = 0.0f64;
    for (probability, outcome) in pairs {
        let value = clip(*probability);
        total -= (if *outcome != 0 { value } else { 1.0 - value }).ln();
    }
    Some(total / pairs.len() as f64)
}

fn calibration_line(pairs: &[(f64, i64)]) -> (Option<f64>, Option<f64>) {
    if pairs.len() < 4 {
        return (None, None);
    }
    let xs: Vec<f64> = pairs
        .iter()
        .map(|(probability, _)| logit(*probability))
        .collect();
    let ys: Vec<f64> = pairs.iter().map(|(_, outcome)| *outcome as f64).collect();
    let mean_x = python_sum(xs.iter().copied()) / xs.len() as f64;
    let mean_y = python_sum(ys.iter().copied()) / ys.len() as f64;
    let variance = python_sum(xs.iter().map(|value| (value - mean_x) * (value - mean_x)));
    if variance <= 1e-12 {
        return (None, None);
    }
    let covariance = python_sum(
        xs.iter()
            .zip(ys.iter())
            .map(|(value, target)| (value - mean_x) * (target - mean_y)),
    );
    let slope = covariance / variance;
    (Some(slope), Some(mean_y - slope * mean_x))
}

fn paired_interval(differences: &[f64], block_length: i64, replicates: usize, seed: u64) -> Value {
    let count = differences.len() as i64;
    let mut result = Map::new();
    if count < 20i64.max(4 * block_length) || block_length < 1 {
        result.insert(
            "status".into(),
            Value::String("insufficient_samples".into()),
        );
        result.insert("observations".into(), Value::from(count));
        result.insert("interval".into(), Value::Null);
        result.insert(
            "method".into(),
            Value::String("circular_moving_block_bootstrap_percentile_mean".into()),
        );
        return Value::Object(result);
    }
    let mut rng = PythonRandom::new(seed);
    let mut means: Vec<f64> = Vec::with_capacity(replicates);
    while means.len() < replicates {
        let mut sample: Vec<f64> = Vec::with_capacity(differences.len() * 2);
        while (sample.len() as i64) < count {
            let start = rng.randrange(count as u64) as usize;
            for offset in 0..block_length as usize {
                sample.push(differences[(start + offset) % differences.len()]);
            }
        }
        let sample = &sample[..differences.len()];
        means.push(python_sum(sample.iter().copied()) / sample.len() as f64);
    }
    means.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let low_index = (0.025 * (means.len() as f64 - 1.0)) as usize;
    let high_index = (0.975 * (means.len() as f64 - 1.0)) as usize;
    result.insert("status".into(), Value::String("exploratory".into()));
    result.insert("observations".into(), Value::from(count));
    result.insert(
        "interval".into(),
        Value::Array(vec![
            Value::from(means[low_index.min(means.len() - 1)]),
            Value::from(means[high_index.min(means.len() - 1)]),
        ]),
    );
    result.insert("block_length".into(), Value::from(block_length));
    result.insert("replicates".into(), Value::from(means.len()));
    result.insert(
        "method".into(),
        Value::String("circular_moving_block_bootstrap_percentile_mean".into()),
    );
    result.insert("coverage_validated".into(), Value::Bool(false));
    Value::Object(result)
}

/// Score the path against the historical base rate on identical dates.
///
/// `predictions` holds `(session_index, probability, base_rate, outcome)`; pairing is by
/// construction because the baseline is the same-date base rate.
pub fn probabilistic_scores(predictions: &[(i64, f64, Option<f64>, i64)], horizon: i64) -> Value {
    let model_pairs: Vec<(f64, i64)> = predictions
        .iter()
        .map(|(_, probability, _, outcome)| (*probability, *outcome))
        .collect();
    let base_pairs: Vec<(f64, i64)> = predictions
        .iter()
        .filter_map(|(_, _, base, outcome)| base.map(|base| (base, *outcome)))
        .collect();
    let common: Vec<(f64, f64, i64)> = predictions
        .iter()
        .filter_map(|(_, probability, base, outcome)| {
            base.map(|base| (*probability, base, *outcome))
        })
        .collect();
    let model_common: Vec<(f64, i64)> = common
        .iter()
        .map(|(probability, _, outcome)| (*probability, *outcome))
        .collect();
    let base_common: Vec<(f64, i64)> = common
        .iter()
        .map(|(_, base, outcome)| (*base, *outcome))
        .collect();
    let brier_model = brier(&model_common);
    let brier_base = brier(&base_common);
    let skill = match (brier_model, brier_base) {
        (Some(model), Some(base)) if base != 0.0 => Some(1.0 - model / base),
        _ => None,
    };
    let (slope, intercept) = calibration_line(&model_common);
    let losses: Vec<f64> = model_common
        .iter()
        .map(|(probability, outcome)| {
            (probability - *outcome as f64) * (probability - *outcome as f64)
        })
        .collect();
    let base_losses: Vec<f64> = base_common
        .iter()
        .map(|(base, outcome)| (base - *outcome as f64) * (base - *outcome as f64))
        .collect();
    let differences: Vec<f64> = losses
        .iter()
        .zip(base_losses.iter())
        .map(|(model, base)| model - base)
        .collect();
    let mut sessions: Vec<i64> = predictions
        .iter()
        .filter_map(|(index, _, base, _)| base.map(|_| *index))
        .collect();
    sessions.sort_unstable();
    sessions.dedup();
    let gaps: Vec<i64> = sessions.windows(2).map(|pair| pair[1] - pair[0]).collect();
    let non_overlapping_sample =
        !gaps.is_empty() && gaps.iter().copied().min().unwrap_or(0) > horizon;
    let block_length = if non_overlapping_sample {
        1
    } else {
        1.max(horizon + 1)
    };
    let paired = paired_interval(&differences, block_length, 500, 0);
    let mean_difference = if differences.is_empty() {
        None
    } else {
        Some(python_sum(differences.iter().copied()) / differences.len() as f64)
    };
    let mut result = Map::new();
    result.insert(
        "brier".into(),
        brier_model.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "base_rate_brier".into(),
        brier_base.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "brier_skill_vs_base_rate".into(),
        skill.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "log_loss".into(),
        log_loss(&model_common)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    result.insert(
        "base_rate_log_loss".into(),
        log_loss(&base_common)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    result.insert(
        "calibration_slope".into(),
        slope.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "calibration_intercept".into(),
        intercept.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "calibration_slope_method".into(),
        Value::String("ols_of_outcome_on_logit_probability_diagnostic_only".into()),
    );
    result.insert(
        "calibration_slope_is_logistic_calibration_slope".into(),
        Value::Bool(false),
    );
    result.insert("paired_common_samples".into(), Value::from(common.len()));
    result.insert(
        "brier_difference_model_minus_base".into(),
        mean_difference.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "brier_difference_interval".into(),
        paired.get("interval").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "brier_difference_interval_status".into(),
        paired.get("status").cloned().unwrap_or(Value::Null),
    );
    result.insert("block_length".into(), Value::from(block_length));
    result.insert(
        "block_length_basis".into(),
        Value::String(
            if non_overlapping_sample {
                "one_record_because_labels_do_not_overlap"
            } else {
                "horizon_plus_one_because_adjacent_labels_overlap"
            }
            .into(),
        ),
    );
    result.insert(
        "minimum_session_gap".into(),
        gaps.iter()
            .copied()
            .min()
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    result.insert(
        "baseline".into(),
        Value::String("same_date_historical_base_rate".into()),
    );
    result.insert("comparison_sample_matched".into(), Value::Bool(true));
    result.insert("scored_predictions".into(), Value::from(model_pairs.len()));
    result.insert(
        "base_rate_scored_predictions".into(),
        Value::from(base_pairs.len()),
    );
    Value::Object(result)
}

/// Decide the route from pre-declared thresholds only.
pub fn qualification(
    nonoverlap_matured: i64,
    bucket_observations: i64,
    buckets_available: i64,
    validation_predictions: i64,
) -> Value {
    let checks = [
        ("matured_nonoverlap", nonoverlap_matured, 12i64),
        ("bucket_observations", bucket_observations, 6),
        ("validation_predictions", validation_predictions, 8),
        ("buckets_available", buckets_available, 2),
    ];
    let failed: Vec<&str> = checks
        .iter()
        .filter(|(_, observed, floor)| *observed < *floor)
        .map(|(name, _, _)| *name)
        .collect();
    let mut check_map = Map::new();
    for (name, observed, floor) in checks {
        let mut row = Map::new();
        row.insert("observed".into(), Value::from(observed));
        row.insert("required".into(), Value::from(floor));
        row.insert("passed".into(), Value::Bool(observed >= floor));
        check_map.insert(name.to_string(), Value::Object(row));
    }
    let mut withheld = Map::new();
    withheld.insert("logistic".into(), logistic_shortfall(nonoverlap_matured));
    let mut result = Map::new();
    result.insert(
        "route".into(),
        Value::String(
            if failed.is_empty() {
                ROUTE_LEGACY
            } else {
                ROUTE_ABSTAINED
            }
            .into(),
        ),
    );
    result.insert(
        "route_reason".into(),
        Value::String(if failed.is_empty() {
            "legacy_bucket_qualified".to_string()
        } else {
            format!("insufficient_matured_evidence:{}", failed.join(","))
        }),
    );
    result.insert("checks".into(), Value::Object(check_map));
    result.insert("withheld_models".into(), Value::Object(withheld));
    result.insert("thresholds_declared_in_advance".into(), Value::Bool(true));
    result.insert(
        "thresholds_adjustable_per_symbol".into(),
        Value::Bool(false),
    );
    Value::Object(result)
}
