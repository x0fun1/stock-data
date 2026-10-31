//! Pre-registered Research B candidate comparison on a fixed pool (`research_b_ab.py`).
//!
//! Two independent questions are declared before any result is read: Q1 compares the
//! selection estimator (legacy spaced point estimate vs restricted dependency-aware
//! overlapping block) and Q2 compares bucket designs (three terciles vs two median
//! buckets). The production arm is the reference for both; every arm shares the frozen
//! bars, horizon, anchored ledger phase and qualification floors. Nothing here switches
//! production.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use sd_core::contracts::normalize_request;
use sd_core::data_checks::{check_market, CheckOptions};
use sd_core::float_sum::python_sum;
use sd_core::random::PythonRandom;
use sd_research::factor::{run_with_options, FactorOptions};

const HORIZON_LABEL: &str = "5D";
const HORIZON_SESSIONS: usize = 5;
const BOOTSTRAP_REPLICATES: usize = 500;
const BOOTSTRAP_SEED: u64 = 0;

/// Registered arms in report order: (arm name, selection profile, bucket mode).
pub const ARMS: [(&str, &str, &str); 4] = [
    ("production_legacy_three", "legacy", "three"),
    ("restricted_selection_three", "restricted", "three"),
    ("legacy_two_bucket", "legacy", "two"),
    ("restricted_two_bucket", "restricted", "two"),
];

/// Registered questions: (question, reference arm, candidate arm).
pub const QUESTIONS: [(&str, &str, &str); 2] = [
    (
        "Q1_selection",
        "production_legacy_three",
        "restricted_selection_three",
    ),
    ("Q2_bucket", "production_legacy_three", "legacy_two_bucket"),
];

/// Registered protocol (Python literal order).
pub fn preregistration() -> Value {
    let mut map = Map::new();
    map.insert(
        "protocol".to_string(),
        Value::String("research-b-candidates-v1".into()),
    );
    map.insert("registered".to_string(), Value::String("2026-10-19".into()));
    map.insert(
        "reference_arm".to_string(),
        Value::String("production_legacy_three".into()),
    );
    let mut arms = Map::new();
    for (name, profile, bucket) in ARMS {
        let mut config = Map::new();
        config.insert(
            "selection_profile".to_string(),
            Value::String(profile.into()),
        );
        config.insert("bucket_mode".to_string(), Value::String(bucket.into()));
        arms.insert(name.to_string(), Value::Object(config));
    }
    map.insert("arms".to_string(), Value::Object(arms));
    let mut questions = Map::new();
    for (name, reference, candidate) in QUESTIONS {
        questions.insert(
            name.to_string(),
            Value::Array(vec![
                Value::String(reference.into()),
                Value::String(candidate.into()),
            ]),
        );
    }
    map.insert("questions".to_string(), Value::Object(questions));
    map.insert(
        "metric".to_string(),
        Value::String("mean_paired_per_session_brier_delta_candidate_minus_reference".into()),
    );
    let mut bootstrap = Map::new();
    bootstrap.insert("unit".to_string(), Value::String("session_block".into()));
    bootstrap.insert(
        "block_length".to_string(),
        Value::String("horizon + 1".into()),
    );
    bootstrap.insert("replicates".to_string(), Value::from(500));
    bootstrap.insert("seed".to_string(), Value::from(0));
    map.insert("bootstrap".to_string(), Value::Object(bootstrap));
    map.insert(
        "minimum_scorable_sessions_for_interval".to_string(),
        Value::from(24),
    );
    let mut decision = Map::new();
    decision.insert(
        "candidate_better".to_string(),
        Value::String("whole interval below zero".into()),
    );
    decision.insert(
        "candidate_worse".to_string(),
        Value::String("whole interval above zero".into()),
    );
    decision.insert(
        "indeterminate".to_string(),
        Value::String("interval contains zero, or too few scorable sessions".into()),
    );
    map.insert("decision".to_string(), Value::Object(decision));
    map.insert(
        "switch_policy".to_string(),
        Value::String(
            "no switch this run; a switch requires a pre-registered re-run that clears the rule"
                .into(),
        ),
    );
    map.insert(
        "thresholds_are_not_adjusted_after_seeing_results".to_string(),
        Value::Bool(true),
    );
    Value::Object(map)
}

