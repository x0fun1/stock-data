//! Small deterministic statistics helpers (`quant_research/math_utils.py`).
//!
//! No market or model opinions live here: every function is a pure numeric helper
//! whose float evaluation order is kept identical to the Python source so checked-in
//! numbers stay comparable bit for bit.

use serde_json::{Map, Value};

use sd_core::float_sum::python_sum;
use sd_core::random::PythonRandom;

/// Arithmetic mean of the finite values, `None` when nothing is finite.
pub fn mean(values: &[f64]) -> Option<f64> {
    let mut count = 0usize;
    let mut sum = 0.0f64;
    for value in values {
        if value.is_finite() {
            sum += *value;
            count += 1;
        }
    }
    if count == 0 {
        None
    } else {
        Some(sum / count as f64)
    }
}

/// Sample standard deviation (`n - 1` denominator), `None` below two values.
pub fn sample_std(values: &[f64]) -> Option<f64> {
    let finite: Vec<f64> = values
        .iter()
        .copied()
        .filter(|value| value.is_finite())
        .collect();
    if finite.len() < 2 {
        return None;
    }
    let center = python_sum(finite.iter().copied()) / finite.len() as f64;
    let squared = python_sum(
        finite
            .iter()
            .map(|value| (value - center) * (value - center)),
    );
    Some((squared / (finite.len() - 1) as f64).sqrt())
}

/// Pearson correlation over finite pairs, `None` below three pairs or zero spread.
pub fn pearson(x: &[f64], y: &[f64]) -> Option<f64> {
    let pairs: Vec<(f64, f64)> = x
        .iter()
        .zip(y.iter())
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .map(|(a, b)| (*a, *b))
        .collect();
    if pairs.len() < 3 {
        return None;
    }
    let xs: Vec<f64> = pairs.iter().map(|(a, _)| *a).collect();
    let ys: Vec<f64> = pairs.iter().map(|(_, b)| *b).collect();
    let sx = sample_std(&xs)?;
    let sy = sample_std(&ys)?;
    if sx == 0.0 || sy == 0.0 {
        return None;
    }
    let mx = python_sum(xs.iter().copied()) / xs.len() as f64;
    let my = python_sum(ys.iter().copied()) / ys.len() as f64;
    let covariance = python_sum(pairs.iter().map(|(a, b)| (a - mx) * (b - my)));
    Some(covariance / ((pairs.len() - 1) as f64 * sx * sy))
}

