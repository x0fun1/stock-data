//! Research B: fixed OHLCV factor diagnostics, redundancy control and calibration.
//!
//! Port of `quant_research/researchers/factor.py` (the shadow factor path). The path is a
//! **shadow candidate**: `production_eligible=false`, `shadow_only=true`, and a numeric
//! estimate that fails the path's own declared floors is moved to `unqualified_estimate`
//! and withheld instead of being reported as a probability.
//!
//! Sampling: the holdout and bucket-defining ledgers use
//! [`sd_core::market_data::anchored_non_overlapping`] (absolute weekday phase), while the
//! selection segment keeps legacy spacing because its density ceiling equals the declared
//! label floor. Both facts are disclosed in `validation.sampling`.

use serde_json::{Map, Value};

use crate::math_utils::{
    benjamini_yekutieli, circular_block_bootstrap_spearman, circular_shift_spearman_p_value,
    pearson, quantile, sample_std, spearman,
};
use crate::result::researcher_result;
use crate::selection::{
    multiple_testing_disclosure, overlapping_ic, shift_p_values, stability_verdict,
};
use sd_core::float_sum::python_sum;
use sd_core::interfaces::Bar;
use sd_core::market_data::{
    anchored_non_overlapping, forward_returns, market_bars, non_overlapping,
};
use sd_core::policy::HORIZONS;
use sd_core::reliability::{probabilistic_scores, sample_diagnostics, ROUTE_ABSTAINED};

/// Minimum matured observations per calibration bucket.
pub const MINIMUM_FACTOR_BUCKET_OBSERVATIONS: i64 = 6;
/// Minimum non-overlapping labels in the selection segment.
pub const MINIMUM_SELECTION_LABELS: i64 = 10;
/// Prior weight (in observations) of the bucket-frequency shrinkage.
pub const SHRINKAGE_PRIOR_OBSERVATIONS: i64 = 12;
/// Fraction of the window reserved for selection.
pub const SELECTION_FRACTION: f64 = 0.5;
/// Sessions of feature warm-up.
pub const FEATURE_WARMUP_SESSIONS: i64 = 60;
/// Minimum absolute selection Rank IC.
pub const MINIMUM_ABS_SELECTION_IC: f64 = 0.02;
/// Maximum number of selected factors (legacy profile).
pub const MAX_FACTORS: i64 = 5;
/// Maximum selected factors per family (legacy profile).
pub const MAX_FACTORS_PER_FAMILY: i64 = 2;
/// Correlation above which a candidate is redundant.
pub const MAX_ABS_FACTOR_CORRELATION: f64 = 0.85;
/// Minimum sign consistency for the dependency-aware rule.
pub const MINIMUM_SIGN_CONSISTENCY: f64 = 2.0 / 3.0;
/// Frozen model version.
pub const MODEL_VERSION: &str = "factor_restricted_two_honest_v1";
/// Production selection profile.
pub const PRODUCTION_SELECTION_PROFILE: &str = "restricted";
/// Production bucket mode.
pub const PRODUCTION_BUCKET_MODE: &str = "two";

/// Declared selection profiles.
pub struct SelectionProfile {
    /// Profile name.
    pub name: &'static str,
    /// Maximum selected factors.
    pub max_factors: i64,
    /// Maximum selected factors per family.
    pub max_factors_per_family: i64,
    /// Selection metric.
    pub metric: &'static str,
}

/// `legacy` = non-overlapping point estimate; `restricted` = dependency-aware.
pub const SELECTION_PROFILES: &[SelectionProfile] = &[
    SelectionProfile {
        name: "legacy",
        max_factors: 5,
        max_factors_per_family: 2,
        metric: "non_overlapping_point",
    },
    SelectionProfile {
        name: "restricted",
        max_factors: 3,
        max_factors_per_family: 1,
        metric: "overlapping_block",
    },
];

fn select_profile(name: &str) -> Option<&'static SelectionProfile> {
    SELECTION_PROFILES
        .iter()
        .find(|profile| profile.name == name)
}

fn python_round(value: f64) -> i64 {
    let floor = value.floor();
    let fraction = value - floor;
    if fraction > 0.5 || (fraction == 0.5 && (floor as i64) % 2 != 0) {
        floor as i64 + 1
    } else {
        floor as i64
    }
}

fn py_mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(python_sum(values.iter().copied()) / values.len() as f64)
}

fn value_of(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

fn window_return(values: &[f64], index: usize, window: usize) -> Option<f64> {
    if index >= window && values[index - window] > 0.0 {
        Some(values[index] / values[index - window] - 1.0)
    } else {
        None
    }
}

type FactorValues = Vec<(&'static str, &'static str, &'static str, Vec<Option<f64>>)>;

fn build_factors(bars: &[Bar]) -> FactorValues {
    let count = bars.len();
    let close: Vec<f64> = bars.iter().map(|bar| bar.close).collect();
    let open: Vec<f64> = bars.iter().map(|bar| bar.open).collect();
    let high: Vec<f64> = bars.iter().map(|bar| bar.high).collect();
    let low: Vec<f64> = bars.iter().map(|bar| bar.low).collect();
    let volume: Vec<f64> = bars.iter().map(|bar| bar.volume).collect();
    let mut factors: FactorValues = Vec::new();
    for window in [5usize, 20, 60] {
        let values = (0..count)
            .map(|index| window_return(&close, index, window))
            .collect();
        let name: &'static str = match window {
            5 => "momentum_5",
            20 => "momentum_20",
            _ => "momentum_60",
        };
        let formula: &'static str = match window {
            5 => "close[t] / close[t-5] - 1",
            20 => "close[t] / close[t-20] - 1",
            _ => "close[t] / close[t-60] - 1",
        };
        factors.push((name, "momentum", formula, values));
    }
    factors.push((
        "reversal_1",
        "reversal",
        "close[t] / close[t-1] - 1",
        (0..count)
            .map(|index| window_return(&close, index, 1))
            .collect(),
    ));
    let mut trend: Vec<Option<f64>> = vec![None; count];
    let mut vol20: Vec<Option<f64>> = vec![None; count];
    let mut volume_z: Vec<Option<f64>> = vec![None; count];
    let mut range_pos: Vec<Option<f64>> = vec![None; count];
    let mut close_location: Vec<Option<f64>> = vec![None; count];
    let mut gap: Vec<Option<f64>> = vec![None; count];
    let mut volume_return_corr: Vec<Option<f64>> = vec![None; count];
    let returns: Vec<Option<f64>> = (0..count)
        .map(|index| window_return(&close, index, 1))
        .collect();
    for index in 0..count {
        if index >= 19 {
            let window = &close[index - 19..=index];
            let average = py_mean(window);
            trend[index] = average.and_then(|average| {
                if average != 0.0 {
                    Some(close[index] / average - 1.0)
                } else {
                    None
                }
            });
            let logs: Vec<f64> = (index - 18..=index)
                .filter(|position| close[position - 1] > 0.0)
                .map(|position| (close[position] / close[position - 1]).ln())
                .collect();
            vol20[index] = sample_std(&logs);
            let volume_window = &volume[index - 19..=index];
            let volume_std = sample_std(volume_window);
            volume_z[index] = match volume_std {
                Some(scale) if scale != 0.0 => {
                    let mean = py_mean(volume_window).unwrap_or(0.0);
                    Some((volume[index] - mean) / scale)
                }
                _ => Some(0.0),
            };
            let lowest = low[index - 19..=index]
                .iter()
                .copied()
                .fold(f64::INFINITY, f64::min);
            let highest = high[index - 19..=index]
                .iter()
                .copied()
                .fold(f64::NEG_INFINITY, f64::max);
            range_pos[index] = if highest > lowest {
                Some((close[index] - lowest) / (highest - lowest))
            } else {
                Some(0.5)
            };
            let rr: Vec<Option<f64>> = (index - 18..=index)
                .map(|position| returns[position])
                .collect();
            let vv: Vec<f64> = (index - 18..=index)
                .filter(|position| volume[position - 1] > 0.0)
                .map(|position| volume[position] / volume[position - 1] - 1.0)
                .collect();
            if rr.len() == vv.len() && rr.iter().all(Option::is_some) {
                let xs: Vec<f64> = rr.iter().map(|value| value.unwrap_or(0.0)).collect();
                volume_return_corr[index] = pearson(&xs, &vv);
            }
        }
        let day_range = high[index] - low[index];
        if day_range > 0.0 {
            close_location[index] =
                Some((2.0 * close[index] - high[index] - low[index]) / day_range);
        }
        if index > 0 && close[index - 1] > 0.0 {
            gap[index] = Some(open[index] / close[index - 1] - 1.0);
        }
    }
    factors.push((
        "trend_sma20",
        "trend",
        "close[t] / mean(close[t-19:t]) - 1",
        trend,
    ));
    factors.push((
        "realized_volatility_20",
        "volatility",
        "sample_std(log(close[j]/close[j-1]), j=t-18..t)",
        vol20,
    ));
    factors.push((
        "volume_z20",
        "volume",
        "(volume[t] - mean(volume[t-19:t])) / sample_std(volume[t-19:t])",
        volume_z,
    ));
    factors.push((
        "range_position_20",
        "trend",
        "(close[t] - rolling_low20) / (rolling_high20 - rolling_low20)",
        range_pos,
    ));
    factors.push((
        "close_location",
        "trend",
        "(2*close[t] - high[t] - low[t]) / (high[t] - low[t])",
        close_location,
    ));
    factors.push(("overnight_gap", "momentum", "open[t] / close[t-1] - 1", gap));
    factors.push((
        "volume_return_corr20",
        "volume",
        "corr(return, volume_change) over 20 sessions",
        volume_return_corr,
    ));
    factors
}

