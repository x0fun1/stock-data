//! Pre-registered A vs B comparison for the 20-session composite momentum feature
//! (`momentum_ab.py`).
//!
//! Signal A is the raw 20-session ROC (the incumbent quantity); signal B is the frozen
//! composite score. This module is an *evaluation*: it never writes `prob_up`, never fits
//! weights, never retunes the tanh scale and never reweights the ensemble. The decision
//! rule lives in [`preregistration`] and is emitted verbatim into every report.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};

use sd_core::float_sum::math_fsum;
use sd_core::random::PythonRandom;
use sd_research::math_utils::spearman;
use sd_research::momentum::score_at;

/// Frozen component weights (order is the report order).
pub const COMPONENT_WEIGHTS: [(&str, f64); 3] = [
    ("risk_adjusted", 0.5),
    ("regression_trend", 0.3),
    ("relative_strength", 0.2),
];
const PRIMARY_HORIZONS: [i64; 2] = [5, 10];
const REPORT_ONLY_HORIZONS: [i64; 1] = [20];
const MIN_NAMES_PER_DATE: usize = 12;
const BOOTSTRAP_ITERATIONS: usize = 2000;
const BOOTSTRAP_CONFIDENCE: f64 = 0.95;
const BOOTSTRAP_SEED: u64 = 20261019;

/// The registered protocol as JSON (key order = the Python literal order).
pub fn preregistration() -> Value {
    let mut map = Map::new();
    map.insert("protocol".into(), Value::String("momentum-ab-v1".into()));
    map.insert("registered".into(), Value::String("2026-10-19".into()));
    map.insert(
        "signal_a".into(),
        Value::String("raw_20_session_roc".into()),
    );
    map.insert(
        "signal_b".into(),
        Value::String("composite_20_session_score".into()),
    );
    map.insert(
        "metric".into(),
        Value::String("mean_cross_sectional_spearman_ic".into()),
    );
    map.insert(
        "primary_horizons".into(),
        Value::Array(vec![Value::from(5), Value::from(10)]),
    );
    map.insert(
        "report_only_horizons".into(),
        Value::Array(vec![Value::from(20)]),
    );
    map.insert("min_names_per_date".into(), Value::from(12));
    map.insert("bootstrap".into(), Value::String("circular_block".into()));
    map.insert(
        "bootstrap_block_length".into(),
        Value::String("horizon_plus_one".into()),
    );
    map.insert("bootstrap_iterations".into(), Value::from(2000));
    map.insert("bootstrap_confidence".into(), Value::from(0.95));
    map.insert("bootstrap_seed".into(), Value::from(20261019));
    map.insert(
        "verdict_rule".into(),
        Value::String(
            "composite_better: delta_ic > 0 and the 95% paired interval excludes zero on every primary horizon; composite_worse: delta_ic < 0 and the 95% paired interval excludes zero on every primary horizon; otherwise indistinguishable".into(),
        ),
    );
    map.insert(
        "report_only_note".into(),
        Value::String(
            "20-session results are reported but never decide the verdict: roughly ten independent blocks cannot support a claim at that horizon.".into(),
        ),
    );
    map.insert(
        "weight_claim".into(),
        Value::String(
            "the 50/30/20 weights stay frozen regardless of the outcome; no re-fitting is performed by this protocol".into(),
        ),
    );
    Value::Object(map)
}

