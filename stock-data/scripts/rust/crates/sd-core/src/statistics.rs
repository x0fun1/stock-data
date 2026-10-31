//! Effectiveness statistics shared by the researcher and the auditor.
//!
//! Port of `quant_research/statistics.py`. Reproducibility and effectiveness are
//! orthogonal: a probability can be rebuilt bit for bit and still be worse than the
//! base rate. This module supplies Brier skill against two naive baselines, AUC, a
//! moving-block bootstrap for the paired loss improvement, effective sample size for
//! overlapping labels, and the power curve of a binomial evidence floor.
//!
//! The module deliberately depends on nothing but [`crate::random`] and
//! [`crate::float_sum`], so neither the producer nor the auditor can create a cycle.

use serde_json::{Map, Value};

use crate::contracts::py_str;
use crate::float_sum::python_sum;
use crate::random::PythonRandom;

/// Bootstrap replicates used when the caller does not override them.
pub const BOOTSTRAP_REPLICATES: usize = 2000;
/// Deterministic bootstrap seed used when the caller does not override it.
pub const BOOTSTRAP_SEED: u64 = 520_052_026;
/// Minimum number of blocks a moving-block bootstrap needs.
pub const MIN_BOOTSTRAP_BLOCKS: i64 = 10;
/// Minimum number of independent blocks for a stable interval.
pub const MIN_BOOTSTRAP_INDEPENDENT_BLOCKS: i64 = 3;

/// Only real, finite numbers count as statistics; booleans are not numbers.
fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Bool(_) | Value::Null | Value::Array(_) | Value::Object(_) => None,
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        Value::String(_) => None,
    }
}

/// Per-record baseline vs model squared error.
pub fn paired_rows(records: &[Value]) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::new();
    for record in records {
        let probability = number(record.get("probability"));
        let target = number(record.get("target"));
        let transform = record
            .get("probability_transform")
            .and_then(Value::as_object);
        let baseline = number(
            transform
                .and_then(|transform| transform.get("base_rate_at_origin"))
                .or_else(|| record.get("causal_base_rate")),
        );
        let (Some(probability), Some(target), Some(baseline)) = (probability, target, baseline)
        else {
            continue;
        };
        let baseline_error = baseline - target;
        let model_error = probability - target;
        let mut row = Map::new();
        row.insert(
            "decision_session".into(),
            record
                .get("decision_session")
                .cloned()
                .unwrap_or(Value::Null),
        );
        row.insert("baseline_probability".into(), Value::from(baseline));
        row.insert("model_probability".into(), Value::from(probability));
        row.insert("target".into(), Value::from(target));
        row.insert(
            "baseline_squared_error".into(),
            Value::from(baseline_error * baseline_error),
        );
        row.insert(
            "model_squared_error".into(),
            Value::from(model_error * model_error),
        );
        row.insert(
            "improvement".into(),
            Value::from(baseline_error * baseline_error - model_error * model_error),
        );
        rows.push(Value::Object(row));
    }
    rows.sort_by_key(|row| py_str(row.get("decision_session").unwrap_or(&Value::Null)));
    rows
}

/// Arithmetic mean of the values, `None` when the sequence is empty.
pub fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        None
    } else {
        Some(python_sum(values.iter().copied()) / values.len() as f64)
    }
}

/// Mean squared value (the Brier component of a probability or a residual).
pub fn brier(values: &[f64]) -> Option<f64> {
    let squared: Vec<f64> = values.iter().map(|value| value * value).collect();
    mean(&squared)
}