fn valid_pairs(
    values: &[Option<f64>],
    labels: &[Option<f64>],
    indices: &[i64],
) -> (Vec<f64>, Vec<f64>, Vec<i64>) {
    let mut x = Vec::new();
    let mut y = Vec::new();
    let mut used = Vec::new();
    for index in indices {
        if *index < 0 {
            continue;
        }
        let position = *index as usize;
        if position >= values.len() || position >= labels.len() {
            continue;
        }
        let (Some(value), Some(label)) = (values[position], labels[position]) else {
            continue;
        };
        if !value.is_finite() || !label.is_finite() {
            continue;
        }
        x.push(value);
        y.push(label);
        used.push(*index);
    }
    (x, y, used)
}

fn factor_metrics(values: &[Option<f64>], labels: &[Option<f64>], indices: &[i64]) -> Value {
    let (x, y, used) = valid_pairs(values, labels, indices);
    let rank_ic = spearman(&x, &y);
    let pearson_ic = pearson(&x, &y);
    let q20 = quantile(&x, 0.2);
    let q80 = quantile(&x, 0.8);
    let mut low_returns: Vec<f64> = Vec::new();
    let mut high_returns: Vec<f64> = Vec::new();
    for (value, target) in x.iter().zip(y.iter()) {
        if q20.map(|cut| *value <= cut).unwrap_or(false) {
            low_returns.push(*target);
        }
        if q80.map(|cut| *value >= cut).unwrap_or(false) {
            high_returns.push(*target);
        }
    }
    let spread = match (py_mean(&high_returns), py_mean(&low_returns)) {
        (Some(high), Some(low)) if !high_returns.is_empty() && !low_returns.is_empty() => {
            Some(high - low)
        }
        _ => None,
    };
    let mut chunks: Vec<f64> = Vec::new();
    if used.len() >= 40 {
        let size = 20.max(used.len() / 4);
        let mut start = 0usize;
        while start < used.len() {
            let part: Vec<i64> = used[start..(start + size).min(used.len())].to_vec();
            let (px, py, _) = valid_pairs(values, labels, &part);
            if let Some(ic) = spearman(&px, &py) {
                chunks.push(ic);
            }
            start += size;
        }
    }
    let ic_mean = if chunks.is_empty() {
        rank_ic
    } else {
        py_mean(&chunks)
    };
    let ic_std = if chunks.len() > 1 {
        sample_std(&chunks)
    } else {
        None
    };
    let icir = match (ic_mean, ic_std) {
        (Some(mean), Some(std)) if std != 0.0 => Some(mean / std),
        _ => None,
    };
    let turnover = if x.len() > 1 {
        q80.map(|cut| {
            let mut flips = 0i64;
            for position in 1..x.len() {
                if (x[position] >= cut) != (x[position - 1] >= cut) {
                    flips += 1;
                }
            }
            flips as f64 / (x.len() - 1) as f64
        })
    } else {
        None
    };
    let coverage = if indices.is_empty() {
        0.0
    } else {
        used.len() as f64 / indices.len() as f64
    };
    let mut map = Map::new();
    map.insert("rank_ic".into(), value_of(rank_ic));
    map.insert("pearson_ic".into(), value_of(pearson_ic));
    map.insert("ic_mean".into(), value_of(ic_mean));
    map.insert("ic_std".into(), value_of(ic_std));
    map.insert("icir".into(), value_of(icir));
    map.insert("top_bottom_quantile_spread".into(), value_of(spread));
    map.insert("coverage".into(), Value::from(coverage));
    map.insert("sample_size".into(), Value::from(used.len()));
    map.insert("top_quantile_turnover".into(), value_of(turnover));
    Value::Object(map)
}

fn oos_factor_inference(
    factors: &FactorValues,
    labels: &[Option<f64>],
    indices: &[i64],
    bars: &[Bar],
    horizon: i64,
) -> Value {
    let mut rows: Map<String, Value> = Map::new();
    let mut raw_p_values: Vec<(String, Option<f64>)> = Vec::new();
    for (name, family, formula, values) in factors {
        let (x, y, used) = valid_pairs(values, labels, indices);
        let observations = used.len();
        let block_length = if observations == 0 {
            horizon
        } else {
            horizon.max(python_round((observations as f64).powf(1.0 / 3.0)))
        };
        let bootstrap =
            circular_block_bootstrap_spearman(&x, &y, block_length, 300, 1701 + rows.len() as u64);
        let shift_test = circular_shift_spearman_p_value(&x, &y, block_length);
        let split = used.len() / 2;
        let first_ic = if split >= 10 {
            spearman(&x[..split], &y[..split])
        } else {
            None
        };
        let second_ic = if used.len() - split >= 10 {
            spearman(&x[split..], &y[split..])
        } else {
            None
        };
        let p_value = shift_test.get("p_value").and_then(Value::as_f64);
        raw_p_values.push(((*name).to_string(), p_value));
        let mut partwise = Map::new();
        partwise.insert("first_half".into(), value_of(first_ic));
        partwise.insert("second_half".into(), value_of(second_ic));
        let direction_consistent = match (first_ic, second_ic) {
            (Some(first), Some(second)) => Some((first >= 0.0) == (second >= 0.0)),
            _ => None,
        };
        let mut row = Map::new();
        row.insert("family".into(), Value::String((*family).into()));
        row.insert("formula".into(), Value::String((*formula).into()));
        row.insert("oos_time_series_ic".into(), value_of(spearman(&x, &y)));
        row.insert("observations".into(), Value::from(observations));
        row.insert(
            "data_start".into(),
            used.first()
                .and_then(|index| bars.get(*index as usize))
                .map(|bar| Value::String(bar.date.clone()))
                .unwrap_or(Value::Null),
        );
        row.insert(
            "data_end".into(),
            used.last()
                .and_then(|index| bars.get((*index + horizon) as usize))
                .map(|bar| Value::String(bar.date.clone()))
                .unwrap_or(Value::Null),
        );
        row.insert("block_bootstrap".into(), bootstrap);
        row.insert("null_test".into(), shift_test);
        row.insert("partwise_ic".into(), Value::Object(partwise));
        row.insert(
            "direction_consistent_across_halves".into(),
            direction_consistent.map(Value::Bool).unwrap_or(Value::Null),
        );
        row.insert("p_value".into(), value_of(p_value));
        row.insert("p_value_adjusted".into(), Value::Null);
        rows.insert((*name).to_string(), Value::Object(row));
    }
    let adjusted = benjamini_yekutieli(&raw_p_values, Some(factors.len()));
    for (name, value) in adjusted {
        if let Some(Value::Object(row)) = rows.get_mut(&name) {
            row.insert("p_value_adjusted".into(), value_of(value));
        }
    }
    let available_tests = raw_p_values
        .iter()
        .filter(|(_, value)| value.is_some())
        .count();
    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String(
            if available_tests != 0 {
                "exploratory"
            } else {
                "insufficient_samples"
            }
            .into(),
        ),
    );
    result.insert(
        "universe_type".into(),
        Value::String("single_security_time_series".into()),
    );
    result.insert(
        "method".into(),
        Value::String(
            "OOS Spearman IC; circular moving-block bootstrap percentile interval; circular-shift null diagnostic; Benjamini-Yekutieli FDR"
                .into(),
        ),
    );
    result.insert(
        "p_value_method".into(),
        Value::String("circular_shift_test_stationarity_assumption".into()),
    );
    result.insert(
        "p_adjustment_method".into(),
        Value::String(
            "Benjamini-Yekutieli FDR; conservative under arbitrary test dependence".into(),
        ),
    );
    result.insert("trials".into(), Value::from(factors.len()));
    result.insert("tests_with_p_value".into(), Value::from(available_tests));
    result.insert("sample_count".into(), Value::from(indices.len()));
    result.insert("horizon_sessions".into(), Value::from(horizon));
    result.insert(
        "data_start".into(),
        indices
            .first()
            .and_then(|index| bars.get(*index as usize))
            .map(|bar| Value::String(bar.date.clone()))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "data_end".into(),
        indices
            .last()
            .map(|index| ((*index + horizon) as usize).min(bars.len().saturating_sub(1)))
            .and_then(|index| bars.get(index))
            .map(|bar| Value::String(bar.date.clone()))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "factor_selection_used_this_window".into(),
        Value::Bool(false),
    );
    result.insert(
        "stationarity_assumption_validated".into(),
        Value::Bool(false),
    );
    result.insert("bootstrap_coverage_validated".into(), Value::Bool(false));
    result.insert(
        "market_regime_stability".into(),
        Value::String("not_assessed_no_frozen_regime_definition".into()),
    );
    result.insert("factors".into(), Value::Object(rows));
    Value::Object(result)
}