/// Date-aligned close panel.
#[derive(Debug, Clone, Default)]
pub struct Panel {
    /// Session axis (benchmark sessions).
    pub dates: Vec<String>,
    /// Symbol -> closes aligned to `dates`, in panel order.
    pub closes: Vec<(String, Vec<f64>)>,
    /// Benchmark closes per session date.
    pub benchmark: BTreeMap<String, f64>,
    /// Symbols excluded from the panel, with the reason.
    pub dropped: BTreeMap<String, String>,
    /// Declared asset types from the pool manifest (never inferred from prices).
    pub asset_types: BTreeMap<String, String>,
    /// Path/identity of the pool manifest for disclosure.
    pub pool_manifest: Option<String>,
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

/// Load collected market payloads into a date-aligned close panel.
pub fn load_panel(
    payloads: &Map<String, Value>,
    pool_manifest: Option<&Map<String, Value>>,
) -> Result<Panel, String> {
    if payloads.is_empty() {
        return Err("no collection payloads under the panel".into());
    }
    let mut keys: Vec<String> = payloads.keys().cloned().collect();
    keys.sort();
    let mut series: BTreeMap<String, BTreeMap<String, f64>> = BTreeMap::new();
    let mut dropped: BTreeMap<String, String> = BTreeMap::new();
    for key in keys {
        let symbol = key.to_uppercase();
        let payload = &payloads[&key];
        let market = payload.get("market").cloned().unwrap_or(Value::Null);
        let bars = market
            .get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut closes: BTreeMap<String, f64> = BTreeMap::new();
        for bar in bars {
            let day = bar.get("date").and_then(Value::as_str).unwrap_or("");
            let close = number(bar.get("adj_close"));
            if day.is_empty() || close.is_none() || close.unwrap_or(0.0) <= 0.0 {
                continue;
            }
            closes.insert(day.to_string(), close.unwrap_or(0.0));
        }
        if closes.is_empty() {
            dropped.insert(symbol.clone(), "no_usable_bars".into());
            continue;
        }
        series.insert(symbol, closes);
    }
    let Some(axis_closes) = series.get("SPY") else {
        return Err("benchmark SPY missing from the panel".into());
    };
    let dates: Vec<String> = axis_closes.keys().cloned().collect();
    let mut aligned: Vec<(String, Vec<f64>)> = Vec::new();
    for (symbol, closes) in &series {
        if symbol == "SPY" {
            continue;
        }
        let missing = dates
            .iter()
            .filter(|day| !closes.contains_key(*day))
            .count();
        if missing > 0 {
            dropped.insert(
                symbol.clone(),
                format!("axis_mismatch:{missing}_missing_sessions"),
            );
            continue;
        }
        let values: Vec<f64> = dates.iter().map(|day| closes[day]).collect();
        aligned.push((symbol.clone(), values));
    }
    let benchmark: BTreeMap<String, f64> = dates
        .iter()
        .map(|day| (day.clone(), axis_closes[day]))
        .collect();
    let asset_types = declared_asset_types(pool_manifest, &aligned);
    Ok(Panel {
        dates,
        closes: aligned,
        benchmark,
        dropped,
        asset_types,
        pool_manifest: pool_manifest
            .and_then(|manifest| manifest.get("manifest_path"))
            .and_then(Value::as_str)
            .map(ToString::to_string),
    })
}

fn declared_asset_types(
    pool_manifest: Option<&Map<String, Value>>,
    symbols: &[(String, Vec<f64>)],
) -> BTreeMap<String, String> {
    let Some(manifest) = pool_manifest else {
        return BTreeMap::new();
    };
    let known: BTreeSet<&str> = symbols.iter().map(|(symbol, _)| symbol.as_str()).collect();
    let mut declared: BTreeMap<String, String> = BTreeMap::new();
    for entry in manifest
        .get("symbols")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let symbol = entry
            .get("symbol")
            .map(sd_core::contracts::py_str)
            .unwrap_or_default()
            .to_uppercase();
        let asset_type = entry.get("asset_type").and_then(Value::as_str);
        if known.contains(symbol.as_str()) {
            if let Some(asset_type) = asset_type {
                declared.insert(symbol, asset_type.to_string());
            }
        }
    }
    declared
}

/// Panel restricted to the given symbols (disclosure slice).
pub fn subset_panel(panel: &Panel, symbols: &BTreeSet<String>) -> Panel {
    Panel {
        dates: panel.dates.clone(),
        closes: panel
            .closes
            .iter()
            .filter(|(symbol, _)| symbols.contains(symbol))
            .cloned()
            .collect(),
        benchmark: panel.benchmark.clone(),
        dropped: BTreeMap::new(),
        asset_types: panel
            .asset_types
            .iter()
            .filter(|(symbol, _)| symbols.contains(*symbol))
            .map(|(symbol, kind)| (symbol.clone(), kind.clone()))
            .collect(),
        pool_manifest: None,
    }
}

