//! Pre-registered candidate comparison for the two 52-week research arms
//! (`research_candidates.py`).
//!
//! Compares frozen candidates against the incumbent on the same decision dates. It never
//! switches production: the output is evidence for a later decision and the decision rule
//! is fixed before the run. Two arms are registered:
//!
//! * `ml.feature_set`: `full_v1` (incumbent) vs `compact_v1`, both through the production
//!   replay function with the same absolute refit anchors and training cap.
//! * `quant.shrinkage`: incumbent conditional frequency vs the pre-registered
//!   `(wins + 12 * p0) / (n + 12)` shrinkage recorded in each Research A OOS row.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use sd_core::float_sum::python_sum;
use sd_core::policy::HORIZONS;
use sd_core::random::PythonRandom;
use sd_research::ml::{ml_dataset, ml_replay, MINIMUM_TRAINING_ROWS};
use sd_research::quant::run as quant_run;

/// Minimum common scoring sessions before a verdict is possible.
pub const MINIMUM_EVALUATION_SESSIONS: i64 = 120;
/// Relative Brier improvement required by the registered rule.
pub const RELATIVE_BRIER_IMPROVEMENT: f64 = -0.02;
/// Maximum allowed coverage drop.
pub const MAX_COVERAGE_DROP: f64 = 0.05;
/// Bootstrap iterations.
pub const BOOTSTRAP_ITERATIONS: usize = 2000;
/// Bootstrap seed (registered).
pub const BOOTSTRAP_SEED: u64 = 20261022;
const HORIZON_LABEL: &str = "5D";

