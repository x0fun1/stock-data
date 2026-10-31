//! Dependency-aware factor-selection statistics (`selection.py`).
//!
//! A 52-week single-name study leaves about ten non-overlapping matured labels in the
//! selection segment, so this module measures the dependence instead of ignoring it: the
//! Rank IC is estimated on every matured label of the segment and the interval comes from
//! a moving-block bootstrap whose block length equals the label horizon. Nothing here
//! selects factors on its own; callers decide and the explicit verdict stays auditable.

use serde_json::{Map, Value};

use crate::math_utils::{
    benjamini_yekutieli, circular_block_bootstrap_spearman, circular_shift_spearman_p_value,
    spearman,
};

/// Minimum matured labels before any Rank IC is reported.
pub const MINIMUM_OBSERVATIONS: usize = 24;
/// Number of time sub-windows used for sign consistency.
pub const SUBWINDOW_COUNT: usize = 3;
/// Minimum labels per time sub-window.
pub const MINIMUM_SUBWINDOW_OBSERVATIONS: usize = 5;
/// Below this absolute Rank IC the direction is reported as flat.
pub const DIRECTION_FLAT_BAND: f64 = 1e-9;

fn pairs(values: &[Option<f64>], labels: &[Option<f64>], indices: &[i64]) -> Vec<(f64, f64)> {
    let mut pairs: Vec<(f64, f64)> = Vec::new();
    for index in indices {
        if *index < 0 || (*index as usize) >= values.len() || (*index as usize) >= labels.len() {
            continue;
        }
        let left = values[*index as usize];
        let right = labels[*index as usize];
        let (Some(left), Some(right)) = (left, right) else {
            continue;
        };
        if !left.is_finite() || !right.is_finite() {
            continue;
        }
        pairs.push((left, right));
    }
    pairs
}

fn subwindow_ics(pairs: &[(f64, f64)], count: usize) -> Vec<Option<f64>> {
    if count <= 1 || pairs.len() < count * MINIMUM_SUBWINDOW_OBSERVATIONS {
        return Vec::new();
    }
    let size = pairs.len() / count;
    let mut out: Vec<Option<f64>> = Vec::new();
    for window in 0..count {
        let start = window * size;
        let end = if window == count - 1 {
            pairs.len()
        } else {
            start + size
        };
        let chunk = &pairs[start..end];
        let xs: Vec<f64> = chunk.iter().map(|(value, _)| *value).collect();
        let ys: Vec<f64> = chunk.iter().map(|(_, label)| *label).collect();
        out.push(spearman(&xs, &ys));
    }
    out
}

fn direction(value: Option<f64>) -> &'static str {
    let Some(value) = value else {
        return "unknown";
    };
    if value > DIRECTION_FLAT_BAND {
        "positive"
    } else if value < -DIRECTION_FLAT_BAND {
        "negative"
    } else {
        "flat"
    }
}