/// Plain 20-session rate of change; the incumbent signal, left unadjusted.
pub fn roc(closes: &[f64], index: usize, window: usize) -> Option<f64> {
    if index < window {
        return None;
    }
    let base = closes[index - window];
    if base <= 0.0 {
        return None;
    }
    Some(closes[index] / base - 1.0)
}

/// Forward return over the horizon, or `None` when immature.
pub fn forward_return(closes: &[f64], index: usize, horizon: usize) -> Option<f64> {
    if index + horizon >= closes.len() {
        return None;
    }
    let base = closes[index];
    if base <= 0.0 {
        return None;
    }
    Some(closes[index + horizon] / base - 1.0)
}

/// Re-normalised composite with one component removed, for ablation only.
pub fn ablation_scores(contributions: &Map<String, Value>) -> Map<String, Value> {
    let mut scores = Map::new();
    for (removed, weight) in COMPONENT_WEIGHTS {
        let remaining = 1.0 - weight;
        if remaining <= 0.0 {
            scores.insert(removed.into(), Value::Null);
            continue;
        }
        let mut total = 0.0;
        let mut usable = true;
        for (name, contribution) in contributions {
            if name == removed {
                continue;
            }
            let Some(value) = contribution.as_f64() else {
                usable = false;
                break;
            };
            total += value;
        }
        scores.insert(
            removed.into(),
            if usable {
                Value::from(total / remaining)
            } else {
                Value::Null
            },
        );
    }
    scores
}

/// Per-session cross-section of both signals, their ablations and the label.
#[derive(Debug, Clone)]
pub struct Row {
    /// Symbol of the row.
    pub symbol: String,
    /// Raw 20-session ROC.
    pub a: f64,
    /// Frozen composite score.
    pub b: f64,
    /// Per-component ablation composites.
    pub ablations: Map<String, Value>,
    /// Forward return label.
    pub label: f64,
}

/// Cross-sectional IC of two aligned lists.
pub fn cross_sectional_ic(scores: &[f64], labels: &[f64]) -> Result<Option<f64>, String> {
    if scores.len() != labels.len() {
        return Err("score and label counts differ".into());
    }
    Ok(spearman(scores, labels))
}

/// Circular block bootstrap interval for the mean of a session-indexed series.
pub fn block_bootstrap_mean_ci(
    values: &[f64],
    block_length: usize,
    iterations: usize,
    confidence: f64,
    seed: u64,
) -> Option<(f64, f64)> {
    let count = values.len();
    if count == 0 {
        return None;
    }
    let block = block_length.clamp(1, count);
    let needed = count.div_ceil(block);
    let mut rng = PythonRandom::new(seed);
    let mut means: Vec<f64> = Vec::new();
    for _ in 0..iterations.max(1) {
        let mut total = 0.0;
        let mut taken = 0usize;
        for _ in 0..needed {
            let start = rng.randrange(count as u64) as usize;
            for offset in 0..block {
                if taken >= count {
                    break;
                }
                total += values[(start + offset) % count];
                taken += 1;
            }
        }
        means.push(total / count as f64);
    }
    means.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let alpha = (1.0 - confidence) / 2.0;
    let low_index = (alpha * (means.len() - 1) as f64).floor().max(0.0) as usize;
    let high_index = ((1.0 - alpha) * (means.len() - 1) as f64)
        .ceil()
        .min((means.len() - 1) as f64) as usize;
    Some((means[low_index], means[high_index]))
}

fn benchmark_value(panel: &Panel) -> Value {
    let mut map = Map::new();
    for (day, close) in &panel.benchmark {
        map.insert(day.clone(), Value::from(*close));
    }
    Value::Object(map)
}