/// Registered protocol (Python literal order).
pub fn preregistration() -> Value {
    let mut map = Map::new();
    map.insert(
        "primary_metric".to_string(),
        Value::String(
            "paired Brier difference, candidate minus incumbent, on common decision dates".into(),
        ),
    );
    map.insert(
        "improvement_rule".to_string(),
        Value::String(
            "mean relative Brier difference <= -2% and the 95% moving-block bootstrap upper bound < 0"
                .into(),
        ),
    );
    map.insert(
        "coverage_rule".to_string(),
        Value::String("candidate coverage may not fall more than 5% below incumbent".into()),
    );
    map.insert(
        "minimum_sessions".to_string(),
        Value::from(MINIMUM_EVALUATION_SESSIONS),
    );
    map.insert(
        "multiple_comparisons".to_string(),
        Value::String(
            "two pre-registered arms; no other candidate may be added after seeing results".into(),
        ),
    );
    map.insert(
        "switch_policy".to_string(),
        Value::String(
            "never automatic; a supported candidate still requires a separate prospective confirmation"
                .into(),
        ),
    );
    Value::Object(map)
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

/// Brier score of one probability/outcome pair.
pub fn brier(probability: f64, target: i64) -> f64 {
    (probability - target as f64).powi(2)
}

fn quantile_index(fraction: f64, length: usize) -> usize {
    (fraction * length as f64) as usize
}

/// Moving-block bootstrap interval of a paired-difference series (`_block_interval`).
pub fn block_interval(values: &[f64], block: usize) -> Value {
    let count = values.len();
    if count < block * 2 {
        let mut result = Map::new();
        result.insert(
            "status".to_string(),
            Value::String("insufficient_for_block_bootstrap".into()),
        );
        result.insert("observations".to_string(), Value::from(count));
        return Value::Object(result);
    }
    let mut rng = PythonRandom::new(BOOTSTRAP_SEED);
    let upper = count.saturating_sub(block) + 1;
    let mut samples: Vec<f64> = Vec::new();
    for _ in 0..BOOTSTRAP_ITERATIONS {
        let mut start = rng.randrange(upper as u64) as usize;
        let mut sample: Vec<f64> = values[start..start + block].to_vec();
        while sample.len() < count {
            start = rng.randrange(upper as u64) as usize;
            sample.extend(values[start..start + block].iter().copied());
        }
        samples.push(python_sum(sample.iter().copied()) / sample.len() as f64);
    }
    samples.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let mut result = Map::new();
    result.insert("status".to_string(), Value::String("estimated".into()));
    result.insert("observations".to_string(), Value::from(count));
    result.insert("block_sessions".to_string(), Value::from(block));
    result.insert("iterations".to_string(), Value::from(BOOTSTRAP_ITERATIONS));
    result.insert(
        "mean".to_string(),
        Value::from(python_sum(values.iter().copied()) / count as f64),
    );
    result.insert(
        "low".to_string(),
        Value::from(samples[quantile_index(0.025, samples.len())]),
    );
    result.insert(
        "high".to_string(),
        Value::from(samples[quantile_index(0.975, samples.len()).saturating_sub(1)]),
    );
    Value::Object(result)
}

/// Registered verdict of one paired comparison.
pub fn verdict(paired: &[Value], incumbent_brier: f64, coverage_delta: f64) -> Value {
    if (paired.len() as i64) < MINIMUM_EVALUATION_SESSIONS {
        let mut result = Map::new();
        result.insert("status".to_string(), Value::String("indeterminate".into()));
        result.insert(
            "reason".to_string(),
            Value::String(format!(
                "{} common scoring sessions < pre-registered {MINIMUM_EVALUATION_SESSIONS}",
                paired.len()
            )),
        );
        return Value::Object(result);
    }
    let differences: Vec<f64> = paired
        .iter()
        .filter_map(|row| row.get("difference").and_then(Value::as_f64))
        .collect();
    let horizon = paired
        .first()
        .and_then(|row| row.get("horizon"))
        .and_then(Value::as_i64)
        .unwrap_or(5);
    let interval = block_interval(&differences, (horizon + 1) as usize);
    if interval.get("status").and_then(Value::as_str) != Some("estimated") {
        let mut result = Map::new();
        result.insert("status".to_string(), Value::String("indeterminate".into()));
        result.insert(
            "reason".to_string(),
            interval.get("status").cloned().unwrap_or(Value::Null),
        );
        result.insert("interval".to_string(), interval);
        return Value::Object(result);
    }
    let mean = interval.get("mean").and_then(Value::as_f64).unwrap_or(0.0);
    let relative = if incumbent_brier != 0.0 {
        Some(mean / incumbent_brier)
    } else {
        None
    };
    let high = interval.get("high").and_then(Value::as_f64).unwrap_or(0.0);
    let supported = relative
        .map(|relative| relative <= RELATIVE_BRIER_IMPROVEMENT)
        .unwrap_or(false)
        && high < 0.0
        && coverage_delta >= -MAX_COVERAGE_DROP;
    let mut result = Map::new();
    result.insert(
        "status".to_string(),
        Value::String(
            if supported {
                "candidate_supported"
            } else {
                "not_supported"
            }
            .into(),
        ),
    );
    result.insert(
        "relative_brier_difference".to_string(),
        relative.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert("interval".to_string(), interval);
    result.insert("coverage_delta".to_string(), Value::from(coverage_delta));
    Value::Object(result)
}

fn market_from_bars(bars: &[Value]) -> Value {
    serde_json::json!({
        "data": {"bars": bars},
        "_data_validation": {"forecast_eligible": true},
    })
}

/// `ml.feature_set` arm: `full_v1` (incumbent) vs `compact_v1`.
pub fn compare_ml(bars: &[Value], horizon_sessions: usize) -> Result<Value, String> {
    let market = market_from_bars(bars);
    let mut arms: BTreeMap<String, BTreeMap<String, Value>> = BTreeMap::new();
    let mut coverage: BTreeMap<String, f64> = BTreeMap::new();
    for name in ["full_v1", "compact_v1"] {
        let dataset = ml_dataset(&market, horizon_sessions, name)?;
        let records = ml_replay(&dataset, horizon_sessions)?;
        let mut by_session: BTreeMap<String, Value> = BTreeMap::new();
        for record in &records {
            if let Some(session) = record.get("decision_session").and_then(Value::as_str) {
                by_session.insert(session.to_string(), record.clone());
            }
        }
        let actionable = (dataset.n() as i64
            - dataset.warmup() as i64
            - horizon_sessions as i64
            - MINIMUM_TRAINING_ROWS as i64)
            .max(1);
        coverage.insert(name.to_string(), records.len() as f64 / actionable as f64);
        arms.insert(name.to_string(), by_session);
    }
    let common: Vec<String> = arms["full_v1"]
        .keys()
        .filter(|session| arms["compact_v1"].contains_key(*session))
        .cloned()
        .collect();
    let mut paired: Vec<Value> = Vec::new();
    let mut incumbent_total = 0.0;
    for date in &common {
        let incumbent = &arms["full_v1"][date];
        let candidate = &arms["compact_v1"][date];
        let incumbent_probability = number(incumbent.get("probability")).unwrap_or(0.0);
        let candidate_probability = number(candidate.get("probability")).unwrap_or(0.0);
        let target = number(incumbent.get("target")).unwrap_or(0.0) as i64;
        let difference =
            brier(candidate_probability, target) - brier(incumbent_probability, target);
        incumbent_total += brier(incumbent_probability, target);
        paired.push(serde_json::json!({
            "decision_session": date,
            "horizon": horizon_sessions as i64,
            "incumbent": incumbent_probability,
            "candidate": candidate_probability,
            "target": target,
            "difference": difference,
        }));
    }
    let incumbent_brier = if paired.is_empty() {
        0.0
    } else {
        incumbent_total / paired.len() as f64
    };
    let mut result = Map::new();
    result.insert(
        "arm".to_string(),
        Value::String("ml.feature_set full_v1 -> compact_v1".into()),
    );
    result.insert("common_sessions".to_string(), Value::from(common.len()));
    result.insert(
        "incumbent_sessions".to_string(),
        Value::from(arms["full_v1"].len()),
    );
    result.insert(
        "candidate_sessions".to_string(),
        Value::from(arms["compact_v1"].len()),
    );
    let mut coverage_map = Map::new();
    coverage_map.insert("incumbent".to_string(), Value::from(coverage["full_v1"]));
    coverage_map.insert("candidate".to_string(), Value::from(coverage["compact_v1"]));
    result.insert("coverage".to_string(), Value::Object(coverage_map));
    result.insert("paired".to_string(), Value::Array(paired.clone()));
    result.insert(
        "verdict".to_string(),
        verdict(
            &paired,
            incumbent_brier,
            coverage["compact_v1"] - coverage["full_v1"],
        ),
    );
    Ok(Value::Object(result))
}

/// `quant.shrinkage` arm: incumbent frequency vs the recorded drift-shrunk shadow.
pub fn compare_quant(bars: &[Value], horizon_sessions: usize) -> Result<Value, String> {
    let horizon_name = HORIZONS
        .iter()
        .find(|(_, value)| *value as usize == horizon_sessions)
        .map(|(name, _)| *name)
        .ok_or_else(|| format!("unsupported horizon {horizon_sessions}"))?;
    let manifest = serde_json::json!({
        "ticker": "SYN",
        "horizon": horizon_name,
        "snapshot_id": "candidate-eval",
        "market": "US",
    });
    let market = market_from_bars(bars);
    let result = quant_run(&manifest, &market)?;
    let quant_status = result.get("status").cloned().unwrap_or(Value::Null);
    let rows = result["validation"]["oos_forecasts"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let index_by_date: BTreeMap<String, i64> = bars
        .iter()
        .enumerate()
        .map(|(index, bar)| {
            (
                bar.get("date")
                    .map(sd_core::contracts::py_str)
                    .unwrap_or_default(),
                index as i64,
            )
        })
        .collect();
    let mut paired: Vec<Value> = Vec::new();
    let mut incumbent_total = 0.0;
    for row in rows {
        let raw = number(row.get("raw_probability"));
        let probability = number(row.get("probability"));
        let (Some(raw), Some(probability)) = (raw, probability) else {
            continue;
        };
        let session = row
            .get("decision_session")
            .and_then(Value::as_str)
            .unwrap_or("");
        let Some(index) = index_by_date.get(session).copied() else {
            continue;
        };
        if index % (horizon_sessions as i64 + 1) != 0 {
            continue;
        }
        let target = number(row.get("target")).unwrap_or(0.0) as i64;
        incumbent_total += brier(raw, target);
        paired.push(serde_json::json!({
            "decision_session": session,
            "horizon": horizon_sessions as i64,
            "incumbent": raw,
            "candidate": probability,
            "target": target,
            "difference": brier(probability, target) - brier(raw, target),
        }));
    }
    let incumbent_brier = if paired.is_empty() {
        0.0
    } else {
        incumbent_total / paired.len() as f64
    };
    let mut coverage = Map::new();
    coverage.insert("incumbent".to_string(), Value::from(1.0));
    coverage.insert(
        "candidate".to_string(),
        Value::from(if paired.is_empty() { 0.0 } else { 1.0 }),
    );
    let mut result = Map::new();
    result.insert(
        "arm".to_string(),
        Value::String("quant.shrinkage incumbent -> (wins + 12*p0)/(n+12)".into()),
    );
    result.insert("status".to_string(), quant_status);
    result.insert("common_sessions".to_string(), Value::from(paired.len()));
    result.insert("coverage".to_string(), Value::Object(coverage));
    result.insert("paired".to_string(), Value::Array(paired.clone()));
    result.insert(
        "verdict".to_string(),
        verdict(&paired, incumbent_brier, 0.0),
    );
    Ok(Value::Object(result))
}

/// Evaluate both registered arms for one bar series.
pub fn evaluate(bars: &[Value], horizon_sessions: usize) -> Result<Value, String> {
    let mut report = Map::new();
    report.insert("preregistration".to_string(), preregistration());
    report.insert("ml".to_string(), compare_ml(bars, horizon_sessions)?);
    report.insert("quant".to_string(), compare_quant(bars, horizon_sessions)?);
    report.insert("switch".to_string(), Value::Bool(false));
    report.insert(
        "note".to_string(),
        Value::String(
            "candidate support is not a production switch; it only justifies a prospective confirmation run"
                .into(),
        ),
    );
    Ok(Value::Object(report))
}

/// One report row for a frozen snapshot (mirrors the per-snapshot loop in `main`).
pub fn report_for_snapshot(
    snapshot_name: &str,
    ticker: &str,
    horizon: &str,
    bars: &[Value],
) -> Result<Value, String> {
    let horizon_sessions = HORIZONS
        .iter()
        .find(|(name, _)| *name == horizon)
        .map(|(_, value)| *value as usize)
        .ok_or_else(|| format!("unsupported horizon {horizon}"))?;
    let mut report = Map::new();
    report.insert(
        "snapshot".to_string(),
        Value::String(snapshot_name.to_string()),
    );
    report.insert("ticker".to_string(), Value::String(ticker.to_string()));
    report.insert("horizon".to_string(), Value::String(horizon.to_string()));
    let evaluated = evaluate(bars, horizon_sessions)?;
    if let Some(map) = evaluated.as_object() {
        for (key, value) in map {
            report.insert(key.clone(), value.clone());
        }
    }
    Ok(Value::Object(report))
}

#[allow(dead_code)]
fn _label_marker() -> &'static str {
    HORIZON_LABEL
}