fn empty_oos_forecasts(
    indices: &[i64],
    bars: &[Bar],
    labels: &[Option<f64>],
    horizon: i64,
) -> Value {
    let target_definition = format!("close[t+{horizon}] / close[t] - 1 > 0");
    let mut rows: Vec<Value> = Vec::new();
    for index in indices {
        let position = *index as usize;
        let Some(label) = labels.get(position).copied().flatten() else {
            continue;
        };
        let mut row = Map::new();
        row.insert(
            "decision_session".into(),
            Value::String(bars[position].date.clone()),
        );
        row.insert(
            "label_end_session".into(),
            Value::String(bars[position + horizon as usize].date.clone()),
        );
        row.insert("target".into(), Value::from(i64::from(label > 0.0)));
        row.insert("probability".into(), Value::Null);
        row.insert("training_label_end_session".into(), Value::Null);
        row.insert(
            "target_definition".into(),
            Value::String(target_definition.clone()),
        );
        rows.push(Value::Object(row));
    }
    Value::Array(rows)
}

fn bucket_keys(mode: &str) -> Vec<&'static str> {
    if mode == "two" {
        vec!["low", "high"]
    } else {
        vec!["low", "mid", "high"]
    }
}

fn bucket_for_score(
    score: Option<f64>,
    low_cut: Option<f64>,
    high_cut: Option<f64>,
    mode: &str,
) -> Option<&'static str> {
    let score = score?;
    let low_cut = low_cut?;
    if mode == "two" {
        return Some(if score < low_cut { "low" } else { "high" });
    }
    let high_cut = high_cut?;
    if score <= low_cut {
        Some("low")
    } else if score >= high_cut {
        Some("high")
    } else {
        Some("mid")
    }
}

fn bucket_cuts(scores: &[f64], mode: &str) -> (Option<f64>, Option<f64>) {
    if mode == "two" {
        return (quantile(scores, 0.5), None);
    }
    let low_cut = quantile(scores, 1.0 / 3.0);
    let high_cut = quantile(scores, 2.0 / 3.0);
    if let (Some(low), Some(high)) = (low_cut, high_cut) {
        if low < high {
            return (low_cut, high_cut);
        }
    }
    let center = py_mean(scores);
    let scale = sample_std(scores);
    match (center, scale) {
        (Some(center), Some(scale)) if scale != 0.0 => {
            (Some(center - 0.25 * scale), Some(center + 0.25 * scale))
        }
        _ => (None, None),
    }
}

fn shrunk_probability(samples: &[f64], base_rate: Option<f64>, prior: i64) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    let raw = samples.iter().filter(|value| **value > 0.0).count() as f64 / samples.len() as f64;
    match base_rate {
        Some(base) if prior > 0 => Some(
            (samples.len() as f64 * raw + prior as f64 * base)
                / (samples.len() as f64 + prior as f64),
        ),
        _ => Some(raw),
    }
}

fn shrunk_mean(samples: &[f64], overall: Option<f64>, prior: i64) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    let center = py_mean(samples)?;
    match overall {
        Some(overall) if prior > 0 => Some(
            (samples.len() as f64 * center + prior as f64 * overall)
                / (samples.len() as f64 + prior as f64),
        ),
        _ => Some(center),
    }
}

fn spaced_label_count(first: i64, last: i64, horizon: i64) -> i64 {
    if last >= first {
        (last - first) / (horizon + 1) + 1
    } else {
        0
    }
}

fn selection_label_capacity(bar_count: i64, horizon: i64) -> i64 {
    let selection_end = (bar_count as f64 * SELECTION_FRACTION) as i64;
    spaced_label_count(
        FEATURE_WARMUP_SESSIONS,
        selection_end - horizon - 1,
        horizon,
    )
}

fn matured_label_capacity(bar_count: i64, horizon: i64) -> i64 {
    spaced_label_count(FEATURE_WARMUP_SESSIONS, bar_count - 1 - horizon, horizon)
}

fn sample_feasibility(bar_count: i64, horizon: i64, bucket_mode: &str) -> Map<String, Value> {
    let required = bucket_keys(bucket_mode).len() as i64 * MINIMUM_FACTOR_BUCKET_OBSERVATIONS;
    let ready = |size: i64| {
        selection_label_capacity(size, horizon) >= MINIMUM_SELECTION_LABELS
            && matured_label_capacity(size, horizon) >= required
    };
    let mut low = 0i64;
    let mut high = 256.max(bar_count);
    while !ready(high) {
        high *= 2;
    }
    while low < high {
        let middle = (low + high) / 2;
        if ready(middle) {
            high = middle;
        } else {
            low = middle + 1;
        }
    }
    let capacity = matured_label_capacity(bar_count, horizon);
    let selection_capacity = selection_label_capacity(bar_count, horizon);
    let mut map = Map::new();
    map.insert(
        "status".into(),
        Value::String(
            if ready(bar_count) {
                "budget_possible_not_guaranteed_by_factor_scores"
            } else {
                "capacity_below_52_week_minimum"
            }
            .into(),
        ),
    );
    map.insert(
        "window_standard".into(),
        Value::String("52_weeks_of_daily_bars".into()),
    );
    map.insert(
        "minimum_bucket_observations".into(),
        Value::from(MINIMUM_FACTOR_BUCKET_OBSERVATIONS),
    );
    map.insert(
        "required_calibration_observations_for_predeclared_buckets".into(),
        Value::from(required),
    );
    map.insert("bucket_mode".into(), Value::String(bucket_mode.into()));
    map.insert(
        "minimum_selection_labels".into(),
        Value::from(MINIMUM_SELECTION_LABELS),
    );
    map.insert(
        "selection_label_capacity".into(),
        Value::from(selection_capacity),
    );
    map.insert(
        "max_matured_non_overlapping_label_capacity".into(),
        Value::from(capacity),
    );
    map.insert("calibration_available".into(), Value::from(0));
    map.insert(
        "calibration_observations_with_complete_factor_scores".into(),
        Value::from(0),
    );
    map.insert(
        "calibration_bucket_counts".into(),
        serde_json::json!({"low": 0, "mid": 0, "high": 0}),
    );
    map.insert(
        "all_current_buckets_meet_minimum".into(),
        Value::Bool(false),
    );
    map.insert("estimated_minimum_daily_bars".into(), Value::from(low));
    map.insert(
        "estimated_minimum_calendar_weeks".into(),
        Value::from((low as f64 / 5.0 * 10.0).round() / 10.0),
    );
    map.insert(
        "additional_daily_bars_at_current_history_length".into(),
        Value::from((low - bar_count).max(0)),
    );
    map.insert(
        "estimate_assumptions".into(),
        Value::String(
            "52-week standard: fixed chronological selection segment, 60-session feature warm-up, H+1-session label spacing, labels matured by the decision bar, and the predeclared bucket count times 6 observations; score ties and missing factors can increase the requirement"
                .into(),
        ),
    );
    map.insert("requested_window_preserved".into(), Value::Bool(true));
    map
}

/// Options of the Research B run (the live path passes the defaults).
pub struct FactorOptions<'a> {
    /// Reproducible rule comparisons only; never changes the evaluation boundary.
    pub selection_override: Option<&'a str>,
    /// `anchored` (production) or `legacy`.
    pub sampling_mode: &'a str,
    /// `legacy` or `restricted`.
    pub selection_profile: Option<&'a str>,
    /// `two` (production) or `three`.
    pub bucket_mode: &'a str,
}

impl Default for FactorOptions<'_> {
    fn default() -> Self {
        Self {
            selection_override: None,
            sampling_mode: "anchored",
            selection_profile: None,
            bucket_mode: PRODUCTION_BUCKET_MODE,
        }
    }
}

fn requested_horizon(horizon_name: &str) -> Result<i64, String> {
    HORIZONS
        .iter()
        .find(|(name, _)| *name == horizon_name)
        .map(|(_, sessions)| i64::from(*sessions))
        .ok_or_else(|| format!("unsupported horizon: {horizon_name}"))
}