/// One pool entry with its frozen market payload.
#[derive(Debug, Clone)]
pub struct PoolEntry {
    /// Symbol.
    pub symbol: String,
    /// Frozen market payload (with `_data_validation`).
    pub market: Value,
    /// Bar count.
    pub bar_count: i64,
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

/// Filter the registered pool to entries that pass the frozen gates.
pub fn load_pool(
    pool_document: &Value,
    collections: &Map<String, Value>,
    asof: &str,
) -> Result<Vec<PoolEntry>, String> {
    let entries: Vec<Value> =
        if let Some(symbols) = pool_document.get("symbols").and_then(Value::as_array) {
            symbols.clone()
        } else if let Some(items) = pool_document.as_array() {
            items.clone()
        } else {
            return Err("pool document must list symbols".into());
        };
    let mut pool: Vec<PoolEntry> = Vec::new();
    for entry in entries {
        let (symbol, asset_type) = if entry.is_object() {
            (
                entry
                    .get("symbol")
                    .map(sd_core::contracts::py_str)
                    .unwrap_or_default(),
                entry
                    .get("asset_type")
                    .map(sd_core::contracts::py_str)
                    .unwrap_or_else(|| "equity".into()),
            )
        } else {
            (sd_core::contracts::py_str(&entry), "equity".to_string())
        };
        let Some(payload) = collections.get(&symbol) else {
            continue;
        };
        let market = payload.get("market").cloned().unwrap_or(Value::Null);
        let bars = market
            .get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if bars.len() < 200 {
            continue;
        }
        let request_value = serde_json::json!({
            "ticker": symbol,
            "market": "US",
            "horizon": HORIZON_LABEL,
            "asof": asof,
            "mode": "standard",
            "intent": "forecast",
            "asset_type": if asset_type != "equity" { "etf" } else { "stock" },
        });
        let request =
            normalize_request(&request_value).map_err(|error| error.message().to_string())?;
        let Some(captured) = market.get("fetched_at_utc").and_then(Value::as_str) else {
            continue;
        };
        let gate = check_market(
            &market,
            &request.to_json(),
            &CheckOptions {
                captured_at: Some(captured),
                allow_library_calendar: true,
                ..CheckOptions::default()
            },
        )
        .map_err(|error| error.message().to_string())?;
        if gate
            .get("errors")
            .and_then(Value::as_array)
            .map(|errors| !errors.is_empty())
            .unwrap_or(false)
        {
            continue;
        }
        let mut market = market;
        market["_data_validation"] = gate.clone();
        if gate.get("forecast_eligible").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        pool.push(PoolEntry {
            symbol,
            market,
            bar_count: bars.len() as i64,
        });
    }
    Ok(pool)
}

/// Mean Brier of probability/outcome pairs.
pub fn brier(pairs: &[(f64, i64)]) -> Option<f64> {
    if pairs.is_empty() {
        return None;
    }
    Some(
        python_sum(
            pairs
                .iter()
                .map(|(probability, outcome)| (probability - *outcome as f64).powi(2)),
        ) / pairs.len() as f64,
    )
}

/// A forward-return label axis (one entry per bar).
pub fn forward_returns(closes: &[f64], horizon: usize) -> Vec<Option<f64>> {
    (0..closes.len())
        .map(|index| {
            if index + horizon >= closes.len() || closes[index] <= 0.0 {
                None
            } else {
                Some(closes[index + horizon] / closes[index] - 1.0)
            }
        })
        .collect()
}

fn quantile_index(fraction: f64, length: usize) -> usize {
    (fraction * (length - 1) as f64) as usize
}

/// Session-block bootstrap interval of a per-session series (`_session_block_interval`).
pub fn session_block_interval(series: &[f64], block_length: usize) -> Value {
    let count = series.len();
    let required = 20.max(4 * block_length);
    if count < required {
        let mut result = Map::new();
        result.insert(
            "status".to_string(),
            Value::String("insufficient_samples".into()),
        );
        result.insert("interval".to_string(), Value::Null);
        result.insert("observations".to_string(), Value::from(count));
        result.insert("required_sessions".to_string(), Value::from(required));
        return Value::Object(result);
    }
    let mut rng = PythonRandom::new(BOOTSTRAP_SEED);
    let mut means: Vec<f64> = Vec::new();
    while means.len() < BOOTSTRAP_REPLICATES {
        let mut sample: Vec<f64> = Vec::new();
        while sample.len() < count {
            let start = rng.randrange(count as u64) as usize;
            for offset in 0..block_length {
                sample.push(series[(start + offset) % count]);
            }
        }
        sample.truncate(count);
        means.push(python_sum(sample.iter().copied()) / sample.len() as f64);
    }
    means.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let mut result = Map::new();
    result.insert("status".to_string(), Value::String("exploratory".into()));
    result.insert("observations".to_string(), Value::from(count));
    result.insert("block_length".to_string(), Value::from(block_length));
    result.insert("replicates".to_string(), Value::from(means.len()));
    result.insert(
        "interval".to_string(),
        Value::Array(vec![
            Value::from(means[quantile_index(0.025, means.len())]),
            Value::from(means[quantile_index(0.975, means.len())]),
        ]),
    );
    result.insert("coverage_validated".to_string(), Value::Bool(false));
    Value::Object(result)
}

fn verdict_for(record: &Value) -> &'static str {
    if record.get("interval_status").and_then(Value::as_str) != Some("exploratory") {
        return "indeterminate";
    }
    let Some(interval) = record.get("interval").and_then(Value::as_array) else {
        return "indeterminate";
    };
    let low = interval.first().and_then(Value::as_f64).unwrap_or(0.0);
    let high = interval.get(1).and_then(Value::as_f64).unwrap_or(0.0);
    if high < 0.0 {
        "candidate_better"
    } else if low > 0.0 {
        "candidate_worse"
    } else {
        "indeterminate"
    }
}