fn value_or_null(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

/// Rank IC over every matured label in `indices`, with a block-bootstrap interval.
///
/// `indices` must already be restricted to labels matured inside the caller's training
/// boundary; this function adds no second maturity filter.
pub fn overlapping_ic(
    values: &[Option<f64>],
    labels: &[Option<f64>],
    indices: &[i64],
    horizon: i64,
    replicates: usize,
    seed: u64,
) -> Value {
    let pairs = pairs(values, labels, indices);
    let block_length = 1.max(horizon + 1);
    let mut base = Map::new();
    base.insert(
        "method".into(),
        Value::String("overlapping_block_rank_ic".into()),
    );
    base.insert("observations".into(), Value::from(pairs.len()));
    base.insert(
        "non_overlapping_equivalent".into(),
        Value::from(pairs.len() as i64 / block_length),
    );
    base.insert("block_length".into(), Value::from(block_length));
    base.insert("requested_indices".into(), Value::from(indices.len()));
    if pairs.len() < MINIMUM_OBSERVATIONS {
        base.insert(
            "status".into(),
            Value::String("insufficient_samples".into()),
        );
        base.insert("rank_ic".into(), Value::Null);
        base.insert("interval".into(), Value::Null);
        base.insert("sign_consistency".into(), Value::Null);
        base.insert("direction".into(), Value::String("unknown".into()));
        base.insert(
            "reason".into(),
            Value::String("fewer_matured_labels_than_minimum_observations".into()),
        );
        return Value::Object(base);
    }
    let xs: Vec<f64> = pairs.iter().map(|(value, _)| *value).collect();
    let ys: Vec<f64> = pairs.iter().map(|(_, label)| *label).collect();
    let observed = spearman(&xs, &ys);
    if observed.is_none() {
        base.insert("status".into(), Value::String("not_assessed".into()));
        base.insert("rank_ic".into(), Value::Null);
        base.insert("interval".into(), Value::Null);
        base.insert("sign_consistency".into(), Value::Null);
        base.insert("direction".into(), Value::String("unknown".into()));
        base.insert("reason".into(), Value::String("rank_ic_undefined".into()));
        return Value::Object(base);
    }
    let bootstrap = circular_block_bootstrap_spearman(&xs, &ys, block_length, replicates, seed);
    let windows = subwindow_ics(&pairs, SUBWINDOW_COUNT);
    let valid_windows: Vec<f64> = windows.iter().filter_map(|value| *value).collect();
    let reference = direction(observed);
    let mut consistency: Option<f64> = None;
    if !valid_windows.is_empty() && reference != "flat" {
        let agreeing = valid_windows
            .iter()
            .filter(|value| direction(Some(**value)) == reference)
            .count();
        consistency = Some(agreeing as f64 / valid_windows.len() as f64);
    }
    let interval = bootstrap.get("interval").cloned().unwrap_or(Value::Null);
    let excludes_zero = match &interval {
        Value::Array(items) if items.len() >= 2 => {
            let low = items[0].as_f64().unwrap_or(0.0);
            let high = items[1].as_f64().unwrap_or(0.0);
            Some(low > 0.0 || high < 0.0)
        }
        _ => None,
    };
    let mut result = base;
    result.insert("status".into(), Value::String("assessed".into()));
    result.insert("rank_ic".into(), value_or_null(observed));
    result.insert("direction".into(), Value::String(reference.into()));
    result.insert("interval".into(), interval);
    result.insert(
        "interval_status".into(),
        bootstrap.get("status").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "interval_replicates".into(),
        bootstrap.get("replicates").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "interval_excludes_zero".into(),
        excludes_zero.map(Value::Bool).unwrap_or(Value::Null),
    );
    result.insert(
        "subwindow_ics".into(),
        Value::Array(windows.into_iter().map(value_or_null).collect()),
    );
    result.insert(
        "valid_subwindow_count".into(),
        Value::from(valid_windows.len()),
    );
    result.insert("sign_consistency".into(), value_or_null(consistency));
    result.insert("overlap_treated_as_dependence".into(), Value::Bool(true));
    result.insert("coverage_validated".into(), Value::Bool(false));
    Value::Object(result)
}

/// Apply the pre-declared selection rule to one factor's evidence.
pub fn stability_verdict(
    metrics: &Value,
    min_abs_rank_ic: f64,
    min_sign_consistency: f64,
) -> Value {
    let status = metrics.get("status").and_then(Value::as_str).unwrap_or("");
    if status != "assessed" {
        let mut map = Map::new();
        map.insert("passed".into(), Value::Bool(false));
        map.insert(
            "reason".into(),
            Value::String("insufficient_selection_evidence".into()),
        );
        map.insert(
            "evidence_status".into(),
            metrics.get("status").cloned().unwrap_or(Value::Null),
        );
        return Value::Object(map);
    }
    let observed = metrics.get("rank_ic").and_then(Value::as_f64);
    let Some(observed) = observed else {
        let mut map = Map::new();
        map.insert("passed".into(), Value::Bool(false));
        map.insert("reason".into(), Value::String("rank_ic_undefined".into()));
        map.insert(
            "evidence_status".into(),
            metrics.get("status").cloned().unwrap_or(Value::Null),
        );
        return Value::Object(map);
    };
    if observed.abs() < min_abs_rank_ic {
        let mut map = Map::new();
        map.insert("passed".into(), Value::Bool(false));
        map.insert(
            "reason".into(),
            Value::String("below_minimum_rank_ic".into()),
        );
        map.insert("rank_ic".into(), Value::from(observed));
        map.insert("threshold".into(), Value::from(min_abs_rank_ic));
        return Value::Object(map);
    }
    let consistency = metrics.get("sign_consistency").and_then(Value::as_f64);
    let Some(consistency) = consistency else {
        let mut map = Map::new();
        map.insert("passed".into(), Value::Bool(false));
        map.insert(
            "reason".into(),
            Value::String("direction_unverifiable".into()),
        );
        map.insert("rank_ic".into(), Value::from(observed));
        map.insert("threshold".into(), Value::from(min_sign_consistency));
        return Value::Object(map);
    };
    if consistency < min_sign_consistency {
        let mut map = Map::new();
        map.insert("passed".into(), Value::Bool(false));
        map.insert("reason".into(), Value::String("direction_unstable".into()));
        map.insert("rank_ic".into(), Value::from(observed));
        map.insert("sign_consistency".into(), Value::from(consistency));
        map.insert("threshold".into(), Value::from(min_sign_consistency));
        return Value::Object(map);
    }
    let mut map = Map::new();
    map.insert("passed".into(), Value::Bool(true));
    map.insert(
        "reason".into(),
        Value::String("cleared_rank_ic_and_stability".into()),
    );
    map.insert("rank_ic".into(), Value::from(observed));
    map.insert("sign_consistency".into(), Value::from(consistency));
    map.insert("threshold".into(), Value::from(min_sign_consistency));
    Value::Object(map)
}

/// Disclose dependence-aware FDR without turning it into a selection gate.
pub fn multiple_testing_disclosure(p_values: &[(String, Option<f64>)]) -> Value {
    let adjusted = benjamini_yekutieli(p_values, Some(p_values.len()));
    let tested = p_values.iter().filter(|(_, value)| value.is_some()).count();
    let mut adjusted_map = Map::new();
    for (name, value) in adjusted {
        adjusted_map.insert(name, value_or_null(value));
    }
    let mut map = Map::new();
    map.insert(
        "method".into(),
        Value::String("benjamini_yekutieli_arbitrary_dependence".into()),
    );
    map.insert("tested_candidates".into(), Value::from(tested));
    map.insert("adjusted".into(), Value::Object(adjusted_map));
    map.insert("used_as_selection_gate".into(), Value::Bool(false));
    map.insert(
        "note".into(),
        Value::String(
            "Adjusted p-values are disclosure only; selection still requires out-of-sample evidence.".into(),
        ),
    );
    Value::Object(map)
}

/// Per-factor circular-shift p-values used for the FDR disclosure.
pub fn shift_p_values(
    values: &[Option<f64>],
    labels: &[Option<f64>],
    indices: &[i64],
    horizon: i64,
) -> Vec<(String, Option<f64>)> {
    let pairs = pairs(values, labels, indices);
    if pairs.len() < MINIMUM_OBSERVATIONS {
        return Vec::new();
    }
    let xs: Vec<f64> = pairs.iter().map(|(value, _)| *value).collect();
    let ys: Vec<f64> = pairs.iter().map(|(_, label)| *label).collect();
    let result = circular_shift_spearman_p_value(&xs, &ys, 1.max(horizon + 1));
    let count = result.get("permutations").and_then(Value::as_i64);
    if result.get("status").and_then(Value::as_str) != Some("exploratory")
        || count.map(|count| count < 19).unwrap_or(true)
    {
        return Vec::new();
    }
    vec![(
        "p_value".to_string(),
        result.get("p_value").and_then(Value::as_f64),
    )]
}