/// Average ranks with ties sharing the mean rank (stable by index for equal values).
pub fn average_ranks(values: &[f64]) -> Vec<f64> {
    let mut ordered: Vec<(usize, f64)> = values.iter().copied().enumerate().collect();
    ordered.sort_by(|left, right| {
        left.1
            .partial_cmp(&right.1)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ranks = vec![0.0f64; values.len()];
    let mut index = 0usize;
    while index < ordered.len() {
        let mut end = index + 1;
        while end < ordered.len() && ordered[end].1 == ordered[index].1 {
            end += 1;
        }
        let rank = (index + 1 + end) as f64 / 2.0;
        for entry in ordered.iter().take(end).skip(index) {
            ranks[entry.0] = rank;
        }
        index = end;
    }
    ranks
}

/// Spearman rank correlation (Pearson on average ranks).
pub fn spearman(x: &[f64], y: &[f64]) -> Option<f64> {
    let pairs: Vec<(f64, f64)> = x
        .iter()
        .zip(y.iter())
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .map(|(a, b)| (*a, *b))
        .collect();
    if pairs.len() < 3 {
        return None;
    }
    let xs: Vec<f64> = pairs.iter().map(|(a, _)| *a).collect();
    let ys: Vec<f64> = pairs.iter().map(|(_, b)| *b).collect();
    pearson(&average_ranks(&xs), &average_ranks(&ys))
}

/// Linear-interpolated quantile (type 7, matching the legacy helper).
pub fn quantile(values: &[f64], probability: f64) -> Option<f64> {
    let mut items: Vec<f64> = values
        .iter()
        .copied()
        .filter(|value| value.is_finite())
        .collect();
    if items.is_empty() {
        return None;
    }
    items.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let position = probability.clamp(0.0, 1.0) * (items.len() - 1) as f64;
    let lower = position as usize;
    let upper = (lower + 1).min(items.len() - 1);
    let fraction = position - lower as f64;
    Some(items[lower] * (1.0 - fraction) + items[upper] * fraction)
}

/// Wilson score interval for a binomial proportion, clamped to `[0, 1]`.
pub fn wilson_interval(successes: u64, count: u64, z: f64) -> Option<(f64, f64)> {
    if count == 0 {
        return None;
    }
    let count = count as f64;
    let p = successes as f64 / count;
    let denominator = 1.0 + z * z / count;
    let center = (p + z * z / (2.0 * count)) / denominator;
    let radius = z * (p * (1.0 - p) / count + z * z / (4.0 * count * count)).sqrt() / denominator;
    Some(((center - radius).max(0.0), (center + radius).min(1.0)))
}

/// Logistic link with the legacy `[-35, 35]` clamp.
pub fn logistic(value: f64) -> f64 {
    let clamped = value.clamp(-35.0, 35.0);
    1.0 / (1.0 + (-clamped).exp())
}

/// Benjamini-Yekutieli FDR adjustment (conservative under arbitrary dependence).
///
/// Returns one entry per input in input order; values that are not a finite
/// probability stay `None`.
pub fn benjamini_yekutieli(
    p_values: &[(String, Option<f64>)],
    trials: Option<usize>,
) -> Vec<(String, Option<f64>)> {
    let valid: Vec<(String, f64)> = p_values
        .iter()
        .filter_map(|(name, value)| match value {
            Some(number) if number.is_finite() && (0.0..=1.0).contains(number) => {
                Some((name.clone(), *number))
            }
            _ => None,
        })
        .collect();
    let family_size = valid.len().max(trials.unwrap_or(0));
    let mut adjusted: Vec<(String, Option<f64>)> = p_values
        .iter()
        .map(|(name, _)| (name.clone(), None))
        .collect();
    if family_size == 0 {
        return adjusted;
    }
    let harmonic = python_sum((1..=family_size).map(|rank| 1.0 / rank as f64));
    let mut ordered = valid.clone();
    ordered.sort_by(|left, right| {
        left.1
            .partial_cmp(&right.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.0.cmp(&right.0))
    });
    let mut running = 1.0f64;
    for index in (0..ordered.len()).rev() {
        let (name, value) = &ordered[index];
        let rank = (index + 1) as f64;
        running = running.min(value * family_size as f64 * harmonic / rank);
        if let Some(slot) = adjusted.iter_mut().find(|(candidate, _)| candidate == name) {
            slot.1 = Some(running.min(1.0));
        }
    }
    adjusted
}

fn bootstrap_provenance(
    status: &str,
    observations: usize,
    block_length: usize,
) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("status".into(), Value::String(status.into()));
    map.insert("observations".into(), Value::from(observations));
    map.insert("interval".into(), Value::Null);
    map.insert("replicates".into(), Value::from(0));
    map.insert("block_length".into(), Value::from(block_length));
    map.insert(
        "method".into(),
        Value::String("circular_moving_block_bootstrap_percentile".into()),
    );
    map.insert("coverage_validated".into(), Value::Bool(false));
    map
}

/// Paired circular moving-block bootstrap interval for a time-series Spearman IC.
pub fn circular_block_bootstrap_spearman(
    x: &[f64],
    y: &[f64],
    block_length: i64,
    replicates: usize,
    seed: u64,
) -> Value {
    let pairs: Vec<(f64, f64)> = x
        .iter()
        .zip(y.iter())
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .map(|(a, b)| (*a, *b))
        .collect();
    let count = pairs.len();
    let block = block_length.max(0) as usize;
    if count < 20usize.max(4 * block) || block_length < 1 || replicates < 1 {
        return Value::Object(bootstrap_provenance("insufficient_samples", count, block));
    }
    let observed = spearman(
        &pairs.iter().map(|(a, _)| *a).collect::<Vec<_>>(),
        &pairs.iter().map(|(_, b)| *b).collect::<Vec<_>>(),
    );
    let Some(observed) = observed else {
        return Value::Object(bootstrap_provenance("not_assessed", count, block));
    };
    let mut rng = PythonRandom::new(seed);
    let mut estimates: Vec<f64> = Vec::new();
    let mut attempts = 0usize;
    while estimates.len() < replicates && attempts < replicates * 5 {
        attempts += 1;
        let mut sample_indices: Vec<usize> = Vec::with_capacity(count + block);
        while sample_indices.len() < count {
            let start = rng.randrange(count as u64) as usize;
            for offset in 0..block {
                sample_indices.push((start + offset) % count);
            }
        }
        sample_indices.truncate(count);
        let sample_x: Vec<f64> = sample_indices.iter().map(|index| pairs[*index].0).collect();
        let sample_y: Vec<f64> = sample_indices.iter().map(|index| pairs[*index].1).collect();
        if let Some(estimate) = spearman(&sample_x, &sample_y) {
            estimates.push(estimate);
        }
    }
    if estimates.len() < 50usize.max(replicates / 2) {
        let mut map = bootstrap_provenance("unstable_resamples", count, block);
        map.insert("observed_ic".into(), Value::from(observed));
        map.insert("replicates".into(), Value::from(estimates.len()));
        return Value::Object(map);
    }
    let low = quantile(&estimates, 0.025).unwrap_or(0.0);
    let high = quantile(&estimates, 0.975).unwrap_or(0.0);
    let mut map = bootstrap_provenance("exploratory", count, block);
    map.insert("observed_ic".into(), Value::from(observed));
    map.insert(
        "interval".into(),
        Value::Array(vec![Value::from(low), Value::from(high)]),
    );
    map.insert("replicates".into(), Value::from(estimates.len()));
    Value::Object(map)
}

/// Circular-shift null diagnostic: interpretable as a p-value only under stationarity.
pub fn circular_shift_spearman_p_value(x: &[f64], y: &[f64], block_length: i64) -> Value {
    let pairs: Vec<(f64, f64)> = x
        .iter()
        .zip(y.iter())
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .map(|(a, b)| (*a, *b))
        .collect();
    let count = pairs.len();
    let observed = spearman(
        &pairs.iter().map(|(a, _)| *a).collect::<Vec<_>>(),
        &pairs.iter().map(|(_, b)| *b).collect::<Vec<_>>(),
    );
    let block = block_length.max(0) as usize;
    let offset_count = if block_length >= 1 && count >= block {
        (block..=(count - block)).count()
    } else {
        0
    };
    let mut result = Map::new();
    if observed.is_none() || offset_count < 19 {
        result.insert(
            "status".into(),
            Value::String("insufficient_samples".into()),
        );
        result.insert("observations".into(), Value::from(count));
        result.insert("p_value".into(), Value::Null);
        result.insert("permutations".into(), Value::from(0));
        result.insert("block_length".into(), Value::from(block_length));
        result.insert(
            "method".into(),
            Value::String("circular_shift_test_stationarity_assumption".into()),
        );
        return Value::Object(result);
    }
    let observed = observed.unwrap_or_default();
    let baseline_x: Vec<f64> = pairs.iter().map(|(a, _)| *a).collect();
    let baseline_y: Vec<f64> = pairs.iter().map(|(_, b)| *b).collect();
    let mut exceedances = 0usize;
    let mut permutations = 0usize;
    for offset in block..=(count - block) {
        if offset == 0 && block > 0 {
            // `range(block, count - block + 1)` starts at the block size; nothing to skip.
        }
        let shifted: Vec<f64> = baseline_y[offset..]
            .iter()
            .chain(baseline_y[..offset].iter())
            .copied()
            .collect();
        let Some(statistic) = spearman(&baseline_x, &shifted) else {
            continue;
        };
        permutations += 1;
        if statistic.abs() >= observed.abs() {
            exceedances += 1;
        }
    }
    let p_value = if permutations == 0 {
        None
    } else {
        Some((1 + exceedances) as f64 / (1 + permutations) as f64)
    };
    result.insert(
        "status".into(),
        Value::String(
            if p_value.is_some() {
                "exploratory"
            } else {
                "not_assessed"
            }
            .into(),
        ),
    );
    result.insert("observations".into(), Value::from(count));
    result.insert(
        "p_value".into(),
        p_value.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert("permutations".into(), Value::from(permutations));
    result.insert("block_length".into(), Value::from(block_length));
    result.insert(
        "method".into(),
        Value::String("circular_shift_test_stationarity_assumption".into()),
    );
    result.insert(
        "stationarity_assumption_validated".into(),
        Value::Bool(false),
    );
    Value::Object(result)
}