fn counts_repr(keys: &[&str], counts: &[usize]) -> String {
    let parts: Vec<String> = keys
        .iter()
        .zip(counts.iter())
        .map(|(key, count)| format!("'{key}': {count}"))
        .collect();
    format!("{{{}}}", parts.join(", "))
}

fn strings_from_bars(bars: &[Bar]) -> Vec<String> {
    bars.iter().map(|bar| bar.date.clone()).collect()
}

/// One ranked factor candidate.
struct Candidate {
    name: String,
    family: String,
    formula: String,
    values: Vec<Option<f64>>,
    metrics: Value,
    score: f64,
}

/// Run Research B with explicit options (defaults reproduce production).
pub fn run_with_options(
    manifest: &Value,
    market: &Value,
    options: &FactorOptions,
) -> Result<Value, String> {
    if options.bucket_mode != "two" && options.bucket_mode != "three" {
        return Err("bucket_mode must be one of ('three', 'two')".into());
    }
    let profile_name = options
        .selection_profile
        .unwrap_or(PRODUCTION_SELECTION_PROFILE);
    let profile = select_profile(profile_name)
        .ok_or_else(|| "selection_profile must be one of ['legacy', 'restricted']".to_string())?;
    let max_factors = profile.max_factors;
    let max_factors_per_family = profile.max_factors_per_family;
    let ticker = sd_core::contracts::py_str(manifest.get("ticker").unwrap_or(&Value::Null));
    let horizon_name = sd_core::contracts::py_str(manifest.get("horizon").unwrap_or(&Value::Null));
    let snapshot_id =
        sd_core::contracts::py_str(manifest.get("snapshot_id").unwrap_or(&Value::Null));
    let requested = requested_horizon(&horizon_name)?;
    let bars = market_bars(market, false).map_err(|error| error.message().to_string())?;
    let close: Vec<f64> = bars.iter().map(|bar| bar.close).collect();
    let factors = build_factors(&bars);
    let count = bars.len();
    let mut feasibility = sample_feasibility(count as i64, requested, options.bucket_mode);
    let train_end = (count as f64 * SELECTION_FRACTION) as usize;
    let holdout_start = train_end;
    let bar_dates = strings_from_bars(&bars);

    let spaced = |lo: usize, hi: usize| -> (Vec<i64>, Value) {
        let indices: Vec<i64> = (lo as i64..hi as i64).collect();
        if options.sampling_mode == "legacy" {
            let chosen = non_overlapping(&indices, requested);
            let mut provenance = Map::new();
            provenance.insert("method".into(), Value::String("legacy_spacing".into()));
            provenance.insert(
                "candidate_phase_window_independent".into(),
                Value::Bool(false),
            );
            provenance.insert(
                "final_membership_window_independent".into(),
                Value::Bool(false),
            );
            provenance.insert("kept".into(), Value::from(chosen.len()));
            provenance.insert(
                "bounds".into(),
                Value::Array(vec![Value::from(lo as i64), Value::from(hi as i64)]),
            );
            return (chosen, Value::Object(provenance));
        }
        anchored_non_overlapping(&bar_dates, &indices, requested, 0)
    };

    let holdout_hi = count.saturating_sub(requested as usize);
    let (holdout_indices, holdout_sampling) = spaced(holdout_start, holdout_hi);

    let mut labels_by_horizon: Vec<(i64, Vec<Option<f64>>)> = Vec::new();
    for horizon in [1i64, 5, 20] {
        labels_by_horizon.push((horizon, forward_returns(&close, horizon as usize)));
    }
    let select_labels = labels_by_horizon
        .iter()
        .find(|(horizon, _)| *horizon == requested)
        .map(|(_, labels)| labels.clone())
        .unwrap_or_else(|| vec![None; count]);
    let oos_inference =
        oos_factor_inference(&factors, &select_labels, &holdout_indices, &bars, requested);

    let mut decay = Map::new();
    for (name, family, formula, values) in &factors {
        let mut horizons = Map::new();
        for horizon in [1i64, 5, 20] {
            let labels = labels_by_horizon
                .iter()
                .find(|(candidate, _)| *candidate == horizon)
                .map(|(_, labels)| labels.clone())
                .unwrap_or_else(|| vec![None; count]);
            let eligible: Vec<i64> = (FEATURE_WARMUP_SESSIONS..holdout_start as i64)
                .filter(|index| index + horizon < holdout_start as i64)
                .collect();
            let indices = if options.sampling_mode == "legacy" {
                non_overlapping(&eligible, horizon)
            } else {
                anchored_non_overlapping(&bar_dates, &eligible, horizon, 0).0
            };
            horizons.insert(
                format!("{horizon}D"),
                factor_metrics(values, &labels, &indices),
            );
        }
        let mut entry = Map::new();
        entry.insert("family".into(), Value::String((*family).into()));
        entry.insert("formula".into(), Value::String((*formula).into()));
        entry.insert("horizons".into(), Value::Object(horizons));
        decay.insert((*name).to_string(), Value::Object(entry));
    }

    let selection_metric = options
        .selection_override
        .map(str::to_string)
        .or_else(|| {
            if profile.metric.is_empty() {
                None
            } else {
                Some(profile.metric.to_string())
            }
        })
        .unwrap_or_else(|| "non_overlapping_point".to_string());
    let selection_pool: Vec<i64> = (FEATURE_WARMUP_SESSIONS..train_end as i64)
        .filter(|index| index + requested < train_end as i64)
        .collect();
    let anchored_selection = anchored_non_overlapping(&bar_dates, &selection_pool, requested, 0).0;
    let mut selection_sampling = Map::new();
    selection_sampling.insert(
        "method".into(),
        Value::String("legacy_spacing_selection_segment".into()),
    );
    selection_sampling.insert(
        "candidate_phase_window_independent".into(),
        Value::Bool(false),
    );
    selection_sampling.insert(
        "final_membership_window_independent".into(),
        Value::Bool(false),
    );
    selection_sampling.insert(
        "reason".into(),
        Value::String("selection_segment_density_ceiling_equals_label_floor".into()),
    );
    selection_sampling.insert("segment_sessions".into(), Value::from(selection_pool.len()));
    selection_sampling.insert(
        "labels_legacy".into(),
        Value::from(non_overlapping(&selection_pool, requested).len()),
    );
    selection_sampling.insert(
        "labels_if_anchored".into(),
        Value::from(anchored_selection.len()),
    );
    selection_sampling.insert("label_floor".into(), Value::from(MINIMUM_SELECTION_LABELS));
    selection_sampling.insert("anchored_kept".into(), Value::Bool(false));
    let select_indices = if selection_metric == "non_overlapping_point" {
        non_overlapping(&selection_pool, requested)
    } else {
        selection_pool.clone()
    };

    let mut selection_evidence: Map<String, Value> = Map::new();
    let mut selection_p: Vec<(String, Option<f64>)> = Vec::new();
    let mut rejected: Vec<Value> = Vec::new();
    let mut ranked: Vec<Candidate> = Vec::new();
    for (name, family, formula, values) in &factors {
        let metrics = factor_metrics(values, &select_labels, &select_indices);
        let evidence = overlapping_ic(values, &select_labels, &selection_pool, requested, 500, 0);
        let stability = stability_verdict(
            &evidence,
            MINIMUM_ABS_SELECTION_IC,
            MINIMUM_SIGN_CONSISTENCY,
        );
        let shift = shift_p_values(values, &select_labels, &selection_pool, requested);
        selection_p.push((
            (*name).to_string(),
            shift.first().and_then(|(_, value)| *value),
        ));
        let (selection_rank_ic, verdict) = if selection_metric == "overlapping_block" {
            (
                evidence.get("rank_ic").and_then(Value::as_f64),
                stability.clone(),
            )
        } else {
            let rank_ic = metrics.get("rank_ic").and_then(Value::as_f64);
            let passed = rank_ic
                .map(|value| value.abs() >= MINIMUM_ABS_SELECTION_IC)
                .unwrap_or(false);
            (
                rank_ic,
                serde_json::json!({"passed": passed, "reason": "legacy_point_estimate_rule"}),
            )
        };
        let mut evidence_value = evidence.as_object().cloned().unwrap_or_default();
        evidence_value.insert(
            "applied_rule".into(),
            Value::String(selection_metric.clone()),
        );
        evidence_value.insert(
            "stability_would_pass".into(),
            stability
                .get("passed")
                .cloned()
                .unwrap_or(Value::Bool(false)),
        );
        evidence_value.insert(
            "stability_reason".into(),
            stability.get("reason").cloned().unwrap_or(Value::Null),
        );
        selection_evidence.insert((*name).to_string(), Value::Object(evidence_value));
        let mut metrics_value = metrics.as_object().cloned().unwrap_or_default();
        metrics_value.insert("selection".into(), evidence);
        metrics_value.insert("verdict".into(), verdict);
        metrics_value.insert("selection_rank_ic".into(), value_of(selection_rank_ic));
        let score = selection_rank_ic.map(f64::abs).unwrap_or(0.0);
        ranked.push(Candidate {
            name: (*name).to_string(),
            family: (*family).to_string(),
            formula: (*formula).to_string(),
            values: values.clone(),
            metrics: Value::Object(metrics_value),
            score,
        });
    }
    ranked.sort_by(|left, right| {
        right
            .score
            .partial_cmp(&left.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.name.cmp(&right.name))
    });

    let mut retained: Vec<Candidate> = Vec::new();
    for candidate in ranked {
        let verdict = candidate
            .metrics
            .get("verdict")
            .cloned()
            .unwrap_or(Value::Null);
        if verdict.get("passed") != Some(&Value::Bool(true)) {
            let mut row = Map::new();
            row.insert("factor".into(), Value::String(candidate.name.clone()));
            row.insert(
                "reason".into(),
                verdict.get("reason").cloned().unwrap_or(Value::Null),
            );
            rejected.push(Value::Object(row));
            continue;
        }
        let mut correlation: Option<Value> = None;
        for previous in &retained {
            let (px, py, _) = valid_pairs(&candidate.values, &previous.values, &select_indices);
            let corr = pearson(&px, &py);
            if let Some(corr) = corr {
                if corr.abs() >= MAX_ABS_FACTOR_CORRELATION {
                    let mut block = Map::new();
                    block.insert("with".into(), Value::String(previous.name.clone()));
                    block.insert("correlation".into(), Value::from(corr));
                    correlation = Some(Value::Object(block));
                    break;
                }
            }
        }
        if let Some(correlation) = correlation {
            let mut row = Map::new();
            row.insert("factor".into(), Value::String(candidate.name.clone()));
            row.insert("reason".into(), Value::String("redundant".into()));
            if let Value::Object(block) = correlation {
                for (key, value) in block {
                    row.insert(key, value);
                }
            }
            rejected.push(Value::Object(row));
            continue;
        }
        let family_count = retained
            .iter()
            .filter(|previous| previous.family == candidate.family)
            .count() as i64;
        if family_count >= max_factors_per_family {
            let mut row = Map::new();
            row.insert("factor".into(), Value::String(candidate.name.clone()));
            row.insert("reason".into(), Value::String("family_limit".into()));
            row.insert("family".into(), Value::String(candidate.family.clone()));
            rejected.push(Value::Object(row));
            continue;
        }
        retained.push(candidate);
        if retained.len() as i64 >= max_factors {
            break;
        }
    }

    let mut warnings: Vec<Value> = vec![
        Value::String(
            "Returns are gross close-to-close observations; this factor path does not model execution, spreads, slippage or fees."
                .into(),
        ),
        Value::String(
            "Factor calibration uses only the 52-week daily-bar standard and never widens the window automatically to buy samples."
                .into(),
        ),
    ];

    let spaced_selection_labels = non_overlapping(&selection_pool, requested).len() as i64;
    if spaced_selection_labels < MINIMUM_SELECTION_LABELS {
        warnings.push(Value::String(format!(
            "Only {spaced_selection_labels} non-overlapping selection labels are available; at least {MINIMUM_SELECTION_LABELS} are required under the 52-week standard, so factor ranks would be noise."
        )));
        let mut validation = Map::new();
        validation.insert("factor_decay".into(), Value::Object(decay));
        validation.insert("oos_factor_inference".into(), oos_inference);
        validation.insert("candidate_factor_count".into(), Value::from(factors.len()));
        validation.insert("sample_feasibility".into(), Value::Object(feasibility));
        validation.insert(
            "selection_label_count".into(),
            Value::from(select_indices.len()),
        );
        validation.insert(
            "oos_forecasts".into(),
            empty_oos_forecasts(&holdout_indices, &bars, &select_labels, requested),
        );
        validation.insert(
            "target_definition".into(),
            Value::String(format!("close[t+{requested}] / close[t] - 1 > 0")),
        );
        return researcher_result(
            "factor",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            serde_json::json!({"positive": [], "negative": []}),
            Value::Object(validation),
            vec![Value::String("daily raw OHLCV".into())],
            warnings,
            None,
            None,
        );
    }
    if retained.is_empty() {
        warnings.push(Value::String(
            "No candidate factor passed the minimum absolute Rank IC screen in the training segment."
                .into(),
        ));
        let mut validation = Map::new();
        validation.insert("factor_decay".into(), Value::Object(decay));
        validation.insert("oos_factor_inference".into(), oos_inference);
        validation.insert("candidate_factor_count".into(), Value::from(factors.len()));
        validation.insert("sample_feasibility".into(), Value::Object(feasibility));
        validation.insert(
            "oos_forecasts".into(),
            empty_oos_forecasts(&holdout_indices, &bars, &select_labels, requested),
        );
        validation.insert(
            "target_definition".into(),
            Value::String(format!("close[t+{requested}] / close[t] - 1 > 0")),
        );
        return researcher_result(
            "factor",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            serde_json::json!({"positive": [], "negative": []}),
            Value::Object(validation),
            vec![Value::String("daily raw OHLCV".into())],
            warnings,
            None,
            None,
        );
    }

    let mut standardized: Vec<(String, Vec<Option<f64>>, f64)> = Vec::new();
    for candidate in &retained {
        let train_values: Vec<f64> = select_indices
            .iter()
            .filter_map(|index| candidate.values.get(*index as usize).copied().flatten())
            .filter(|value| value.is_finite())
            .collect();
        let center = py_mean(&train_values);
        let scale = sample_std(&train_values);
        let (Some(center), Some(scale)) = (center, scale) else {
            continue;
        };
        if scale == 0.0 {
            continue;
        }
        let direction_ic = candidate
            .metrics
            .get("selection_rank_ic")
            .and_then(Value::as_f64)
            .or_else(|| candidate.metrics.get("rank_ic").and_then(Value::as_f64));
        let sign = if direction_ic.unwrap_or(0.0) >= 0.0 {
            1.0
        } else {
            -1.0
        };
        let values: Vec<Option<f64>> = candidate
            .values
            .iter()
            .map(|value| value.map(|value| ((value - center) / scale).clamp(-3.0, 3.0)))
            .collect();
        standardized.push((candidate.name.clone(), values, sign));
    }
    if standardized.is_empty() {
        let mut validation = Map::new();
        validation.insert("oos_factor_inference".into(), oos_inference);
        validation.insert("candidate_factor_count".into(), Value::from(factors.len()));
        validation.insert("sample_feasibility".into(), Value::Object(feasibility));
        validation.insert(
            "oos_forecasts".into(),
            empty_oos_forecasts(&holdout_indices, &bars, &select_labels, requested),
        );
        validation.insert(
            "target_definition".into(),
            Value::String(format!("close[t+{requested}] / close[t] - 1 > 0")),
        );
        return researcher_result(
            "factor",
            &ticker,
            &horizon_name,
            &snapshot_id,
            "insufficient_data",
            "forecast",
            None,
            None,
            serde_json::json!({"positive": [], "negative": []}),
            Value::Object(validation),
            vec![Value::String("daily raw OHLCV".into())],
            vec![Value::String(
                "Selected factors had no stable scale in the training segment.".into(),
            )],
            None,
            None,
        );
    }

    let composite = |index: usize| -> Option<f64> {
        let mut values: Vec<f64> = Vec::new();
        for (_, series, sign) in &standardized {
            if let Some(value) = series.get(index).copied().flatten() {
                values.push(value * sign);
            }
        }
        if values.len() == standardized.len() && !values.is_empty() {
            py_mean(&values)
        } else {
            None
        }
    };

    let calibration_hi = count.saturating_sub(requested as usize);
    let (calibration_indices, calibration_sampling) =
        spaced(FEATURE_WARMUP_SESSIONS as usize, calibration_hi);
    let calibration_pairs: Vec<(f64, f64)> = calibration_indices
        .iter()
        .filter_map(|index| {
            let position = *index as usize;
            let score = composite(position)?;
            let label = select_labels.get(position).copied().flatten()?;
            Some((score, label))
        })
        .collect();
    let calibration_scores: Vec<f64> = calibration_pairs.iter().map(|(score, _)| *score).collect();
    let (low_cut, high_cut) = bucket_cuts(&calibration_scores, options.bucket_mode);
    let mut bucket_samples: Vec<(&'static str, Vec<f64>)> = bucket_keys(options.bucket_mode)
        .into_iter()
        .map(|key| (key, Vec::new()))
        .collect();
    for (score, target) in &calibration_pairs {
        if let Some(bucket) = bucket_for_score(Some(*score), low_cut, high_cut, options.bucket_mode)
        {
            if let Some((_, samples)) = bucket_samples.iter_mut().find(|(key, _)| *key == bucket) {
                samples.push(*target);
            }
        }
    }
    let calibration_prevalence = py_mean(
        &calibration_pairs
            .iter()
            .map(|(_, target)| f64::from(*target > 0.0))
            .collect::<Vec<_>>(),
    );
    feasibility.insert(
        "calibration_observations_with_complete_factor_scores".into(),
        Value::from(calibration_pairs.len()),
    );
    feasibility.insert(
        "calibration_available".into(),
        Value::from(calibration_pairs.len()),
    );
    let mut bucket_counts = Map::new();
    for (key, samples) in &bucket_samples {
        bucket_counts.insert((*key).to_string(), Value::from(samples.len()));
    }
    feasibility.insert(
        "calibration_bucket_counts".into(),
        Value::Object(bucket_counts.clone()),
    );
    feasibility.insert(
        "all_current_buckets_meet_minimum".into(),
        Value::Bool(
            bucket_samples
                .iter()
                .all(|(_, samples)| samples.len() as i64 >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS),
        ),
    );
    let reused_selection_labels: Vec<i64> = calibration_indices
        .iter()
        .copied()
        .filter(|index| index + requested < train_end as i64)
        .collect();
    let cross_boundary_labels: Vec<i64> = calibration_indices
        .iter()
        .copied()
        .filter(|index| *index < train_end as i64 && train_end as i64 <= index + requested)
        .collect();
    let post_selection_labels: Vec<i64> = calibration_indices
        .iter()
        .copied()
        .filter(|index| *index >= train_end as i64)
        .collect();
    feasibility.insert(
        "selection_labels_reused_for_calibration".into(),
        Value::from(reused_selection_labels.len()),
    );
    feasibility.insert(
        "cross_boundary_calibration_labels".into(),
        Value::from(cross_boundary_labels.len()),
    );
    feasibility.insert(
        "post_selection_calibration_labels".into(),
        Value::from(post_selection_labels.len()),
    );
    feasibility.insert(
        "calibration_reuses_selection_labels".into(),
        Value::Bool(!reused_selection_labels.is_empty()),
    );

    let current_score = composite(count.saturating_sub(1));
    let current_bucket = bucket_for_score(current_score, low_cut, high_cut, options.bucket_mode);
    let selected_returns: Vec<f64> = current_bucket
        .and_then(|bucket| {
            bucket_samples
                .iter()
                .find(|(key, _)| *key == bucket)
                .map(|(_, samples)| samples.clone())
        })
        .unwrap_or_default();

    let honest_pairs: Vec<(f64, f64)> = post_selection_labels
        .iter()
        .filter_map(|index| {
            let position = *index as usize;
            let score = composite(position)?;
            let label = select_labels.get(position).copied().flatten()?;
            Some((score, label))
        })
        .collect();
    let honest_scores: Vec<f64> = honest_pairs.iter().map(|(score, _)| *score).collect();
    let (honest_low_cut, honest_high_cut) = bucket_cuts(&honest_scores, options.bucket_mode);
    let mut honest_buckets: Vec<(&'static str, Vec<f64>)> = bucket_keys(options.bucket_mode)
        .into_iter()
        .map(|key| (key, Vec::new()))
        .collect();
    for (score, target) in &honest_pairs {
        if let Some(bucket) = bucket_for_score(
            Some(*score),
            honest_low_cut,
            honest_high_cut,
            options.bucket_mode,
        ) {
            if let Some((_, samples)) = honest_buckets.iter_mut().find(|(key, _)| *key == bucket) {
                samples.push(*target);
            }
        }
    }
    let honest_bucket = bucket_for_score(
        current_score,
        honest_low_cut,
        honest_high_cut,
        options.bucket_mode,
    );
    let honest_sample: Vec<f64> = honest_bucket
        .and_then(|bucket| {
            honest_buckets
                .iter()
                .find(|(key, _)| *key == bucket)
                .map(|(_, samples)| samples.clone())
        })
        .unwrap_or_default();
    let honest_counts: Vec<usize> = honest_buckets
        .iter()
        .map(|(_, samples)| samples.len())
        .collect();
    let honest_keys: Vec<&str> = honest_buckets.iter().map(|(key, _)| *key).collect();
    let honest_probability = if honest_bucket.is_some()
        && honest_sample.len() as i64 >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS
        && honest_buckets
            .iter()
            .all(|(_, samples)| samples.len() as i64 >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS)
    {
        shrunk_probability(
            &honest_sample,
            py_mean(
                &honest_pairs
                    .iter()
                    .map(|(_, target)| f64::from(*target > 0.0))
                    .collect::<Vec<_>>(),
            ),
            SHRINKAGE_PRIOR_OBSERVATIONS,
        )
    } else {
        None
    };
    let mut honest_counts_map = Map::new();
    for (key, samples) in &honest_buckets {
        honest_counts_map.insert((*key).to_string(), Value::from(samples.len()));
    }
    let mut honest_candidate = Map::new();
    honest_candidate.insert("used_for_reported_probability".into(), Value::Bool(true));
    honest_candidate.insert(
        "status".into(),
        Value::String(
            if honest_probability.is_some() {
                "candidate_available"
            } else {
                "abstained"
            }
            .into(),
        ),
    );
    honest_candidate.insert(
        "reason".into(),
        Value::String(match honest_probability {
            Some(_) => "post_selection_labels_available".to_string(),
            None => format!(
                "post-selection budget is {} labels with counts {}; at least {} per bucket are required",
                honest_pairs.len(),
                counts_repr(&honest_keys, &honest_counts),
                MINIMUM_FACTOR_BUCKET_OBSERVATIONS
            ),
        }),
    );
    honest_candidate.insert(
        "selection_boundary_session".into(),
        bars.get(train_end)
            .map(|bar| Value::String(bar.date.clone()))
            .unwrap_or(Value::Null),
    );
    honest_candidate.insert("observations".into(), Value::from(honest_pairs.len()));
    honest_candidate.insert("bucket_counts".into(), Value::Object(honest_counts_map));
    honest_candidate.insert(
        "probability".into(),
        honest_probability.map(Value::from).unwrap_or(Value::Null),
    );
    let mut probability = honest_probability;
    let mut expected = if probability.is_some() {
        shrunk_mean(
            &honest_sample,
            py_mean(
                &honest_pairs
                    .iter()
                    .map(|(_, target)| f64::from(*target > 0.0))
                    .collect::<Vec<_>>(),
            ),
            SHRINKAGE_PRIOR_OBSERVATIONS,
        )
    } else {
        None
    };
    if probability.is_none() {
        match current_bucket {
            None => warnings.push(Value::String(
                "The latest OHLCV row did not produce a complete selected-factor score.".into(),
            )),
            Some(bucket) => warnings.push(Value::String(format!(
                "Calibration bucket '{bucket}' has {} matured observations; at least {} are required for a probability.",
                selected_returns.len(),
                MINIMUM_FACTOR_BUCKET_OBSERVATIONS
            ))),
        }
    } else if bucket_samples
        .iter()
        .map(|(_, samples)| samples.len() as i64)
        .min()
        .unwrap_or(0)
        < 2 * MINIMUM_FACTOR_BUCKET_OBSERVATIONS
    {
        warnings.push(Value::String(format!(
            "At least one 52-week calibration bucket is thin; probabilities are shrunk toward the matured base rate with a prior weight of {SHRINKAGE_PRIOR_OBSERVATIONS} observations."
        )));
    }

    let mut holdout_pairs: Vec<(i64, f64, i64, Option<f64>)> = Vec::new();
    let mut oos_forecasts: Vec<Value> = Vec::new();
    let mut matured_base_rate: Option<f64> = None;
    for index in &holdout_indices {
        let position = *index as usize;
        let score = composite(position);
        let Some(target) = select_labels.get(position).copied().flatten() else {
            continue;
        };
        let matured: Vec<i64> = calibration_indices
            .iter()
            .copied()
            .filter(|candidate| *candidate >= train_end as i64 && *candidate + requested <= *index)
            .collect();
        let matured_pairs: Vec<(f64, f64)> = matured
            .iter()
            .filter_map(|candidate| {
                let candidate_position = *candidate as usize;
                let score = composite(candidate_position)?;
                let label = select_labels.get(candidate_position).copied().flatten()?;
                Some((score, label))
            })
            .collect();
        matured_base_rate = py_mean(
            &matured_pairs
                .iter()
                .map(|(_, value)| f64::from(*value > 0.0))
                .collect::<Vec<_>>(),
        );
        let matured_scores: Vec<f64> = matured_pairs.iter().map(|(value, _)| *value).collect();
        let (matured_low_cut, matured_high_cut) = bucket_cuts(&matured_scores, options.bucket_mode);
        let mut matured_buckets: Vec<(&'static str, Vec<f64>)> = bucket_keys(options.bucket_mode)
            .into_iter()
            .map(|key| (key, Vec::new()))
            .collect();
        for (matured_score, matured_target) in &matured_pairs {
            if let Some(bucket) = bucket_for_score(
                Some(*matured_score),
                matured_low_cut,
                matured_high_cut,
                options.bucket_mode,
            ) {
                if let Some((_, samples)) =
                    matured_buckets.iter_mut().find(|(key, _)| *key == bucket)
                {
                    samples.push(*matured_target);
                }
            }
        }
        let training_end = matured.iter().map(|candidate| candidate + requested).max();
        let bucket = bucket_for_score(
            score,
            matured_low_cut,
            matured_high_cut,
            options.bucket_mode,
        );
        let sample: Vec<f64> = bucket
            .and_then(|bucket| {
                matured_buckets
                    .iter()
                    .find(|(key, _)| *key == bucket)
                    .map(|(_, samples)| samples.clone())
            })
            .unwrap_or_default();
        let forecast_probability =
            if bucket.is_some() && sample.len() as i64 >= MINIMUM_FACTOR_BUCKET_OBSERVATIONS {
                shrunk_probability(&sample, matured_base_rate, SHRINKAGE_PRIOR_OBSERVATIONS)
            } else {
                None
            };
        let mut row = Map::new();
        row.insert(
            "decision_session".into(),
            Value::String(bars[position].date.clone()),
        );
        row.insert(
            "label_end_session".into(),
            Value::String(bars[position + requested as usize].date.clone()),
        );
        row.insert("target".into(), Value::from(i64::from(target > 0.0)));
        row.insert(
            "probability".into(),
            forecast_probability.map(Value::from).unwrap_or(Value::Null),
        );
        row.insert(
            "training_label_end_session".into(),
            training_end
                .and_then(|end| bars.get(end as usize))
                .map(|bar| Value::String(bar.date.clone()))
                .unwrap_or(Value::Null),
        );
        row.insert("training_sample_count".into(), Value::from(sample.len()));
        row.insert(
            "training_observation_count".into(),
            Value::from(matured_pairs.len()),
        );
        row.insert(
            "target_definition".into(),
            Value::String(format!("close[t+{requested}] / close[t] - 1 > 0")),
        );
        oos_forecasts.push(Value::Object(row));
        if let Some(forecast) = forecast_probability {
            holdout_pairs.push((*index, forecast, i64::from(target > 0.0), matured_base_rate));
        }
    }
    let brier = py_mean(
        &holdout_pairs
            .iter()
            .map(|(_, forecast, outcome, _)| (forecast - *outcome as f64).powi(2))
            .collect::<Vec<_>>(),
    );
    let baseline_brier = py_mean(
        &holdout_pairs
            .iter()
            .filter_map(|(_, _, outcome, base)| base.map(|base| (base - *outcome as f64).powi(2)))
            .collect::<Vec<_>>(),
    );

    let lookbacks: &[(&str, i64)] = &[
        ("momentum_5", 5),
        ("momentum_20", 20),
        ("momentum_60", 60),
        ("reversal_1", 1),
        ("trend_sma20", 20),
        ("realized_volatility_20", 20),
        ("volume_z20", 20),
        ("range_position_20", 20),
        ("close_location", 1),
        ("overnight_gap", 1),
        ("volume_return_corr20", 20),
    ];
    let mut selected_factor_rows: Vec<Value> = Vec::new();
    for candidate in &retained {
        let Some((_, series, sign)) = standardized
            .iter()
            .find(|(name, _, _)| *name == candidate.name)
        else {
            continue;
        };
        let last = series.last().copied().flatten();
        let mut row = Map::new();
        row.insert("factor_name".into(), Value::String(candidate.name.clone()));
        row.insert("family".into(), Value::String(candidate.family.clone()));
        row.insert("formula".into(), Value::String(candidate.formula.clone()));
        row.insert(
            "lookback".into(),
            lookbacks
                .iter()
                .find(|(name, _)| *name == candidate.name)
                .map(|(_, lookback)| Value::from(*lookback))
                .unwrap_or(Value::Null),
        );
        row.insert(
            "training_direction".into(),
            Value::String(if *sign > 0.0 { "positive" } else { "negative" }.into()),
        );
        row.insert(
            "current_contribution".into(),
            last.map(|value| Value::from(value * sign))
                .unwrap_or(Value::Null),
        );
        row.insert(
            "training_rank_ic".into(),
            candidate
                .metrics
                .get("rank_ic")
                .cloned()
                .unwrap_or(Value::Null),
        );
        row.insert(
            "coverage".into(),
            candidate
                .metrics
                .get("coverage")
                .cloned()
                .unwrap_or(Value::Null),
        );
        selected_factor_rows.push(Value::Object(row));
    }
    let mut positive: Vec<Value> = Vec::new();
    let mut negative: Vec<Value> = Vec::new();
    for item in &selected_factor_rows {
        let Some(contribution) = item.get("current_contribution").and_then(Value::as_f64) else {
            continue;
        };
        let mut row = Map::new();
        for key in [
            "family",
            "factor_name",
            "formula",
            "training_rank_ic",
            "training_direction",
            "current_contribution",
        ] {
            row.insert(
                key.to_string(),
                item.get(key).cloned().unwrap_or(Value::Null),
            );
        }
        if contribution >= 0.0 {
            positive.push(Value::Object(row));
        } else {
            negative.push(Value::Object(row));
        }
    }
    let diversity = {
        let mut families: Vec<&str> = selected_factor_rows
            .iter()
            .filter_map(|row| row.get("family").and_then(Value::as_str))
            .collect();
        families.sort_unstable();
        families.dedup();
        families.len()
    };
    let status = if probability.is_some() && holdout_pairs.len() >= 10 {
        "success"
    } else if probability.is_some() {
        "partial"
    } else {
        "insufficient_data"
    };
    if probability.is_some() && holdout_pairs.len() < 10 {
        warnings.push(Value::String(
            "The rolling-origin evaluation has fewer than 10 matured-label factor predictions."
                .into(),
        ));
    }

    let oos_coverage = if oos_forecasts.is_empty() {
        0.0
    } else {
        holdout_pairs.len() as f64 / oos_forecasts.len() as f64
    };
    let mut validation = Map::new();
    validation.insert(
        "method".into(),
        Value::String(
            "52-week standard: training-segment factor selection + correlation pruning + rolling-origin matured-label bucket calibration with causal terciles"
                .into(),
        ),
    );
    validation.insert(
        "selected_factors".into(),
        Value::Array(selected_factor_rows.clone()),
    );
    validation.insert("factor_family_count".into(), Value::from(diversity));
    validation.insert("factor_decay".into(), Value::Object(decay));
    validation.insert("oos_factor_inference".into(), oos_inference);
    validation.insert("oos_forecasts".into(), Value::Array(oos_forecasts.clone()));
    validation.insert(
        "oos_evaluable_samples".into(),
        Value::from(oos_forecasts.len()),
    );
    validation.insert("oos_predictions".into(), Value::from(holdout_pairs.len()));
    validation.insert("oos_coverage".into(), Value::from(oos_coverage));
    validation.insert(
        "oos_abstain_rate".into(),
        if oos_forecasts.is_empty() {
            Value::Null
        } else {
            Value::from(1.0 - oos_coverage)
        },
    );
    validation.insert(
        "target_definition".into(),
        Value::String(format!("close[t+{requested}] / close[t] - 1 > 0")),
    );
    validation.insert("candidate_factor_count".into(), Value::from(factors.len()));
    validation.insert(
        "selection_label_count".into(),
        Value::from(select_indices.len()),
    );
    validation.insert(
        "evaluation_mode".into(),
        Value::String("rolling_origin_matured_labels".into()),
    );
    validation.insert(
        "evaluation_predictions".into(),
        Value::from(holdout_indices.len()),
    );
    validation.insert(
        "calibration_label_capacity".into(),
        Value::from(calibration_indices.len()),
    );
    validation.insert(
        "calibration_observations".into(),
        Value::from(calibration_pairs.len()),
    );
    validation.insert(
        "calibration_bucket_counts".into(),
        Value::Object(bucket_counts.clone()),
    );
    let mut raw_probabilities = Map::new();
    for (key, samples) in &bucket_samples {
        let value = if samples.is_empty() {
            Value::Null
        } else {
            Value::from(
                samples.iter().filter(|value| **value > 0.0).count() as f64 / samples.len() as f64,
            )
        };
        raw_probabilities.insert((*key).to_string(), value);
    }
    validation.insert(
        "calibration_bucket_raw_probabilities".into(),
        Value::Object(raw_probabilities),
    );
    validation.insert(
        "calibration_bucket_cut_points".into(),
        serde_json::json!({"low": low_cut, "high": high_cut}),
    );
    validation.insert(
        "bucket_cuts_recomputed_from_matured_scores".into(),
        Value::Bool(true),
    );
    validation.insert(
        "calibration_base_rate".into(),
        value_of(calibration_prevalence),
    );
    validation.insert(
        "honest_calibration_candidate".into(),
        Value::Object(honest_candidate),
    );
    let bucket_weight = if probability.is_some() {
        Value::from(
            selected_returns.len() as f64
                / (selected_returns.len() as f64 + SHRINKAGE_PRIOR_OBSERVATIONS as f64),
        )
    } else {
        Value::Null
    };
    validation.insert(
        "probability_shrinkage".into(),
        serde_json::json!({
            "prior_observations": SHRINKAGE_PRIOR_OBSERVATIONS,
            "bucket_weight": bucket_weight,
            "base_rate": if probability.is_some() { value_of(matured_base_rate) } else { Value::Null },
            "base_rate_scope": "matured_labels_available_at_decision",
            "segment_prevalence": value_of(calibration_prevalence),
            "segment_prevalence_scope": "descriptive_full_segment_not_used_for_probability",
        }),
    );
    validation.insert(
        "holdout_predictions".into(),
        Value::from(holdout_pairs.len()),
    );
    validation.insert("holdout_brier".into(), value_of(brier));
    validation.insert("holdout_base_rate_brier".into(), value_of(baseline_brier));
    validation.insert(
        "holdout_start_session".into(),
        bars.get(holdout_start)
            .map(|bar| Value::String(bar.date.clone()))
            .unwrap_or(Value::Null),
    );
    validation.insert("factor_correlation_threshold".into(), Value::from(0.85));
    validation.insert(
        "leakage_audit".into(),
        serde_json::json!({
            "features_use_data_through_decision_close_only": true,
            "future_returns_used_only_as_labels": true,
            "selection_precedes_calibration_and_holdout": true,
            "calibration_reuses_selection_labels": !reused_selection_labels.is_empty(),
            "selection_reused_label_count": reused_selection_labels.len(),
            "cross_boundary_label_count": cross_boundary_labels.len(),
            "post_selection_label_count": post_selection_labels.len(),
            "calibration_labels_non_overlapping": true,
            "calibration_labels_matured_before_each_decision": true,
            "oos_inference_did_not_select_or_modify_probability": true,
        }),
    );
    validation.insert(
        "diagnostics_label_end_exclusive".into(),
        Value::from(holdout_start),
    );
    feasibility.insert("minimum_holdout_predictions".into(), Value::from(10));
    validation.insert("sample_feasibility".into(), Value::Object(feasibility));
    let predictions: Vec<(i64, f64, Option<f64>, i64)> = holdout_pairs
        .iter()
        .map(|(session, forecast, outcome, base)| (*session, *forecast, *base, *outcome))
        .collect();
    validation.insert(
        "probabilistic_scores".into(),
        probabilistic_scores(&predictions, requested),
    );
    validation.insert(
        "sample_diagnostics".into(),
        sample_diagnostics(
            count as i64,
            FEATURE_WARMUP_SESSIONS,
            requested,
            Some(&bar_dates),
        ),
    );
    let mut selection = Map::new();
    selection.insert("metric".into(), Value::String(selection_metric.clone()));
    selection.insert("profile".into(), Value::String(profile_name.to_string()));
    selection.insert(
        "pool_observations".into(),
        Value::from(selection_pool.len()),
    );
    selection.insert(
        "spaced_observations".into(),
        Value::from(spaced_selection_labels),
    );
    selection.insert(
        "minimum_abs_rank_ic".into(),
        Value::from(MINIMUM_ABS_SELECTION_IC),
    );
    selection.insert(
        "minimum_sign_consistency".into(),
        Value::from(MINIMUM_SIGN_CONSISTENCY),
    );
    selection.insert("max_factors".into(), Value::from(max_factors));
    selection.insert(
        "max_factors_per_family".into(),
        Value::from(max_factors_per_family),
    );
    selection.insert(
        "max_abs_correlation".into(),
        Value::from(MAX_ABS_FACTOR_CORRELATION),
    );
    selection.insert(
        "multiple_testing".into(),
        multiple_testing_disclosure(&selection_p),
    );
    selection.insert("candidates".into(), Value::Object(selection_evidence));
    selection.insert("rejected".into(), Value::Array(rejected));
    selection.insert(
        "overlap_treated_as_dependence".into(),
        Value::Bool(selection_metric == "overlapping_block"),
    );
    validation.insert("selection".into(), Value::Object(selection));
    let bucket_defining_independent =
        [&holdout_sampling, &calibration_sampling]
            .iter()
            .all(|sampling| {
                sampling
                    .get("candidate_phase_window_independent")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            });
    let sampling = serde_json::json!({
        "selection": selection_sampling,
        "holdout": holdout_sampling,
        "calibration": calibration_sampling,
        "phase_rule": "weekday_ordinal_mod_horizon_plus_two",
        "mode": options.sampling_mode,
        "bucket_defining_segments_candidate_phase_window_independent": bucket_defining_independent,
        "bucket_defining_segments_final_membership_window_independent": false,
        "selection_segment_candidate_phase_window_independent": selection_sampling
            .get("candidate_phase_window_independent")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        "selection_segment_final_membership_window_independent": false,
    });
    validation.insert("sampling".into(), sampling);
    validation.insert(
        "bucket_mode".into(),
        Value::String(options.bucket_mode.into()),
    );
    validation.insert(
        "selection_profile".into(),
        Value::String(profile_name.to_string()),
    );

    let mut route = Map::new();
    route.insert(
        "route".into(),
        Value::String(
            if probability.is_some() {
                "shadow_candidate"
            } else {
                ROUTE_ABSTAINED
            }
            .into(),
        ),
    );
    route.insert(
        "route_reason".into(),
        Value::String(match probability {
            Some(_) => "post_selection_bucket_budget_met".to_string(),
            None => format!(
                "post_selection_budget_insufficient:{} labels, {} bucket counts",
                honest_pairs.len(),
                counts_repr(&honest_keys, &honest_counts)
            ),
        }),
    );
    route.insert(
        "checks".into(),
        serde_json::json!({
            "post_selection_labels": honest_pairs.len(),
            "bucket_counts": Value::Object({
                let mut map = Map::new();
                for (key, count) in honest_keys.iter().zip(honest_counts.iter()) {
                    map.insert((*key).to_string(), Value::from(*count));
                }
                map
            }),
            "minimum_per_bucket": MINIMUM_FACTOR_BUCKET_OBSERVATIONS,
        }),
    );
    route.insert("production_eligible".into(), Value::Bool(false));
    route.insert("shadow_only".into(), Value::Bool(true));
    let mut unqualified_estimate: Option<Value> = None;
    if probability.is_some() && route.get("route").and_then(Value::as_str) == Some(ROUTE_ABSTAINED)
    {
        unqualified_estimate = Some(serde_json::json!({
            "prob_up": probability,
            "expected_return": expected,
            "withheld_reason": route.get("route_reason").cloned().unwrap_or(Value::Null),
            "note": "Computed but not reportable: the path failed its own declared floors.",
        }));
        probability = None;
        expected = None;
    }
    if probability.is_none() {
        route.insert("route".into(), Value::String(ROUTE_ABSTAINED.into()));
        let reason = route
            .get("route_reason")
            .cloned()
            .filter(|value| !value.is_null())
            .unwrap_or_else(|| Value::String("probability_unavailable_for_current_session".into()));
        route.insert("route_reason".into(), reason);
        route.insert("legacy_probability_available".into(), Value::Bool(false));
    } else {
        route.insert("legacy_probability_available".into(), Value::Bool(true));
    }
    route.insert(
        "unqualified_estimate_retained".into(),
        Value::Bool(unqualified_estimate.is_some()),
    );
    validation.insert("qualification".into(), Value::Object(route));
    if let Some(estimate) = unqualified_estimate {
        validation.insert("unqualified_estimate".into(), estimate);
    }
    let status = if probability.is_none() {
        "insufficient_data"
    } else {
        status
    };
    researcher_result(
        "factor",
        &ticker,
        &horizon_name,
        &snapshot_id,
        status,
        "forecast",
        probability,
        expected,
        serde_json::json!({"positive": positive, "negative": negative}),
        Value::Object(validation),
        vec![Value::String("daily raw OHLCV".into())],
        warnings,
        if probability.is_some() {
            Some("out_of_sample_factor_score_bucket_frequency")
        } else {
            None
        },
        // `researcher_result` defaults raw_probability to prob_up when it is omitted;
        // the legacy factor call omits it, so pass the published probability here.
        probability,
    )
}

/// Run Research B with production defaults.
pub fn run(manifest: &Value, market: &Value) -> Result<Value, String> {
    run_with_options(manifest, market, &FactorOptions::default())
}

#[allow(dead_code)]
fn _model_version() -> &'static str {
    MODEL_VERSION
}
