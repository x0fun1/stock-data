//! Deterministic 20-session composite momentum feature (`momentum_20d_v1`).
//!
//! Port of `quant_research/momentum.py`. The composite score is a *feature*, never a
//! probability: three factors with fixed nominal weights (risk-adjusted 0.50, regression
//! trend 0.30, relative strength 0.20), each compressed with `tanh(x / 1)`, no runtime
//! tuning and no silent weight redistribution. A missing or degenerate factor yields
//! `None` plus an explicit status instead of a renormalised number.
//!
//! Absolute 20-session ROC direction is reported separately and is not forced to agree
//! with the composite score.

use serde_json::{Map, Value};

use sd_core::float_sum::math_fsum;

/// Frozen model version.
pub const MOMENTUM_MODEL_VERSION: &str = "momentum_20d_v1";
/// Below this daily volatility the risk-adjusted factor is degenerate.
pub const MIN_DAILY_VOLATILITY: f64 = 1e-6;
/// Below this tracking error the relative-strength factor is degenerate.
pub const MIN_TRACKING_ERROR: f64 = 1e-6;
/// Benchmark overlap below this fraction of own volatility is nearly identical.
pub const BENCHMARK_OVERLAP_RATIO: f64 = 0.10;
/// Absolute ROC at or below this tolerance is reported as flat.
pub const FLAT_ROC_TOLERANCE: f64 = 1e-12;

fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64().filter(|value| value.is_finite()),
        _ => None,
    }
}

fn int_field(config: &Value, group: &str, key: &str, default: i64) -> i64 {
    config
        .get(group)
        .and_then(|group| group.get(key))
        .and_then(Value::as_i64)
        .unwrap_or(default)
}

fn float_field(config: &Value, group: &str, key: &str, default: f64) -> f64 {
    number(config.get(group).and_then(|group| group.get(key))).unwrap_or(default)
}

fn string_field<'a>(config: &'a Value, group: &str, key: &str) -> Option<&'a str> {
    config
        .get(group)
        .and_then(|group| group.get(key))
        .and_then(Value::as_str)
}

/// Frozen default configuration (the only configuration the policy permits at runtime).
pub fn default_config() -> Value {
    let mut windows = Map::new();
    windows.insert("momentum_days".into(), Value::from(20));
    windows.insert("volatility_days".into(), Value::from(60));
    windows.insert("regression_price_points".into(), Value::from(21));
    windows.insert("tracking_error_days".into(), Value::from(60));
    windows.insert("annual_trading_days".into(), Value::from(252));
    let mut weights = Map::new();
    weights.insert("risk_adjusted".into(), Value::from(0.50));
    weights.insert("regression_trend".into(), Value::from(0.30));
    weights.insert("relative_strength".into(), Value::from(0.20));
    let mut normalization = Map::new();
    normalization.insert("method".into(), Value::String("tanh".into()));
    normalization.insert("scale_A".into(), Value::from(1.0));
    normalization.insert("scale_T".into(), Value::from(1.0));
    normalization.insert("scale_R".into(), Value::from(1.0));
    normalization.insert(
        "calibration_status".into(),
        Value::String("uncalibrated".into()),
    );
    let mut validation = Map::new();
    validation.insert("minimum_daily_returns".into(), Value::from(60));
    validation.insert("std_ddof".into(), Value::from(1));
    validation.insert("reject_nonfinite".into(), Value::Bool(true));
    validation.insert("reject_invalid_prices".into(), Value::Bool(true));
    validation.insert("reject_near_zero_denominators".into(), Value::Bool(true));
    validation.insert("require_all_components".into(), Value::Bool(true));
    validation.insert(
        "allow_silent_weight_redistribution".into(),
        Value::Bool(false),
    );
    let mut output = Map::new();
    output.insert("score_min".into(), Value::from(-100.0));
    output.insert("score_max".into(), Value::from(100.0));
    output.insert("display_decimals".into(), Value::from(2));
    output.insert("include_component_details".into(), Value::Bool(true));
    output.insert("include_roc_direction".into(), Value::Bool(true));
    let mut config = Map::new();
    config.insert(
        "version".into(),
        Value::String(MOMENTUM_MODEL_VERSION.into()),
    );
    config.insert("windows".into(), Value::Object(windows));
    config.insert("weights".into(), Value::Object(weights));
    config.insert("normalization".into(), Value::Object(normalization));
    config.insert("validation".into(), Value::Object(validation));
    config.insert("output".into(), Value::Object(output));
    Value::Object(config)
}