/// Rank-based AUC with tie handling; `None` when one class is absent.
pub fn auc(probabilities: &[f64], targets: &[i64]) -> Option<f64> {
    let pairs: Vec<(f64, i64)> = probabilities
        .iter()
        .zip(targets.iter())
        .map(|(probability, target)| (*probability, *target))
        .collect();
    let positives = pairs.iter().filter(|(_, target)| *target == 1).count();
    let negatives = pairs.len() - positives;
    if positives == 0 || negatives == 0 {
        return None;
    }
    let mut order: Vec<usize> = (0..pairs.len()).collect();
    order.sort_by(|left, right| {
        pairs[*left]
            .0
            .partial_cmp(&pairs[*right].0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ranks = vec![0.0f64; pairs.len()];
    let mut position = 0usize;
    while position < order.len() {
        let mut stop = position;
        while stop + 1 < order.len() && pairs[order[stop + 1]].0 == pairs[order[position]].0 {
            stop += 1;
        }
        let average_rank = (position + stop) as f64 / 2.0 + 1.0;
        for offset in position..=stop {
            ranks[order[offset]] = average_rank;
        }
        position = stop + 1;
    }
    let rank_sum = python_sum(
        (0..pairs.len())
            .filter(|index| pairs[*index].1 == 1)
            .map(|index| ranks[index]),
    );
    let numerator = rank_sum - (positives * (positives + 1)) as f64 / 2.0;
    Some(numerator / (positives * negatives) as f64)
}

/// Non-overlapping equivalents inside a daily label ledger.
pub fn effective_sample_size(observations: i64, horizon: i64) -> i64 {
    (observations / horizon.max(1)).max(0)
}

/// Bootstrap block lengths that respect the label's dependence span.
pub fn block_lengths_for(observations: i64, horizon: i64) -> Vec<i64> {
    let candidates = [horizon + 1, horizon + 5, horizon + 20];
    let usable: Vec<i64> = candidates
        .iter()
        .copied()
        .filter(|length| observations - length + 1 >= MIN_BOOTSTRAP_BLOCKS)
        .collect();
    if !usable.is_empty() {
        return usable;
    }
    vec![1.max((horizon + 1).min(observations))]
}

/// Standard error of a base rate given an effective sample size.
pub fn base_rate_standard_error(base_rate: Option<f64>, effective: i64) -> Option<f64> {
    let base_rate = base_rate?;
    if effective <= 0 {
        return None;
    }
    Some((0.0f64.max(base_rate * (1.0 - base_rate)) / effective as f64).sqrt())
}

fn unavailable_interval(reason: &str, block_length: i64) -> Value {
    let mut map = Map::new();
    map.insert("status".into(), Value::String("unavailable".into()));
    map.insert("reason".into(), Value::String(reason.into()));
    map.insert("block_length".into(), Value::from(block_length));
    Value::Object(map)
}

/// Moving-block bootstrap for the mean of a sampled, correlated series.
pub fn bootstrap_mean_interval(
    values: &[f64],
    block_length: i64,
    replicates: usize,
    seed: u64,
) -> Value {
    if values.is_empty() {
        return unavailable_interval("NO_VALUES", block_length);
    }
    let count = values.len() as i64;
    let length = block_length.max(1).min(count);
    let blocks = count - length + 1;
    if blocks < MIN_BOOTSTRAP_BLOCKS {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("unavailable".into()));
        map.insert("reason".into(), Value::String("INSUFFICIENT_BLOCKS".into()));
        map.insert("block_length".into(), Value::from(length));
        map.insert("blocks".into(), Value::from(blocks));
        map.insert("observations".into(), Value::from(count));
        return Value::Object(map);
    }
    let mut rng = PythonRandom::new(seed);
    let mut means: Vec<f64> = Vec::with_capacity(replicates);
    for _ in 0..replicates {
        let mut sample: Vec<f64> = Vec::with_capacity(values.len() * 2);
        while (sample.len() as i64) < count {
            let start = rng.randrange(blocks as u64) as usize;
            let end = (start + length as usize).min(values.len());
            sample.extend_from_slice(&values[start..end]);
        }
        let sampled = &sample[..values.len()];
        means.push(python_sum(sampled.iter().copied()) / count as f64);
    }
    means.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let first = *means.first().unwrap_or(&0.0);
    let last = *means.last().unwrap_or(&0.0);
    if first == last {
        let mut map = Map::new();
        map.insert(
            "status".into(),
            Value::String("degenerate_zero_width".into()),
        );
        map.insert("block_length".into(), Value::from(length));
        map.insert("blocks".into(), Value::from(blocks));
        map.insert("observations".into(), Value::from(count));
        map.insert(
            "reason".into(),
            Value::String("every resample reproduced the same mean".into()),
        );
        return Value::Object(map);
    }
    let lower_index = (0.025 * (replicates as f64 - 1.0)) as usize;
    let upper_index = (0.975 * (replicates as f64 - 1.0)) as usize;
    let mut map = Map::new();
    map.insert("status".into(), Value::String("estimated".into()));
    map.insert("block_length".into(), Value::from(length));
    map.insert("blocks".into(), Value::from(blocks));
    map.insert("observations".into(), Value::from(count));
    map.insert(
        "mean".into(),
        Value::from(python_sum(values.iter().copied()) / count as f64),
    );
    map.insert(
        "lower".into(),
        Value::from(means[lower_index.min(means.len() - 1)]),
    );
    map.insert(
        "upper".into(),
        Value::from(means[upper_index.min(means.len() - 1)]),
    );
    map.insert("replicates".into(), Value::from(replicates));
    map.insert("seed".into(), Value::from(seed));
    map.insert(
        "method".into(),
        Value::String("moving_block_bootstrap".into()),
    );
    map.insert(
        "interpretation".into(),
        Value::String(
            "overlapping labels remain correlated; this is an in-sample interval, not a future coverage guarantee"
                .into(),
        ),
    );
    Value::Object(map)
}

/// Compare the model with the causal base rate and with a constant 0.5.
pub fn baseline_comparison(
    records: &[Value],
    horizon: i64,
    block_lengths: Option<&[i64]>,
) -> Value {
    let paired = paired_rows(records);
    let observations = paired.len();
    if observations == 0 {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("unavailable".into()));
        map.insert(
            "reason".into(),
            Value::String("NO_MATURED_REPLAY_ROWS".into()),
        );
        map.insert("observations".into(), Value::from(0));
        map.insert("effective_sample_size".into(), Value::from(0));
        let mut baselines = Map::new();
        baselines.insert("causal_base_rate".into(), Value::Null);
        baselines.insert("constant_half".into(), Value::Null);
        map.insert("baselines".into(), Value::Object(baselines));
        return Value::Object(map);
    }
    let probabilities: Vec<f64> = paired
        .iter()
        .map(|row| row["model_probability"].as_f64().unwrap_or_default())
        .collect();
    let targets: Vec<i64> = paired
        .iter()
        .map(|row| row["target"].as_f64().unwrap_or_default() as i64)
        .collect();
    let baselines: Vec<f64> = paired
        .iter()
        .map(|row| row["baseline_probability"].as_f64().unwrap_or_default())
        .collect();
    let model_errors: Vec<f64> = probabilities
        .iter()
        .zip(targets.iter())
        .map(|(probability, target)| probability - *target as f64)
        .collect();
    let baseline_errors: Vec<f64> = baselines
        .iter()
        .zip(targets.iter())
        .map(|(baseline, target)| baseline - *target as f64)
        .collect();
    let half_errors: Vec<f64> = targets.iter().map(|target| 0.5 - *target as f64).collect();
    let brier_model = brier(&model_errors).unwrap_or_default();
    let brier_base = brier(&baseline_errors).unwrap_or_default();
    let brier_half = brier(&half_errors).unwrap_or_default();
    let mut skills = Map::new();
    skills.insert(
        "vs_causal_base_rate".into(),
        if brier_base != 0.0 {
            Value::from(1.0 - brier_model / brier_base)
        } else {
            Value::Null
        },
    );
    skills.insert(
        "vs_constant_half".into(),
        if brier_half != 0.0 {
            Value::from(1.0 - brier_model / brier_half)
        } else {
            Value::Null
        },
    );
    let improvements: Vec<f64> = paired
        .iter()
        .map(|row| row["improvement"].as_f64().unwrap_or_default())
        .collect();
    let lengths: Vec<i64> = match block_lengths {
        Some(lengths) => lengths.to_vec(),
        None => block_lengths_for(observations as i64, horizon),
    };
    let intervals: Vec<Value> = lengths
        .iter()
        .enumerate()
        .map(|(position, length)| {
            let mut interval = bootstrap_mean_interval(
                &improvements,
                *length,
                BOOTSTRAP_REPLICATES,
                BOOTSTRAP_SEED,
            );
            if let Value::Object(map) = &mut interval {
                map.insert("primary".into(), Value::Bool(position == 0));
            }
            interval
        })
        .collect();
    let effective = effective_sample_size(observations as i64, horizon);
    let primary = intervals.first().cloned().unwrap_or(Value::Null);
    let mut verdict = "insufficient_evidence";
    if primary.get("status").and_then(Value::as_str) == Some("estimated") {
        let lower = primary.get("lower").and_then(Value::as_f64).unwrap_or(0.0);
        let upper = primary.get("upper").and_then(Value::as_f64).unwrap_or(0.0);
        if lower > 0.0 {
            verdict = "positive_skill_with_estimated_interval_excluding_zero";
        } else if upper < 0.0 {
            verdict = "negative_skill_with_estimated_interval_excluding_zero";
        }
    }
    let mean_probability = mean(&probabilities);
    let observed_up_rate = mean(
        &targets
            .iter()
            .map(|target| *target as f64)
            .collect::<Vec<_>>(),
    );
    let mut baselines_note = Map::new();
    baselines_note.insert(
        "causal_base_rate".into(),
        Value::String("rate of matured 5D labels available at each origin (same row)".into()),
    );
    baselines_note.insert(
        "constant_half".into(),
        Value::String("0.5 for every decision".into()),
    );
    let mut map = Map::new();
    map.insert("status".into(), Value::String("estimated".into()));
    map.insert("observations".into(), Value::from(observations as i64));
    map.insert("effective_sample_size".into(), Value::from(effective));
    map.insert(
        "effective_sample_definition".into(),
        Value::String(format!("observations / horizon ({horizon})")),
    );
    map.insert("horizon_sessions".into(), Value::from(horizon));
    map.insert("brier_model".into(), Value::from(brier_model));
    map.insert("brier_causal_base_rate".into(), Value::from(brier_base));
    map.insert("brier_constant_half".into(), Value::from(brier_half));
    map.insert("skill".into(), Value::Object(skills));
    map.insert(
        "auc".into(),
        auc(&probabilities, &targets)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    map.insert(
        "mean_probability".into(),
        mean_probability.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "observed_up_rate".into(),
        observed_up_rate.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "base_rate_standard_error".into(),
        base_rate_standard_error(mean(&baselines), effective)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    map.insert("intervals".into(), Value::Array(intervals));
    map.insert("verdict".into(), Value::String(verdict.into()));
    map.insert("baselines".into(), Value::Object(baselines_note));
    map.insert(
        "evidence_source".into(),
        Value::String("snapshot_bars_unclassified".into()),
    );
    map.insert(
        "evidence_source_note".into(),
        Value::String(
            "the frozen snapshot does not record whether its bars are synthetic; a report must declare the source, and synthetic bars may support process conclusions only, never an effectiveness conclusion"
                .into(),
        ),
    );
    map.insert(
        "note".into(),
        Value::String(
            "skill is a relative Brier improvement; it is not a trading edge and ignores costs"
                .into(),
        ),
    );
    Value::Object(map)
}

/// `P(X >= k)` for `X ~ Binomial(n, probability)`.
pub fn binomial_tail(n: i64, k: i64, probability: f64) -> f64 {
    let start = k.max(0);
    python_sum((start..=n).map(|index| {
        binomial_coefficient(n, index)
            * probability.powf(index as f64)
            * (1.0 - probability).powf((n - index) as f64)
    }))
}

/// Big-endian-limb helper: exact `C(n, k)` rounded once to `f64`.
///
/// Python's `math.comb` is arbitrary precision and converts the exact integer to a
/// float before multiplying, so a `u128` stops working at `C(250, 125)`. The limbs are
/// multiplied/divided exactly and the final value is rounded half-to-even, matching
/// CPython's int-to-float conversion.
fn binomial_coefficient(n: i64, k: i64) -> f64 {
    if k < 0 || k > n {
        return 0.0;
    }
    let k = k.min(n - k);
    let mut limbs: Vec<u32> = vec![1];
    for step in 1..=k {
        let numerator = (n - k + step) as u32;
        let denominator = step as u32;
        let mut carry: u64 = 0;
        for limb in limbs.iter_mut() {
            let product = u64::from(*limb) * u64::from(numerator) + carry;
            *limb = (product & 0xffff_ffff) as u32;
            carry = product >> 32;
        }
        if carry > 0 {
            limbs.push(carry as u32);
        }
        let mut remainder: u64 = 0;
        for limb in limbs.iter_mut().rev() {
            let current = (remainder << 32) | u64::from(*limb);
            *limb = (current / u64::from(denominator)) as u32;
            remainder = current % u64::from(denominator);
        }
        while limbs.len() > 1 && *limbs.last().unwrap_or(&0) == 0 {
            limbs.pop();
        }
    }
    big_to_f64(&limbs)
}

fn big_to_f64(limbs: &[u32]) -> f64 {
    let Some(top) = limbs.last() else {
        return 0.0;
    };
    if *top == 0 {
        return 0.0;
    }
    let total_bits = (limbs.len() - 1) * 32 + (32 - top.leading_zeros() as usize);
    if total_bits <= 53 {
        let mut value = 0u64;
        for limb in limbs.iter().rev() {
            value = (value << 32) | u64::from(*limb);
        }
        return value as f64;
    }
    let shift = total_bits - 53;
    let mut mantissa = extract_bits(limbs, shift, 53);
    let guard = extract_bits(limbs, shift - 1, 1) == 1;
    let sticky = !is_zero_below(limbs, shift - 1);
    if guard && (sticky || mantissa & 1 == 1) {
        mantissa += 1;
    }
    // `2^53` is exactly representable, and both factors are powers of two scaled by
    // integers, so their product rounds exactly once at the final multiply.
    mantissa as f64 * 2f64.powi(shift as i32)
}

fn extract_bits(limbs: &[u32], start: usize, count: usize) -> u64 {
    let mut value = 0u64;
    for offset in (0..count).rev() {
        let bit = start + offset;
        let limb = bit / 32;
        let index = bit % 32;
        let set = limbs.get(limb).map(|word| (word >> index) & 1).unwrap_or(0);
        value = (value << 1) | u64::from(set);
    }
    value
}

fn is_zero_below(limbs: &[u32], end: usize) -> bool {
    let mut bit = 0;
    while bit < end {
        let limb = bit / 32;
        let index = bit % 32;
        let remaining = end - bit;
        let take = remaining.min(32 - index);
        let mask = if take == 32 {
            u32::MAX
        } else {
            (1u32 << take) - 1
        };
        let word = limbs.get(limb).copied().unwrap_or(0);
        if (word >> index) & mask != 0 {
            return false;
        }
        bit += take;
    }
    true
}

fn critical_count(n: i64, probability: f64, alpha: f64) -> i64 {
    for count in 0..=n {
        if binomial_tail(n, count, probability) <= alpha {
            return count;
        }
    }
    n
}

/// Power of "k successes out of n" as an evidence floor.
pub fn power_curve(
    sample_sizes: &[i64],
    true_rates: &[f64],
    null_rate: f64,
    alpha: f64,
) -> Vec<Value> {
    let mut rows = Vec::with_capacity(sample_sizes.len());
    for sample_size in sample_sizes {
        let critical = critical_count(*sample_size, null_rate, alpha);
        let mut power = Map::new();
        for rate in true_rates {
            power.insert(
                format!("{rate:.2}"),
                Value::from(binomial_tail(*sample_size, critical, *rate)),
            );
        }
        let mut row = Map::new();
        row.insert("sample_size".into(), Value::from(*sample_size));
        row.insert("critical_successes".into(), Value::from(critical));
        row.insert("alpha_one_sided".into(), Value::from(alpha));
        row.insert("power_by_true_rate".into(), Value::Object(power));
        rows.push(Value::Object(row));
    }
    rows
}
