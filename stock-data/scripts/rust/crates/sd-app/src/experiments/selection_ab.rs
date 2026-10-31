//! Compare the two Research B factor-selection rules on a registered pool
//! (`selection_ab.py`).
//!
//! The question is narrow: does the dependency-aware selection estimator
//! (`overlapping_block`) change out-of-sample Brier versus the spaced point-estimate rule
//! (`non_overlapping_point`)? Both arms run the same production function on the same data
//! with the same evaluation boundary; only the selection estimator differs. Nothing here
//! switches production.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use sd_core::contracts::normalize_request;
use sd_core::data_checks::{check_market, CheckOptions};
use sd_core::float_sum::python_sum;
use sd_core::random::PythonRandom;
use sd_research::factor::{run_with_options, FactorOptions};

/// The two registered arms.
pub const ARMS: [(&str, &str); 2] = [("A", "non_overlapping_point"), ("B", "overlapping_block")];
const HORIZON_LABEL: &str = "5D";
const HORIZON_SESSIONS: usize = 5;
const BOOTSTRAP_REPLICATES: usize = 500;
const BOOTSTRAP_SEED: u64 = 0;

/// Registered protocol (Python literal order).
pub fn preregistration() -> Value {
    let mut map = Map::new();
    map.insert(
        "question".into(),
        Value::String(
            "Does dependency-aware factor selection (overlapping labels with a moving-block interval plus a direction-stability requirement) change out-of-sample Brier versus the spaced point-estimate rule?"
                .into(),
        ),
    );
    let mut arms = Map::new();
    arms.insert("A".into(), Value::String("non_overlapping_point".into()));
    arms.insert("B".into(), Value::String("overlapping_block".into()));
    map.insert("arms".into(), Value::Object(arms));
    map.insert("horizon".into(), Value::String(HORIZON_LABEL.into()));
    map.insert(
        "primary_metric".into(),
        Value::String("mean_per_session_brier_delta_B_minus_A".into()),
    );
    let mut bootstrap = Map::new();
    bootstrap.insert("unit".into(), Value::String("session_block".into()));
    bootstrap.insert("block_length".into(), Value::String("horizon + 1".into()));
    bootstrap.insert("replicates".into(), Value::from(500));
    bootstrap.insert("seed".into(), Value::from(0));
    map.insert("bootstrap".into(), Value::Object(bootstrap));
    let mut decision = Map::new();
    decision.insert(
        "B_better".into(),
        Value::String("interval entirely below zero".into()),
    );
    decision.insert(
        "A_better".into(),
        Value::String("interval entirely above zero".into()),
    );
    decision.insert(
        "indistinguishable".into(),
        Value::String("interval contains zero (the expected outcome on this sample)".into()),
    );
    map.insert("decision".into(), Value::Object(decision));
    map.insert(
        "pairing".into(),
        Value::String(
            "decision_session, only sessions where both arms produced a prediction".into(),
        ),
    );
    map.insert(
        "coverage".into(),
        Value::String(
            "reported, never used to discard hard sessions from the score comparison".into(),
        ),
    );
    map.insert(
        "default_if_indistinguishable".into(),
        Value::String("keep the incumbent rule".into()),
    );
    Value::Object(map)
}

/// One pool entry with its frozen market payload.
#[derive(Debug, Clone)]
pub struct PoolEntry {
    /// Symbol.
    pub symbol: String,
    /// Declared asset type.
    pub asset_type: String,
    /// Frozen market payload (with `_data_validation` from the gate).
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

/// Filter the registered pool to the entries that pass the frozen gates.
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
            asset_type,
            market,
            bar_count: bars.len() as i64,
        });
    }
    Ok(pool)
}