fn collect_rows(panel: &Panel, horizon: usize) -> BTreeMap<String, Vec<Row>> {
    let benchmark = benchmark_value(panel);
    let mut by_date: BTreeMap<String, Vec<Row>> = BTreeMap::new();
    let last_index = panel.dates.len() as i64 - horizon as i64 - 1;
    let mut index = last_index;
    while index >= 0 {
        let index_usize = index as usize;
        let mut rows: Vec<Row> = Vec::new();
        for (symbol, closes) in &panel.closes {
            let Some(label) = forward_return(closes, index_usize, horizon) else {
                continue;
            };
            let Some(signal_a) = roc(closes, index_usize, 20) else {
                continue;
            };
            let evaluated = score_at(closes, &panel.dates, Some(&benchmark), index_usize, None);
            if evaluated.get("status").and_then(Value::as_str) != Some("valid") {
                continue;
            }
            let components = evaluated
                .get("components")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut contributions = Map::new();
            for (name, _) in COMPONENT_WEIGHTS {
                let value = components
                    .get(name)
                    .and_then(|component| component.get("contribution"))
                    .cloned()
                    .unwrap_or(Value::Null);
                contributions.insert(name.to_string(), value);
            }
            if contributions.values().any(|value| !value.is_number()) {
                continue;
            }
            rows.push(Row {
                symbol: symbol.clone(),
                a: signal_a,
                b: evaluated
                    .get("score")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                ablations: ablation_scores(&contributions),
                label,
            });
        }
        if !rows.is_empty() {
            by_date.insert(panel.dates[index_usize].clone(), rows);
        }
        index -= 1;
    }
    by_date
}

fn ic_by_date(by_date: &BTreeMap<String, Vec<Row>>, key: &str) -> BTreeMap<String, f64> {
    let mut series: BTreeMap<String, f64> = BTreeMap::new();
    for (day, rows) in by_date {
        let mut scores: Vec<f64> = Vec::new();
        let mut usable = true;
        match key {
            "a" => scores.extend(rows.iter().map(|row| row.a)),
            "b" => scores.extend(rows.iter().map(|row| row.b)),
            other => {
                let name = other.split_once(':').map(|(_, name)| name).unwrap_or("");
                for row in rows {
                    match row.ablations.get(name).and_then(Value::as_f64) {
                        Some(value) => scores.push(value),
                        None => {
                            usable = false;
                            break;
                        }
                    }
                }
            }
        }
        if !usable {
            continue;
        }
        let labels: Vec<f64> = rows.iter().map(|row| row.label).collect();
        if let Ok(Some(ic)) = cross_sectional_ic(&scores, &labels) {
            series.insert(day.clone(), ic);
        }
    }
    series
}

fn interval_value(interval: Option<(f64, f64)>) -> Value {
    match interval {
        Some((low, high)) => Value::Array(vec![Value::from(low), Value::from(high)]),
        None => Value::Null,
    }
}