fn stdev(values: &[f64], ddof: i64) -> Option<f64> {
    let count = values.len() as i64;
    if count - ddof <= 0 {
        return None;
    }
    let average = math_fsum(values.iter().copied()) / count as f64;
    let mut variance = math_fsum(
        values
            .iter()
            .map(|value| (value - average) * (value - average)),
    ) / (count - ddof) as f64;
    variance = variance.max(0.0);
    Some(variance.sqrt())
}

/// Absolute `window`-session return, identical to the pre-existing baseline.
pub fn roc(values: &[f64], index: usize, window: usize) -> Option<f64> {
    if index < window || index >= values.len() || values[index - window] <= 0.0 {
        return None;
    }
    Some(values[index] / values[index - window] - 1.0)
}

/// OLS of `series[k]` on `k = 0..n-1`; returns `(slope, r_squared, r2_defined)`.
fn ols_slope_r2(series: &[f64]) -> (Option<f64>, Option<f64>, bool) {
    let count = series.len();
    if count < 3 {
        return (None, None, false);
    }
    let mean_k = (count - 1) as f64 / 2.0;
    let mean_y = math_fsum(series.iter().copied()) / count as f64;
    let sxx = math_fsum((0..count).map(|k| (k as f64 - mean_k) * (k as f64 - mean_k)));
    if sxx <= 0.0 {
        return (None, None, false);
    }
    let sxy = math_fsum(
        series
            .iter()
            .enumerate()
            .map(|(k, value)| (k as f64 - mean_k) * (value - mean_y)),
    );
    let syy = math_fsum(
        series
            .iter()
            .map(|value| (value - mean_y) * (value - mean_y)),
    );
    let slope = sxy / sxx;
    if syy <= 0.0 {
        return (Some(0.0), Some(0.0), false);
    }
    let r_squared = (slope * sxy / syy).clamp(0.0, 1.0);
    (Some(slope), Some(r_squared), true)
}

fn component(weight: f64) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("raw_value".into(), Value::Null);
    map.insert("normalized_value".into(), Value::Null);
    map.insert("weight".into(), Value::from(weight));
    map.insert("contribution".into(), Value::Null);
    map.insert("status".into(), Value::String("unavailable".into()));
    map.insert("reason".into(), Value::Null);
    map
}

fn fail(component: &mut Map<String, Value>, status: &str, reason: &str) {
    component.insert("status".into(), Value::String(status.into()));
    component.insert("reason".into(), Value::String(reason.into()));
}

fn normalize(raw: f64, scale: f64) -> f64 {
    (raw / scale).tanh()
}

/// Accept a date -> adjusted close mapping; reject anything unparsable.
fn benchmark_series(benchmark: Option<&Value>) -> Option<Map<String, Value>> {
    let benchmark = benchmark?.as_object()?;
    let mut series = Map::new();
    for (date, value) in benchmark {
        let numeric = number(Some(value)).filter(|value| *value > 0.0)?;
        let key = date.get(..10).unwrap_or(date).to_string();
        series.insert(key, Value::from(numeric));
    }
    if series.is_empty() {
        None
    } else {
        Some(series)
    }
}