/// Forecasts of one arm keyed by decision session.
pub fn forecasts(result: &Value) -> BTreeMap<String, (f64, i64)> {
    let mut out: BTreeMap<String, (f64, i64)> = BTreeMap::new();
    for record in result
        .get("validation")
        .and_then(|validation| validation.get("oos_forecasts"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let probability = number(record.get("probability"));
        let target = number(record.get("target"));
        let session = record.get("decision_session").and_then(Value::as_str);
        let (Some(probability), Some(target), Some(session)) = (probability, target, session)
        else {
            continue;
        };
        out.insert(session.to_string(), (probability, target as i64));
    }
    out
}

/// Mean Brier score of the paired predictions.
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

fn quantile_index(fraction: f64, length: usize) -> usize {
    (fraction * (length - 1) as f64) as usize
}

/// Session-block bootstrap interval of a per-session series.
pub fn session_block_interval(
    series: &[f64],
    block_length: usize,
    replicates: usize,
    seed: u64,
) -> Value {
    let count = series.len();
    if count < 20.max(4 * block_length) {
        let mut result = Map::new();
        result.insert(
            "status".into(),
            Value::String("insufficient_samples".into()),
        );
        result.insert("interval".into(), Value::Null);
        result.insert("observations".into(), Value::from(count));
        return Value::Object(result);
    }
    let mut rng = PythonRandom::new(seed);
    let mut means: Vec<f64> = Vec::new();
    while means.len() < replicates {
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
    result.insert("status".into(), Value::String("exploratory".into()));
    result.insert("observations".into(), Value::from(count));
    result.insert("block_length".into(), Value::from(block_length));
    result.insert("replicates".into(), Value::from(means.len()));
    result.insert(
        "interval".into(),
        Value::Array(vec![
            Value::from(means[quantile_index(0.025, means.len())]),
            Value::from(means[quantile_index(0.975, means.len())]),
        ]),
    );
    result.insert("coverage_validated".into(), Value::Bool(false));
    Value::Object(result)
}

/// Symbol-cluster bootstrap interval (disclosure only, never the decision input).
pub fn symbol_cluster_interval(values: &[f64], replicates: usize, seed: u64) -> Value {
    let count = values.len();
    if count < 8 {
        let mut result = Map::new();
        result.insert(
            "status".into(),
            Value::String("insufficient_samples".into()),
        );
        result.insert("interval".into(), Value::Null);
        result.insert("observations".into(), Value::from(count));
        return Value::Object(result);
    }
    let mut rng = PythonRandom::new(seed);
    let mut means: Vec<f64> = Vec::new();
    for _ in 0..replicates {
        let mut sample: Vec<f64> = Vec::with_capacity(count);
        for _ in 0..count {
            sample.push(values[rng.randrange(count as u64) as usize]);
        }
        means.push(python_sum(sample.iter().copied()) / count as f64);
    }
    means.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let mut result = Map::new();
    result.insert("status".into(), Value::String("exploratory".into()));
    result.insert("observations".into(), Value::from(count));
    result.insert("replicates".into(), Value::from(means.len()));
    result.insert(
        "interval".into(),
        Value::Array(vec![
            Value::from(means[quantile_index(0.025, means.len())]),
            Value::from(means[quantile_index(0.975, means.len())]),
        ]),
    );
    result.insert("unit".into(), Value::String("symbol_cluster".into()));
    result.insert(
        "understates_cross_sectional_dependence".into(),
        Value::Bool(true),
    );
    result.insert("used_for_decision".into(), Value::Bool(false));
    Value::Object(result)
}

fn interval_value(interval: &Value) -> Value {
    interval.get("interval").cloned().unwrap_or(Value::Null)
}

/// Run the registered comparison over the evaluated pool.
pub fn evaluate(pool: &[PoolEntry]) -> Value {
    let block_length = HORIZON_SESSIONS + 1;
    let mut per_symbol: Vec<Value> = Vec::new();
    let mut deltas_by_session: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    let mut pairs_a: Vec<(f64, i64)> = Vec::new();
    let mut pairs_b: Vec<(f64, i64)> = Vec::new();
    for entry in pool {
        let manifest = serde_json::json!({
            "ticker": entry.symbol,
            "horizon": HORIZON_LABEL,
            "snapshot_id": format!("selection-ab:{}", entry.symbol),
        });
        let mut record = Map::new();
        record.insert("symbol".into(), Value::String(entry.symbol.clone()));
        record.insert("asset_type".into(), Value::String(entry.asset_type.clone()));
        record.insert("bar_count".into(), Value::from(entry.bar_count));
        let mut arm_forecasts: BTreeMap<String, BTreeMap<String, (f64, i64)>> = BTreeMap::new();
        for (arm, metric) in ARMS {
            let options = FactorOptions {
                selection_override: Some(metric),
                ..FactorOptions::default()
            };
            let result = match run_with_options(&manifest, &entry.market, &options) {
                Ok(result) => result,
                Err(error) => {
                    let mut record = record.clone();
                    record.insert("error".into(), Value::String(error));
                    per_symbol.push(Value::Object(record));
                    return Value::Object(Map::new());
                }
            };
            let arm_data = forecasts(&result);
            record.insert(format!("{arm}_coverage"), Value::from(arm_data.len()));
            record.insert(
                format!("{arm}_status"),
                result.get("status").cloned().unwrap_or(Value::Null),
            );
            record.insert(
                format!("{arm}_prob_up"),
                result.get("prob_up").cloned().unwrap_or(Value::Null),
            );
            let factors: Vec<Value> = result["validation"]["selected_factors"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .map(|item| item.get("factor_name").cloned().unwrap_or(Value::Null))
                        .collect()
                })
                .unwrap_or_default();
            record.insert(format!("{arm}_factors"), Value::Array(factors));
            arm_forecasts.insert(arm.to_string(), arm_data);
        }
        let common: Vec<String> = arm_forecasts["A"]
            .keys()
            .filter(|session| arm_forecasts["B"].contains_key(*session))
            .cloned()
            .collect();
        record.insert("common_dates".into(), Value::from(common.len()));
        if !common.is_empty() {
            let values_a: Vec<(f64, i64)> = common
                .iter()
                .map(|session| arm_forecasts["A"][session])
                .collect();
            let values_b: Vec<(f64, i64)> = common
                .iter()
                .map(|session| arm_forecasts["B"][session])
                .collect();
            let brier_a = brier(&values_a);
            let brier_b = brier(&values_b);
            record.insert(
                "brier_A".into(),
                brier_a.map(Value::from).unwrap_or(Value::Null),
            );
            record.insert(
                "brier_B".into(),
                brier_b.map(Value::from).unwrap_or(Value::Null),
            );
            record.insert(
                "delta_B_minus_A".into(),
                match (brier_a, brier_b) {
                    (Some(a), Some(b)) => Value::from(b - a),
                    _ => Value::Null,
                },
            );
            pairs_a.extend(values_a.iter().copied());
            pairs_b.extend(values_b.iter().copied());
            for session in &common {
                let (probability_a, outcome) = arm_forecasts["A"][session];
                let (probability_b, _) = arm_forecasts["B"][session];
                deltas_by_session.entry(session.clone()).or_default().push(
                    (probability_b - outcome as f64).powi(2)
                        - (probability_a - outcome as f64).powi(2),
                );
            }
        }
        per_symbol.push(Value::Object(record));
    }
    let session_series: Vec<f64> = deltas_by_session
        .values()
        .filter(|values| !values.is_empty())
        .map(|values| python_sum(values.iter().copied()) / values.len() as f64)
        .collect();
    let symbol_series: Vec<f64> = per_symbol
        .iter()
        .filter_map(|record| record.get("delta_B_minus_A").and_then(Value::as_f64))
        .collect();
    let pooled_delta = if session_series.is_empty() {
        Value::Null
    } else {
        Value::from(python_sum(session_series.iter().copied()) / session_series.len() as f64)
    };
    let interval = session_block_interval(
        &session_series,
        block_length,
        BOOTSTRAP_REPLICATES,
        BOOTSTRAP_SEED,
    );
    let low = interval
        .get("interval")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(Value::as_f64);
    let high = interval
        .get("interval")
        .and_then(Value::as_array)
        .and_then(|items| items.get(1))
        .and_then(Value::as_f64);
    let verdict = match (low, high) {
        (Some(_), Some(high)) if high < 0.0 => "new_selection_better",
        (Some(low), Some(_)) if low > 0.0 => "incumbent_selection_better",
        (Some(_), Some(_)) => "indistinguishable",
        _ => "indeterminate",
    };
    let equities = pool
        .iter()
        .filter(|entry| matches!(entry.asset_type.as_str(), "stock" | "equity"))
        .count();
    let etfs = pool.len() - equities;
    let mut report = Map::new();
    report.insert("preregistration".into(), preregistration());
    let mut pool_block = Map::new();
    pool_block.insert("symbols".into(), Value::from(pool.len()));
    pool_block.insert("equities".into(), Value::from(equities));
    pool_block.insert("etfs".into(), Value::from(etfs));
    pool_block.insert("shared_calendar".into(), Value::Bool(true));
    report.insert("pool".into(), Value::Object(pool_block));
    let mut pooled = Map::new();
    pooled.insert("scored_pairs_arm_A".into(), Value::from(pairs_a.len()));
    pooled.insert("scored_pairs_arm_B".into(), Value::from(pairs_b.len()));
    pooled.insert(
        "brier_arm_A".into(),
        brier(&pairs_a).map(Value::from).unwrap_or(Value::Null),
    );
    pooled.insert(
        "brier_arm_B".into(),
        brier(&pairs_b).map(Value::from).unwrap_or(Value::Null),
    );
    pooled.insert("mean_per_session_delta_B_minus_A".into(), pooled_delta);
    pooled.insert("session_block_interval".into(), interval_value(&interval));
    pooled.insert(
        "session_block_interval_status".into(),
        interval.get("status").cloned().unwrap_or(Value::Null),
    );
    pooled.insert("sessions_scored".into(), Value::from(session_series.len()));
    pooled.insert(
        "delta_definition".into(),
        Value::String("positive means the new rule scored worse".into()),
    );
    report.insert("pooled".into(), Value::Object(pooled));
    let mut secondary = Map::new();
    secondary.insert(
        "symbol_cluster".into(),
        symbol_cluster_interval(&symbol_series, BOOTSTRAP_REPLICATES, BOOTSTRAP_SEED),
    );
    secondary.insert("symbols_scored".into(), Value::from(symbol_series.len()));
    secondary.insert(
        "note".into(),
        Value::String(
            "Answers a different question than the registered test and is reported only so the reader can see whether the sign is stable across names."
                .into(),
        ),
    );
    report.insert("secondary_disclosure".into(), Value::Object(secondary));
    let mut decision = Map::new();
    decision.insert("verdict".into(), Value::String(verdict.into()));
    decision.insert("rule".into(), preregistration()["decision"].clone());
    decision.insert(
        "applies_to".into(),
        Value::String("this registered pool and this horizon only".into()),
    );
    report.insert("decision".into(), Value::Object(decision));
    report.insert("per_symbol".into(), Value::Array(per_symbol));
    report.insert(
        "limitations".into(),
        Value::Array(vec![
            Value::String("Both arms share the same 52-week window per symbol; this compares selection estimators, not added history.".into()),
            Value::String("The pool shares one market factor, so session-block resampling is the honest unit and the interval is wide by design.".into()),
            Value::String("Brier on ten to twenty forecasts per symbol is a coarse instrument; the pooled figure is what the decision uses.".into()),
            Value::String("No calibration, no trading cost and no position sizing are evaluated here.".into()),
        ]),
    );
    Value::Object(report)
}

/// Render the selection-rule comparison markdown.
pub fn render_markdown(report: &Value) -> String {
    let block_length = HORIZON_SESSIONS + 1;
    let pooled = &report["pooled"];
    let lines: Vec<String> = vec![
        "# Research B selection-rule comparison".into(),
        String::new(),
        format!(
            "- pool: {} symbols ({} equities, {} ETFs), shared calendar",
            report["pool"]["symbols"], report["pool"]["equities"], report["pool"]["etfs"]
        ),
        format!("- horizon: {HORIZON_LABEL}; block length: {block_length} sessions"),
        format!(
            "- scored pairs: arm A {}, arm B {}",
            pooled["scored_pairs_arm_A"], pooled["scored_pairs_arm_B"]
        ),
        format!(
            "- Brier arm A (spaced point estimate): {}",
            pooled["brier_arm_A"]
        ),
        format!(
            "- Brier arm B (overlapping block): {}",
            pooled["brier_arm_B"]
        ),
        format!(
            "- mean per-session delta (B - A): {}",
            pooled["mean_per_session_delta_B_minus_A"]
        ),
        format!(
            "- session-block 95% interval: {} ({})",
            sd_core::contracts::python_str(&pooled["session_block_interval"]),
            sd_core::contracts::python_str(&pooled["session_block_interval_status"])
        ),
        format!(
            "- **verdict: {}**",
            report["decision"]["verdict"].as_str().unwrap_or("")
        ),
        String::new(),
        "Positive delta means the new rule scored worse. The interval is the decision input;"
            .into(),
        "the point estimate is reported for completeness only.".into(),
        String::new(),
        "## Preregistration".into(),
        String::new(),
        "```json".into(),
        sd_core::json_text::python_pretty_ascii(&sd_core::json_text::sorted_value(
            &preregistration(),
        ))
        .trim_end()
        .to_string(),
        "```".into(),
    ];
    let mut text = lines.join("\n");
    text.push('\n');
    text
}