/// Evaluate one horizon under the registered protocol.
pub fn evaluate_horizon(panel: &Panel, horizon: usize) -> Value {
    let by_date = collect_rows(panel, horizon);
    let usable: BTreeMap<String, Vec<Row>> = by_date
        .into_iter()
        .filter(|(_, rows)| rows.len() >= MIN_NAMES_PER_DATE)
        .collect();
    let ic_a = ic_by_date(&usable, "a");
    let ic_b = ic_by_date(&usable, "b");
    let shared: Vec<String> = ic_a
        .keys()
        .filter(|day| ic_b.contains_key(*day))
        .cloned()
        .collect();
    if shared.is_empty() {
        let mut result = Map::new();
        result.insert("horizon".into(), Value::from(horizon));
        result.insert("status".into(), Value::String("no_usable_sessions".into()));
        result.insert("usable_sessions".into(), Value::from(0));
        return Value::Object(result);
    }
    let series_a: Vec<f64> = shared.iter().map(|day| ic_a[day]).collect();
    let series_b: Vec<f64> = shared.iter().map(|day| ic_b[day]).collect();
    let count = shared.len();
    let delta: Vec<f64> = series_a
        .iter()
        .zip(series_b.iter())
        .map(|(a, b)| b - a)
        .collect();
    let block = horizon + 1;
    let mean_a = math_fsum(series_a.iter().copied()) / count as f64;
    let mean_b = math_fsum(series_b.iter().copied()) / count as f64;
    let mut result = Map::new();
    result.insert("horizon".into(), Value::from(horizon));
    result.insert("status".into(), Value::String("evaluated".into()));
    result.insert("usable_sessions".into(), Value::from(count));
    result.insert(
        "independent_blocks".into(),
        Value::from((count / block).max(1)),
    );
    result.insert("mean_ic_a".into(), Value::from(mean_a));
    result.insert("mean_ic_b".into(), Value::from(mean_b));
    result.insert("delta_ic".into(), Value::from(mean_b - mean_a));
    result.insert(
        "ci_a".into(),
        interval_value(block_bootstrap_mean_ci(
            &series_a,
            block,
            BOOTSTRAP_ITERATIONS,
            BOOTSTRAP_CONFIDENCE,
            BOOTSTRAP_SEED,
        )),
    );
    result.insert(
        "ci_b".into(),
        interval_value(block_bootstrap_mean_ci(
            &series_b,
            block,
            BOOTSTRAP_ITERATIONS,
            BOOTSTRAP_CONFIDENCE,
            BOOTSTRAP_SEED,
        )),
    );
    result.insert(
        "ci_delta".into(),
        interval_value(block_bootstrap_mean_ci(
            &delta,
            block,
            BOOTSTRAP_ITERATIONS,
            BOOTSTRAP_CONFIDENCE,
            BOOTSTRAP_SEED,
        )),
    );
    result.insert("first_session".into(), Value::String(shared[0].clone()));
    result.insert(
        "last_session".into(),
        Value::String(shared[count - 1].clone()),
    );
    let mut ablations = Map::new();
    for (name, _) in COMPONENT_WEIGHTS {
        let series = ic_by_date(&usable, &format!("ablate:{name}"));
        let paired: Vec<f64> = shared
            .iter()
            .filter_map(|day| series.get(day).copied())
            .collect();
        if paired.len() != count {
            let mut entry = Map::new();
            entry.insert("status".into(), Value::String("incomplete".into()));
            entry.insert("sessions".into(), Value::from(paired.len()));
            entry.insert("expected_sessions".into(), Value::from(count));
            ablations.insert(name.into(), Value::Object(entry));
            continue;
        }
        let mean = math_fsum(paired.iter().copied()) / count as f64;
        let mut entry = Map::new();
        entry.insert("status".into(), Value::String("evaluated".into()));
        entry.insert("sessions".into(), Value::from(count));
        entry.insert("mean_ic_without".into(), Value::from(mean));
        entry.insert("delta_vs_full".into(), Value::from(mean - mean_b));
        ablations.insert(name.into(), Value::Object(entry));
    }
    result.insert("ablation".into(), Value::Object(ablations));
    Value::Object(result)
}

/// Apply the registered rule; nothing here looks at the ablation output.
pub fn decide_verdict(results: &Map<String, Value>) -> &'static str {
    let mut better: Vec<bool> = Vec::new();
    let mut worse: Vec<bool> = Vec::new();
    for horizon in PRIMARY_HORIZONS {
        let Some(record) = results.get(&horizon.to_string()) else {
            return "indistinguishable";
        };
        if record.get("status").and_then(Value::as_str) != Some("evaluated") {
            return "indistinguishable";
        }
        let Some(interval) = record.get("ci_delta").and_then(Value::as_array) else {
            return "indistinguishable";
        };
        let Some(delta) = record.get("delta_ic").and_then(Value::as_f64) else {
            return "indistinguishable";
        };
        let low = interval.first().and_then(Value::as_f64).unwrap_or(0.0);
        let high = interval.get(1).and_then(Value::as_f64).unwrap_or(0.0);
        better.push(delta > 0.0 && low > 0.0);
        worse.push(delta < 0.0 && high < 0.0);
    }
    if !better.is_empty() && better.iter().all(|flag| *flag) {
        return "composite_better";
    }
    if !worse.is_empty() && worse.iter().all(|flag| *flag) {
        return "composite_worse";
    }
    "indistinguishable"
}