fn evaluate(
    closes: &[f64],
    dates: &[String],
    benchmark: Option<&Map<String, Value>>,
    index: usize,
    config: &Value,
) -> Value {
    let momentum_days = int_field(config, "windows", "momentum_days", 20).max(0) as usize;
    let volatility_days = int_field(config, "windows", "volatility_days", 60).max(0) as usize;
    let regression_points =
        int_field(config, "windows", "regression_price_points", 21).max(0) as usize;
    let tracking_days = int_field(config, "windows", "tracking_error_days", 60).max(0) as usize;
    let ddof = int_field(config, "validation", "std_ddof", 1);
    let annual_days = int_field(config, "windows", "annual_trading_days", 252) as f64;

    let mut components = Map::new();
    components.insert(
        "risk_adjusted".into(),
        Value::Object(component(float_field(
            config,
            "weights",
            "risk_adjusted",
            0.5,
        ))),
    );
    components.insert(
        "regression_trend".into(),
        Value::Object(component(float_field(
            config,
            "weights",
            "regression_trend",
            0.3,
        ))),
    );
    components.insert(
        "relative_strength".into(),
        Value::Object(component(float_field(
            config,
            "weights",
            "relative_strength",
            0.2,
        ))),
    );
    if let Some(Value::Object(trend)) = components.get_mut("regression_trend") {
        trend.insert("beta".into(), Value::Null);
        trend.insert("r_squared".into(), Value::Null);
        trend.insert("r_squared_defined".into(), Value::Bool(false));
    }
    let mut warnings: Vec<Value> = Vec::new();
    let mut return_20d = Value::Null;
    let mut direction = Value::Null;
    let absolute_return = roc(closes, index, momentum_days);
    match absolute_return {
        None => warnings.push(Value::String("absolute_return_unavailable".into())),
        Some(value) => {
            return_20d = Value::from(value);
            direction = Value::String(
                if value.abs() <= FLAT_ROC_TOLERANCE {
                    "flat"
                } else if value > 0.0 {
                    "positive"
                } else {
                    "negative"
                }
                .into(),
            );
        }
    }
    let mut daily_volatility: Option<Value> = None;
    let mut annual_volatility: Option<Value> = None;

    let needed = volatility_days
        .max(momentum_days)
        .max(regression_points.saturating_sub(1));
    if index < needed {
        for name in ["risk_adjusted", "regression_trend", "relative_strength"] {
            if let Some(Value::Object(component)) = components.get_mut(name) {
                fail(component, "unavailable", "insufficient_history");
            }
        }
        return json_evaluate(
            return_20d,
            direction,
            components,
            warnings,
            0,
            daily_volatility,
            annual_volatility,
        );
    }
    let window_prices = &closes[index - volatility_days..=index];
    if window_prices
        .iter()
        .any(|value| !value.is_finite() || *value <= 0.0)
    {
        for name in ["risk_adjusted", "regression_trend", "relative_strength"] {
            if let Some(Value::Object(component)) = components.get_mut(name) {
                fail(component, "invalid", "invalid_price");
            }
        }
        return json_evaluate(
            return_20d,
            direction,
            components,
            warnings,
            0,
            daily_volatility,
            annual_volatility,
        );
    }
    let returns: Vec<f64> = (1..window_prices.len())
        .map(|k| (window_prices[k] / window_prices[k - 1]).ln())
        .collect();
    let sigma = stdev(&returns, ddof);
    daily_volatility = sigma.map(Value::from);
    annual_volatility = sigma.map(|value| Value::from(value * annual_days.sqrt()));

    let risk_adjusted_ok = sigma
        .map(|value| value > MIN_DAILY_VOLATILITY)
        .unwrap_or(false);
    if !risk_adjusted_ok {
        let reason = if sigma.is_some() {
            "degenerate_volatility"
        } else {
            "insufficient_history"
        };
        let status = if sigma.is_some() {
            "invalid"
        } else {
            "unavailable"
        };
        for name in ["risk_adjusted", "regression_trend"] {
            if let Some(Value::Object(component)) = components.get_mut(name) {
                fail(component, status, reason);
            }
        }
    } else {
        let sigma_value = sigma.unwrap_or_default();
        let log_20 = (closes[index] / closes[index - momentum_days]).ln();
        let raw_a = log_20 / (sigma_value * (momentum_days as f64).sqrt());
        let scale_a = float_field(config, "normalization", "scale_A", 1.0);
        if let Some(Value::Object(component)) = components.get_mut("risk_adjusted") {
            let normalized = normalize(raw_a, scale_a);
            let weight = amount(component, "weight");
            component.insert("raw_value".into(), Value::from(raw_a));
            component.insert("normalized_value".into(), Value::from(normalized));
            component.insert(
                "contribution".into(),
                Value::from(100.0 * weight * normalized),
            );
            component.insert("status".into(), Value::String("valid".into()));
        }
        let regression_prices = &closes[index - (regression_points - 1)..=index];
        if regression_prices
            .iter()
            .any(|value| !value.is_finite() || *value <= 0.0)
        {
            if let Some(Value::Object(component)) = components.get_mut("regression_trend") {
                fail(component, "invalid", "invalid_price");
            }
        } else {
            let logs: Vec<f64> = regression_prices.iter().map(|value| value.ln()).collect();
            let (slope, r_squared, r2_defined) = ols_slope_r2(&logs);
            if let Some(Value::Object(trend)) = components.get_mut("regression_trend") {
                trend.insert("beta".into(), slope.map(Value::from).unwrap_or(Value::Null));
                trend.insert(
                    "r_squared".into(),
                    r_squared.map(Value::from).unwrap_or(Value::Null),
                );
                trend.insert("r_squared_defined".into(), Value::Bool(r2_defined));
                match (slope, r_squared) {
                    (Some(slope), Some(r_squared)) => {
                        if !r2_defined {
                            warnings
                                .push(Value::String("flat_regression_window_r2_undefined".into()));
                        }
                        let raw_t = (momentum_days as f64 * slope
                            / (sigma_value * (momentum_days as f64).sqrt()))
                            * r_squared;
                        let scale_t = float_field(config, "normalization", "scale_T", 1.0);
                        let normalized = normalize(raw_t, scale_t);
                        let weight = amount(trend, "weight");
                        trend.insert("raw_value".into(), Value::from(raw_t));
                        trend.insert("normalized_value".into(), Value::from(normalized));
                        trend.insert(
                            "contribution".into(),
                            Value::from(100.0 * weight * normalized),
                        );
                        trend.insert("status".into(), Value::String("valid".into()));
                    }
                    _ => fail(trend, "invalid", "degenerate_regression"),
                }
            }
        }
    }

    let mut aligned = 0i64;
    let relative_ok = benchmark.is_some();
    if !relative_ok {
        if let Some(Value::Object(component)) = components.get_mut("relative_strength") {
            fail(component, "unavailable", "benchmark_unavailable");
        }
    } else {
        let benchmark = benchmark.expect("checked above");
        let start = index.saturating_sub(tracking_days);
        let span_dates = &dates[start..=index];
        let missing = span_dates
            .iter()
            .filter(|date| !benchmark.contains_key(*date))
            .count();
        aligned = span_dates.len() as i64 - missing as i64;
        if missing > 0 {
            if let Some(Value::Object(component)) = components.get_mut("relative_strength") {
                fail(component, "unavailable", "benchmark_alignment_incomplete");
            }
        } else {
            let stock_prices = &closes[start..=index];
            let benchmark_prices: Vec<f64> = span_dates
                .iter()
                .map(|date| number(benchmark.get(date)).unwrap_or(0.0))
                .collect();
            let excess: Vec<f64> = (1..span_dates.len())
                .map(|k| {
                    (stock_prices[k] / stock_prices[k - 1]).ln()
                        - (benchmark_prices[k] / benchmark_prices[k - 1]).ln()
                })
                .collect();
            let tracking_error = stdev(&excess, ddof);
            let tracking_ok = tracking_error
                .map(|value| value > MIN_TRACKING_ERROR)
                .unwrap_or(false);
            if !tracking_ok {
                if let Some(Value::Object(component)) = components.get_mut("relative_strength") {
                    fail(component, "invalid", "degenerate_tracking_error");
                }
            } else {
                let tracking_error = tracking_error.unwrap_or_default();
                let decision_date = &dates[index];
                let prior_date = &dates[index - momentum_days];
                let excess_20 = (closes[index] / closes[index - momentum_days]).ln()
                    - (number(benchmark.get(decision_date)).unwrap_or(0.0)
                        / number(benchmark.get(prior_date)).unwrap_or(0.0))
                    .ln();
                let raw_r = excess_20 / (tracking_error * (momentum_days as f64).sqrt());
                let scale_r = float_field(config, "normalization", "scale_R", 1.0);
                if let Some(Value::Object(component)) = components.get_mut("relative_strength") {
                    let normalized = normalize(raw_r, scale_r);
                    let weight = amount(component, "weight");
                    component.insert("raw_value".into(), Value::from(raw_r));
                    component.insert("normalized_value".into(), Value::from(normalized));
                    component.insert(
                        "contribution".into(),
                        Value::from(100.0 * weight * normalized),
                    );
                    component.insert("status".into(), Value::String("valid".into()));
                }
                if let Some(sigma) = sigma {
                    if tracking_error < BENCHMARK_OVERLAP_RATIO * sigma {
                        warnings.push(Value::String(
                            "benchmark_near_identical_low_economic_signal".into(),
                        ));
                    }
                }
            }
        }
    }
    json_evaluate(
        return_20d,
        direction,
        components,
        warnings,
        aligned,
        daily_volatility,
        annual_volatility,
    )
}