/// Run the registered comparisons over the evaluated pool.
pub fn evaluate(pool: &[PoolEntry]) -> Result<Value, String> {
    let block_length = HORIZON_SESSIONS + 1;
    let mut per_symbol: Vec<Value> = Vec::new();
    type ArmForecasts = BTreeMap<String, (f64, i64)>;
    let mut pairs_by_arm: BTreeMap<String, Vec<ArmForecasts>> = BTreeMap::new();
    for entry in pool {
        let bars = entry
            .market
            .get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let close: Vec<f64> = bars
            .iter()
            .map(|bar| number(bar.get("close")).unwrap_or(0.0))
            .collect();
        let labels = forward_returns(&close, HORIZON_SESSIONS);
        let dates: Vec<String> = bars
            .iter()
            .map(|bar| {
                bar.get("date")
                    .map(sd_core::contracts::py_str)
                    .unwrap_or_default()
            })
            .collect();
        let manifest = serde_json::json!({
            "ticker": entry.symbol,
            "horizon": HORIZON_LABEL,
            "snapshot_id": format!("research-b:{}", entry.symbol),
        });
        let mut record = Map::new();
        record.insert("symbol".to_string(), Value::String(entry.symbol.clone()));
        record.insert("bar_count".to_string(), Value::from(entry.bar_count));
        for (arm, profile, bucket) in ARMS {
            let options = FactorOptions {
                selection_profile: Some(profile),
                bucket_mode: bucket,
                ..FactorOptions::default()
            };
            let result = run_with_options(&manifest, &entry.market, &options)?;
            let validation = result.get("validation").cloned().unwrap_or(Value::Null);
            let mut predictions: BTreeMap<String, (f64, i64)> = BTreeMap::new();
            for row in validation
                .get("oos_forecasts")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let probability = number(row.get("probability"));
                let session = row.get("decision_session").and_then(Value::as_str);
                let (Some(probability), Some(session)) = (probability, session) else {
                    continue;
                };
                let Some(index) = dates.iter().position(|day| day == session) else {
                    continue;
                };
                let Some(Some(label)) = labels.get(index).copied() else {
                    continue;
                };
                predictions.insert(session.to_string(), (probability, i64::from(label > 0.0)));
            }
            let mut arm_record = Map::new();
            arm_record.insert(
                "prob_up".to_string(),
                result.get("prob_up").cloned().unwrap_or(Value::Null),
            );
            arm_record.insert(
                "status".to_string(),
                result.get("status").cloned().unwrap_or(Value::Null),
            );
            arm_record.insert("predictions".to_string(), Value::from(predictions.len()));
            let arm_pairs: Vec<(f64, i64)> = predictions.values().copied().collect();
            arm_record.insert(
                "brier".to_string(),
                brier(&arm_pairs).map(Value::from).unwrap_or(Value::Null),
            );
            arm_record.insert(
                "factors".to_string(),
                Value::from(
                    validation
                        .get("selected_factors")
                        .and_then(Value::as_array)
                        .map(Vec::len)
                        .unwrap_or(0),
                ),
            );
            arm_record.insert(
                "bucket_counts".to_string(),
                validation
                    .get("calibration_bucket_counts")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            arm_record.insert(
                "route".to_string(),
                validation
                    .get("qualification")
                    .and_then(|qualification| qualification.get("route"))
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            record.insert(arm.to_string(), Value::Object(arm_record));
            pairs_by_arm
                .entry(arm.to_string())
                .or_default()
                .push(predictions);
        }
        per_symbol.push(Value::Object(record));
    }
    let mut comparisons = Map::new();
    for (question, reference, candidate) in QUESTIONS {
        let mut deltas: BTreeMap<String, Vec<f64>> = BTreeMap::new();
        for (index, _record) in per_symbol.iter().enumerate() {
            let ref_pairs = &pairs_by_arm[reference][index];
            let cand_pairs = &pairs_by_arm[candidate][index];
            for (session, (ref_probability, outcome)) in ref_pairs {
                let Some((cand_probability, _)) = cand_pairs.get(session) else {
                    continue;
                };
                deltas.entry(session.clone()).or_default().push(
                    (cand_probability - *outcome as f64).powi(2)
                        - (ref_probability - *outcome as f64).powi(2),
                );
            }
        }
        let series: Vec<f64> = deltas
            .values()
            .filter(|values| !values.is_empty())
            .map(|values| python_sum(values.iter().copied()) / values.len() as f64)
            .collect();
        let interval = session_block_interval(&series, block_length);
        let mut entry = Map::new();
        entry.insert("reference".to_string(), Value::String(reference.into()));
        entry.insert("candidate".to_string(), Value::String(candidate.into()));
        entry.insert("sessions".to_string(), Value::from(series.len()));
        entry.insert(
            "mean_delta".to_string(),
            if series.is_empty() {
                Value::Null
            } else {
                Value::from(python_sum(series.iter().copied()) / series.len() as f64)
            },
        );
        entry.insert(
            "interval".to_string(),
            interval.get("interval").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "interval_status".to_string(),
            interval.get("status").cloned().unwrap_or(Value::Null),
        );
        entry.insert(
            "required_sessions".to_string(),
            interval
                .get("required_sessions")
                .cloned()
                .unwrap_or_else(|| Value::from(20.max(4 * block_length))),
        );
        entry.insert(
            "delta_definition".to_string(),
            Value::String("positive means the candidate scored worse than production".into()),
        );
        entry.insert(
            "verdict".to_string(),
            Value::String(verdict_for(&Value::Object(entry.clone())).into()),
        );
        comparisons.insert(question.to_string(), Value::Object(entry));
    }
    let mut report = Map::new();
    report.insert("preregistration".to_string(), preregistration());
    let mut pool_block = Map::new();
    pool_block.insert("symbols".to_string(), Value::from(pool.len()));
    pool_block.insert("horizon".to_string(), Value::String(HORIZON_LABEL.into()));
    report.insert("pool".to_string(), Value::Object(pool_block));
    report.insert("comparisons".to_string(), Value::Object(comparisons));
    report.insert("production_unchanged".to_string(), Value::Bool(true));
    report.insert("per_symbol".to_string(), Value::Array(per_symbol));
    report.insert(
        "limitations".to_string(),
        Value::Array(vec![
            Value::String("Every arm shares the same 52-week window and the same anchored ledger phase; this compares designs, not added history.".into()),
            Value::String("The pool shares one market factor, so session-block resampling is the honest unit and the interval is wide by design.".into()),
            Value::String("A 52-week ledger yields few scorable sessions; an indeterminate verdict is the expected outcome, not a failure.".into()),
            Value::String("No calibration, trading cost, turnover or position sizing is evaluated.".into()),
        ]),
    );
    Ok(Value::Object(report))
}

/// Render the Research B candidate-comparison markdown.
pub fn render_markdown(report: &Value) -> String {
    let block_length = HORIZON_SESSIONS + 1;
    let comparisons = report["comparisons"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let first = comparisons.values().next().cloned().unwrap_or(Value::Null);
    let required = first
        .get("required_sessions")
        .and_then(Value::as_i64)
        .unwrap_or(20.max(4 * block_length) as i64);
    let mut lines: Vec<String> = vec![
        "# Research B candidate comparison (pre-registered)".into(),
        String::new(),
        format!(
            "- pool: {} symbols, horizon {}, block length {} sessions",
            report["pool"]["symbols"], HORIZON_LABEL, block_length
        ),
        "- reference arm: production (legacy selection, three buckets)".into(),
        String::new(),
        "| question | candidate | sessions | mean delta (Brier) | 95% interval | verdict |".into(),
        "| --- | --- | --- | --- | --- | --- |".into(),
    ];
    for (name, record) in &comparisons {
        let interval = record.get("interval").and_then(Value::as_array);
        let shown = match interval {
            Some(items) if items.len() >= 2 => format!(
                "[{:+.4}, {:+.4}]",
                items[0].as_f64().unwrap_or(0.0),
                items[1].as_f64().unwrap_or(0.0)
            ),
            _ => "n/a".to_string(),
        };
        let mean = match record.get("mean_delta").and_then(Value::as_f64) {
            Some(value) => format!("{value:+.4}"),
            None => "n/a".to_string(),
        };
        lines.push(format!(
            "| {name} | {} | {} | {mean} | {shown} | {} |",
            record["candidate"].as_str().unwrap_or(""),
            record["sessions"],
            record["verdict"].as_str().unwrap_or("")
        ));
    }
    lines.extend([
        String::new(),
        format!(
            "Interval needs at least {required} scorable sessions; below that the verdict is indeterminate by rule."
        ),
        String::new(),
        "Positive delta means the candidate scored worse than production. Production is unchanged either way.".into(),
        String::new(),
        "## Preregistration".into(),
        String::new(),
        "```json".into(),
        sd_core::json_text::python_pretty_ascii(&sd_core::json_text::sorted_value(&preregistration()))
            .trim_end()
            .to_string(),
        "```".into(),
    ]);
    let mut text = lines.join("\n");
    text.push('\n');
    text
}