fn verdict_claim(verdict: &str) -> &'static str {
    match verdict {
        "composite_better" => "supported_in_registered_pool",
        "composite_worse" => "refuted_in_registered_pool",
        _ => "not_established",
    }
}

/// Whether the verdict survives dropping the declared non-stock slice.
pub fn assess_durability(verdict: &str, robustness: &Value) -> &'static str {
    if robustness.get("status").and_then(Value::as_str) != Some("evaluated") {
        return "not_assessed";
    }
    if robustness.get("verdict").and_then(Value::as_str) == Some(verdict) {
        "consistent_across_pool_slices"
    } else {
        "pool_dependent"
    }
}

fn robustness_slice(panel: &Panel, horizons: &[usize]) -> Value {
    let stocks: BTreeSet<String> = panel
        .asset_types
        .iter()
        .filter(|(_, kind)| kind.as_str() == "stock")
        .map(|(symbol, _)| symbol.clone())
        .collect();
    let others: BTreeSet<String> = panel
        .asset_types
        .iter()
        .filter(|(_, kind)| kind.as_str() != "stock")
        .map(|(symbol, _)| symbol.clone())
        .collect();
    if stocks.is_empty() || others.is_empty() {
        let mut result = Map::new();
        result.insert("status".into(), Value::String("not_applicable".into()));
        result.insert(
            "reason".into(),
            Value::String("no_declared_mixed_pool".into()),
        );
        return Value::Object(result);
    }
    let subset = subset_panel(panel, &stocks);
    let mut results = Map::new();
    for horizon in horizons {
        results.insert(horizon.to_string(), evaluate_horizon(&subset, *horizon));
    }
    let verdict = decide_verdict(&results);
    let mut result = Map::new();
    result.insert("status".into(), Value::String("evaluated".into()));
    result.insert("label".into(), Value::String("stocks_only".into()));
    result.insert(
        "excluded".into(),
        Value::Array(others.iter().cloned().map(Value::String).collect()),
    );
    result.insert("symbol_count".into(), Value::from(stocks.len()));
    result.insert("results".into(), Value::Object(results));
    result.insert("verdict".into(), Value::String(verdict.into()));
    result.insert(
        "note".into(),
        Value::String(
            "disclosure only; the registered verdict is computed on the full pool".into(),
        ),
    );
    Value::Object(result)
}