fn amount(component: &Map<String, Value>, key: &str) -> f64 {
    component.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn json_evaluate(
    return_20d: Value,
    direction: Value,
    components: Map<String, Value>,
    warnings: Vec<Value>,
    aligned: i64,
    daily_volatility: Option<Value>,
    annual_volatility: Option<Value>,
) -> Value {
    let mut result = Map::new();
    result.insert("return_20d".into(), return_20d);
    result.insert("momentum_direction".into(), direction);
    result.insert("components".into(), Value::Object(components));
    result.insert("warnings".into(), Value::Array(warnings));
    result.insert("benchmark_aligned_sessions".into(), Value::from(aligned));
    if let Some(value) = daily_volatility {
        result.insert("daily_volatility_60d".into(), value);
    }
    if let Some(value) = annual_volatility {
        result.insert("annual_volatility_60d".into(), value);
    }
    Value::Object(result)
}

fn assemble(evaluation: &Value, config: &Value) -> (Value, String, Vec<String>) {
    let components = evaluation.get("components").and_then(Value::as_object);
    let empty = Map::new();
    let components = components.unwrap_or(&empty);
    let mut reasons: Vec<String> = Vec::new();
    let mut statuses: Vec<String> = Vec::new();
    for (name, component) in components {
        let status = component
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unavailable")
            .to_string();
        if status != "valid" {
            let reason = component
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            reasons.push(format!("{name}:{reason}"));
        }
        statuses.push(status);
    }
    let mut score_status = if statuses.iter().any(|status| status == "invalid") {
        "invalid"
    } else if statuses.iter().any(|status| status == "unavailable") {
        "unavailable"
    } else {
        "valid"
    };
    let contributions: Vec<Option<f64>> = components
        .values()
        .map(|component| component.get("contribution").and_then(Value::as_f64))
        .collect();
    let mut score = Value::Null;
    if score_status == "valid" {
        if contributions.iter().any(Option::is_none) {
            // Defensive: require_all_components forbids partial scores.
            score_status = "invalid";
            reasons.push("incomplete_contributions".into());
        } else {
            let total = math_fsum(contributions.iter().map(|value| value.unwrap_or_default()));
            let low = float_field(config, "output", "score_min", -100.0);
            let high = float_field(config, "output", "score_max", 100.0);
            if !(low < total && total < high) {
                score = Value::from(total.min(f64::next_down(high)).max(f64::next_up(low)));
                reasons.push("score_clamped_to_bounds".into());
            } else {
                score = Value::from(total);
            }
        }
    }
    (score, score_status.to_string(), reasons)
}

/// Single-point factor evaluation for offline studies.
pub fn score_at(
    closes: &[f64],
    dates: &[String],
    benchmark: Option<&Value>,
    index: usize,
    config: Option<&Value>,
) -> Value {
    let default = default_config();
    let active = config.unwrap_or(&default);
    let series = benchmark_series(benchmark);
    let evaluation = evaluate(closes, dates, series.as_ref(), index, active);
    let (score, status, reasons) = assemble(&evaluation, active);
    let mut result = Map::new();
    result.insert(
        "as_of".into(),
        dates
            .get(index)
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    result.insert("score".into(), score);
    result.insert("status".into(), Value::String(status));
    result.insert(
        "status_reasons".into(),
        Value::Array(reasons.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "momentum_direction".into(),
        evaluation
            .get("momentum_direction")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "return_20d".into(),
        evaluation.get("return_20d").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "components".into(),
        evaluation.get("components").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "warnings".into(),
        evaluation.get("warnings").cloned().unwrap_or(Value::Null),
    );
    Value::Object(result)
}

/// Compute the frozen composite momentum block for the final session.
#[allow(clippy::too_many_arguments)]
pub fn compute(
    closes: &[f64],
    dates: &[String],
    benchmark: Option<&Value>,
    symbol: &str,
    benchmark_symbol: Option<&str>,
    benchmark_selection: &str,
    price_field: Option<&str>,
    config: Option<&Value>,
) -> Result<Value, String> {
    let default = default_config();
    let active = config.unwrap_or(&default);
    let index = closes.len().saturating_sub(1);
    let series = benchmark_series(benchmark);
    let evaluation = evaluate(closes, dates, series.as_ref(), index, active);
    let (score, score_status, reasons) = assemble(&evaluation, active);
    let weights = [
        float_field(active, "weights", "risk_adjusted", 0.5),
        float_field(active, "weights", "regression_trend", 0.3),
        float_field(active, "weights", "relative_strength", 0.2),
    ];
    if (math_fsum(weights.iter().copied()) - 1.0).abs() > 1e-12 {
        return Err("momentum weights must sum to one".into());
    }
    let mut benchmark_block = Map::new();
    benchmark_block.insert(
        "symbol".into(),
        benchmark_symbol
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    benchmark_block.insert(
        "selection".into(),
        Value::String(benchmark_selection.into()),
    );
    benchmark_block.insert(
        "alignment".into(),
        Value::String("exact_date_intersection_no_forward_fill".into()),
    );
    benchmark_block.insert(
        "aligned_sessions".into(),
        evaluation
            .get("benchmark_aligned_sessions")
            .cloned()
            .unwrap_or(Value::Null),
    );
    let mut result = Map::new();
    result.insert(
        "model_version".into(),
        active
            .get("version")
            .cloned()
            .unwrap_or(Value::String(MOMENTUM_MODEL_VERSION.into())),
    );
    result.insert(
        "as_of".into(),
        dates
            .get(index)
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    result.insert("symbol".into(), Value::String(symbol.into()));
    result.insert("benchmark".into(), Value::Object(benchmark_block));
    result.insert(
        "price_field".into(),
        price_field
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "momentum_direction".into(),
        evaluation
            .get("momentum_direction")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "return_20d".into(),
        evaluation.get("return_20d").cloned().unwrap_or(Value::Null),
    );
    result.insert("momentum_score_20d".into(), score);
    result.insert("score_status".into(), Value::String(score_status));
    result.insert(
        "status_reasons".into(),
        Value::Array(reasons.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "calibration_status".into(),
        string_field(active, "normalization", "calibration_status")
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "interpretation".into(),
        Value::String("feature_not_probability".into()),
    );
    result.insert(
        "weights".into(),
        active.get("weights").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "windows".into(),
        active.get("windows").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "normalization".into(),
        active.get("normalization").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "daily_volatility_60d".into(),
        evaluation
            .get("daily_volatility_60d")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "annual_volatility_60d".into(),
        evaluation
            .get("annual_volatility_60d")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "components".into(),
        evaluation.get("components").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "warnings".into(),
        evaluation.get("warnings").cloned().unwrap_or(Value::Null),
    );
    Ok(Value::Object(result))
}

fn average_ranks(values: &[f64]) -> Vec<f64> {
    let mut order: Vec<usize> = (0..values.len()).collect();
    order.sort_by(|left, right| {
        values[*left]
            .partial_cmp(&values[*right])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ranks = vec![0.0f64; values.len()];
    let mut position = 0usize;
    while position < order.len() {
        let mut end = position;
        while end + 1 < order.len() && values[order[end + 1]] == values[order[position]] {
            end += 1;
        }
        let average = (position + end) as f64 / 2.0 + 1.0;
        for cursor in position..=end {
            ranks[order[cursor]] = average;
        }
        position = end + 1;
    }
    ranks
}

fn spearman(xs: &[f64], ys: &[f64]) -> Option<f64> {
    if xs.len() < 3 || xs.len() != ys.len() {
        return None;
    }
    let rx = average_ranks(xs);
    let ry = average_ranks(ys);
    let mean_x = math_fsum(rx.iter().copied()) / rx.len() as f64;
    let mean_y = math_fsum(ry.iter().copied()) / ry.len() as f64;
    let sxy = math_fsum(
        rx.iter()
            .zip(ry.iter())
            .map(|(a, b)| (a - mean_x) * (b - mean_y)),
    );
    let sxx = math_fsum(rx.iter().map(|value| (value - mean_x) * (value - mean_x)));
    let syy = math_fsum(ry.iter().map(|value| (value - mean_y) * (value - mean_y)));
    if sxx <= 0.0 || syy <= 0.0 {
        return None;
    }
    Some(sxy / (sxx * syy).sqrt())
}

/// A rank-IC selector: (score, baseline, label) -> selected value.
type IcSelector = fn(&(Option<f64>, f64, f64)) -> Option<f64>;

/// Descriptive rank IC of the composite score and of plain ROC20.
pub fn rank_ic_diagnostic(
    closes: &[f64],
    dates: &[String],
    benchmark: Option<&Value>,
    horizon: usize,
    config: Option<&Value>,
    minimum_samples: usize,
) -> Value {
    let default = default_config();
    let active = config.unwrap_or(&default);
    let series = benchmark_series(benchmark);
    let window = int_field(active, "windows", "momentum_days", 20).max(0) as usize;
    let mut rows: Vec<(Option<f64>, f64, f64)> = Vec::new();
    if closes.len() > horizon {
        for index in window..closes.len() - horizon {
            if closes[index + horizon] <= 0.0 || closes[index] <= 0.0 {
                continue;
            }
            let label = closes[index + horizon] / closes[index] - 1.0;
            let Some(baseline) = roc(closes, index, window) else {
                continue;
            };
            let evaluation = evaluate(closes, dates, series.as_ref(), index, active);
            let components = evaluation.get("components").and_then(Value::as_object);
            let mut score = None;
            if let Some(components) = components {
                if components.values().all(|component| {
                    component.get("status").and_then(Value::as_str) == Some("valid")
                }) {
                    score = Some(math_fsum(components.values().filter_map(|component| {
                        component.get("contribution").and_then(Value::as_f64)
                    })));
                }
            }
            rows.push((score, baseline, label));
        }
    }
    let pair = |selector: IcSelector| -> (Value, i64) {
        let usable: Vec<(f64, f64)> = rows
            .iter()
            .filter_map(|row| selector(row).map(|value| (value, row.2)))
            .collect();
        if usable.len() < minimum_samples {
            return (Value::Null, usable.len() as i64);
        }
        let xs: Vec<f64> = usable.iter().map(|(value, _)| *value).collect();
        let ys: Vec<f64> = usable.iter().map(|(_, label)| *label).collect();
        (
            spearman(&xs, &ys).map(Value::from).unwrap_or(Value::Null),
            usable.len() as i64,
        )
    };
    let midpoint = rows.len() / 2;
    let split = |selector: IcSelector, low: usize, high: usize| -> Value {
        let usable: Vec<(f64, f64)> = rows[low..high]
            .iter()
            .filter_map(|row| selector(row).map(|value| (value, row.2)))
            .collect();
        if usable.len() < 3.max(minimum_samples / 4) {
            return Value::Null;
        }
        let xs: Vec<f64> = usable.iter().map(|(value, _)| *value).collect();
        let ys: Vec<f64> = usable.iter().map(|(_, label)| *label).collect();
        spearman(&xs, &ys).map(Value::from).unwrap_or(Value::Null)
    };
    let (score_ic, score_n) = pair(|row| row.0);
    let (roc_ic, roc_n) = pair(|row| Some(row.1));
    let mut composite = Map::new();
    composite.insert("rank_ic".into(), score_ic);
    composite.insert("samples".into(), Value::from(score_n));
    composite.insert("rank_ic_first_half".into(), split(|row| row.0, 0, midpoint));
    composite.insert(
        "rank_ic_second_half".into(),
        split(|row| row.0, midpoint, rows.len()),
    );
    let mut baseline_block = Map::new();
    baseline_block.insert("rank_ic".into(), roc_ic);
    baseline_block.insert("samples".into(), Value::from(roc_n));
    baseline_block.insert(
        "rank_ic_first_half".into(),
        split(|row| Some(row.1), 0, midpoint),
    );
    baseline_block.insert(
        "rank_ic_second_half".into(),
        split(|row| Some(row.1), midpoint, rows.len()),
    );
    let mut result = Map::new();
    result.insert("horizon_sessions".into(), Value::from(horizon));
    result.insert(
        "label".into(),
        Value::String(format!("forward_return_{horizon}d")),
    );
    result.insert(
        "scope".into(),
        Value::String("in_sample_descriptive_no_hyperparameter_fitting".into()),
    );
    result.insert(
        "incremental_value_claim".into(),
        Value::String("not_established".into()),
    );
    result.insert("samples".into(), Value::from(rows.len()));
    result.insert("composite_score".into(), Value::Object(composite));
    result.insert("baseline_roc20".into(), Value::Object(baseline_block));
    result.insert(
        "warnings".into(),
        Value::Array(vec![
            Value::String("in_sample_descriptive_only".into()),
            Value::String("no_out_of_sample_evidence_of_superiority".into()),
        ]),
    );
    Value::Object(result)
}