/// Build the registered A/B report for one panel.
pub fn build_report(panel: &Panel) -> Value {
    let mut horizons: Vec<usize> = PRIMARY_HORIZONS
        .iter()
        .map(|value| *value as usize)
        .collect();
    horizons.extend(REPORT_ONLY_HORIZONS.iter().map(|value| *value as usize));
    let mut results = Map::new();
    for horizon in &horizons {
        results.insert(horizon.to_string(), evaluate_horizon(panel, *horizon));
    }
    let verdict = decide_verdict(&results);
    let mut report = Map::new();
    report.insert("preregistration".into(), preregistration());
    let mut pool = Map::new();
    let mut symbols: Vec<String> = panel
        .closes
        .iter()
        .map(|(symbol, _)| symbol.clone())
        .collect();
    symbols.sort();
    pool.insert(
        "symbols".into(),
        Value::Array(symbols.iter().cloned().map(Value::String).collect()),
    );
    pool.insert("symbol_count".into(), Value::from(panel.closes.len()));
    pool.insert("sessions".into(), Value::from(panel.dates.len()));
    pool.insert(
        "first_session".into(),
        panel
            .dates
            .first()
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    pool.insert(
        "last_session".into(),
        panel
            .dates
            .last()
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    pool.insert("benchmark".into(), Value::String("SPY".into()));
    let mut dropped = Map::new();
    for (symbol, reason) in &panel.dropped {
        dropped.insert(symbol.clone(), Value::String(reason.clone()));
    }
    pool.insert("dropped".into(), Value::Object(dropped));
    pool.insert(
        "manifest".into(),
        panel
            .pool_manifest
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    report.insert("pool".into(), Value::Object(pool));
    report.insert("results".into(), Value::Object(results));
    report.insert("verdict".into(), Value::String(verdict.into()));
    report.insert(
        "incremental_value_claim".into(),
        Value::String(verdict_claim(verdict).into()),
    );
    let robustness = robustness_slice(panel, &horizons);
    report.insert(
        "durability".into(),
        Value::String(assess_durability(verdict, &robustness).into()),
    );
    report.insert("robustness".into(), robustness);
    Value::Object(report)
}

fn fmt(value: Option<f64>) -> String {
    match value {
        None => "n/a".to_string(),
        Some(value) => format!("{value:+.3}"),
    }
}

fn fmt_ci(interval: Option<&Value>) -> String {
    let Some(items) = interval.and_then(Value::as_array) else {
        return "n/a".to_string();
    };
    if items.len() != 2 {
        return "n/a".to_string();
    }
    format!("[{}, {}]", fmt(items[0].as_f64()), fmt(items[1].as_f64()))
}

/// Render the registered A/B markdown report.
pub fn render_markdown(report: &Value) -> String {
    let pool = &report["pool"];
    let mut lines: Vec<String> = vec![
        "# 20 日动量 A/B 对照（预登记协议 momentum-ab-v1）".into(),
        String::new(),
        format!(
            "- 池：{} 只标的，各自 {} 个 session（{} → {}），基准 {}",
            pool["symbol_count"],
            pool["sessions"],
            pool["first_session"].as_str().unwrap_or("None"),
            pool["last_session"].as_str().unwrap_or("None"),
            pool["benchmark"].as_str().unwrap_or("")
        ),
        format!(
            "- 判定：**{}** → `incremental_value_claim = {}`",
            report["verdict"].as_str().unwrap_or(""),
            report["incremental_value_claim"].as_str().unwrap_or("")
        ),
        format!(
            "- 结论强度：`{}`{}",
            report["durability"].as_str().unwrap_or(""),
            if report["durability"].as_str() == Some("pool_dependent") {
                "（同一协议在剔除 ETF 的股票子集上给出不同判定，说明结论依赖池子构成）"
            } else {
                ""
            }
        ),
        String::new(),
        "## 逐期限结果".into(),
        String::new(),
        "| 期限 | 可用 session | 独立块 | IC(A 裸 ROC20) | IC(B 综合分) | ΔIC | Δ 的 95% 区间 |"
            .into(),
        "| --- | --- | --- | --- | --- | --- | --- |".into(),
    ];
    let results = report["results"].as_object().cloned().unwrap_or_default();
    let mut keys: Vec<String> = results.keys().cloned().collect();
    keys.sort_by_key(|key| key.parse::<i64>().unwrap_or(0));
    for key in &keys {
        let record = &results[key];
        if record.get("status").and_then(Value::as_str) != Some("evaluated") {
            lines.push(format!("| {key}D | 0 | 0 | n/a | n/a | n/a | n/a |"));
            continue;
        }
        lines.push(format!(
            "| {key}D | {} | {} | {} {} | {} {} | {} | {} |",
            record["usable_sessions"],
            record["independent_blocks"],
            fmt(record["mean_ic_a"].as_f64()),
            fmt_ci(record.get("ci_a")),
            fmt(record["mean_ic_b"].as_f64()),
            fmt_ci(record.get("ci_b")),
            fmt(record["delta_ic"].as_f64()),
            fmt_ci(record.get("ci_delta"))
        ));
    }
    lines.extend([
        String::new(),
        "## 单因子消融（去掉该因子后重新归一化）".into(),
        String::new(),
        "| 期限 | 去掉风险调整 | 去掉回归趋势 | 去掉相对强度 |".into(),
        "| --- | --- | --- | --- |".into(),
    ]);
    for key in &keys {
        let record = &results[key];
        let ablation = record.get("ablation").cloned().unwrap_or(Value::Null);
        let mut cells: Vec<String> = Vec::new();
        for name in ["risk_adjusted", "regression_trend", "relative_strength"] {
            let entry = ablation.get(name).cloned().unwrap_or(Value::Null);
            if entry.get("status").and_then(Value::as_str) != Some("evaluated") {
                cells.push("n/a".into());
            } else {
                cells.push(format!(
                    "{} (Δ {})",
                    fmt(entry["mean_ic_without"].as_f64()),
                    fmt(entry["delta_vs_full"].as_f64())
                ));
            }
        }
        lines.push(format!("| {key}D | {} |", cells.join(" | ")));
    }
    let robustness = report.get("robustness").cloned().unwrap_or(Value::Null);
    lines.extend([
        String::new(),
        "## 稳健性披露（不参与判定）".into(),
        String::new(),
    ]);
    if robustness.get("status").and_then(Value::as_str) != Some("evaluated") {
        lines.push(format!(
            "- 不适用：{}",
            robustness
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
        ));
    } else {
        let excluded = robustness
            .get("excluded")
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        lines.push(format!(
            "- 仅 {} 只股票（剔除 {} 只 ETF）按同一协议复算 → **{}**",
            robustness["symbol_count"],
            excluded,
            robustness["verdict"].as_str().unwrap_or("")
        ));
        lines.extend([
            String::new(),
            "| 期限 | ΔIC | Δ 的 95% 区间 |".into(),
            "| --- | --- | --- |".into(),
        ]);
        let robust_results = robustness["results"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut robust_keys: Vec<String> = robust_results.keys().cloned().collect();
        robust_keys.sort_by_key(|key| key.parse::<i64>().unwrap_or(0));
        for key in &robust_keys {
            let record = &robust_results[key];
            if record.get("status").and_then(Value::as_str) != Some("evaluated") {
                lines.push(format!("| {key}D | n/a | n/a |"));
                continue;
            }
            lines.push(format!(
                "| {key}D | {} | {} |",
                fmt(record["delta_ic"].as_f64()),
                fmt_ci(record.get("ci_delta"))
            ));
        }
        lines.push("- 若该子集在某个主判定期限上的区间包含零，说明主判定依赖于池子构成。".into());
    }
    let prereg = preregistration();
    lines.extend([
        String::new(),
        "## 预登记判定规则（结果出来前已固定）".into(),
        String::new(),
        format!(
            "- 指标：`{}`，按 session 做横截面排序后取均值",
            prereg["metric"].as_str().unwrap_or("")
        ),
        format!(
            "- 主判定期限：{}；仅报告：{}",
            sd_core::contracts::python_str(&prereg["primary_horizons"]),
            sd_core::contracts::python_str(&prereg["report_only_horizons"])
        ),
        format!(
            "- 自助法：{}，块长 = 期限 + 1，{} 次，{}%，seed {}",
            prereg["bootstrap"].as_str().unwrap_or(""),
            prereg["bootstrap_iterations"],
            (prereg["bootstrap_confidence"].as_f64().unwrap_or(0.0) * 100.0) as i64,
            prereg["bootstrap_seed"]
        ),
        format!("- 判定：{}", prereg["verdict_rule"].as_str().unwrap_or("")),
        format!("- 权重：{}", prereg["weight_claim"].as_str().unwrap_or("")),
        format!(
            "- 说明：{}",
            prereg["report_only_note"].as_str().unwrap_or("")
        ),
        String::new(),
        "## 解释边界".into(),
        String::new(),
        "- 本协议只评估排序信息（IC），不评估校准，也不评估可交易性；没有计入交易成本、换手或回撤。".into(),
        "- 结论是关于**这个标的池**在这个 52 周窗口内的，不是关于任何单只标的的。".into(),
        "- 无论结果如何，`prob_up`、路径权重与聚合顺序都不因本协议改变。".into(),
        String::new(),
    ]);
    lines.join("\n")
}
