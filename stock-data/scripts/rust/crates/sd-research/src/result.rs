//! Shared researcher-result envelope (`contracts.researcher_result`).
//!
//! The legacy contract validates the identity/status/role and the probability shape, then
//! returns a fixed key order. Every researcher path (`quant`, `factor`, `ml`,
//! `factor_backtest`) uses this one function so the envelope cannot drift between paths.

use serde_json::{Map, Value};

/// Permitted result roles.
pub const RESULT_ROLES: [&str; 4] = ["forecast", "diagnostic", "shadow", "production"];

/// Build a validated researcher result envelope.
#[allow(clippy::too_many_arguments)] // mirrors the legacy keyword-call positionally
pub fn researcher_result(
    researcher_id: &str,
    ticker: &str,
    horizon: &str,
    snapshot_id: &str,
    status: &str,
    result_role: &str,
    prob_up: Option<f64>,
    expected_return: Option<f64>,
    evidence: Value,
    validation: Value,
    data_used: Vec<Value>,
    warnings: Vec<Value>,
    probability_source: Option<&str>,
    raw_probability: Option<f64>,
) -> Result<Value, String> {
    if !RESULT_ROLES.contains(&result_role) {
        return Err(format!("unknown result_role: {result_role}"));
    }
    if let Some(probability) = prob_up {
        if !(0.0..=1.0).contains(&probability) {
            return Err("prob_up must be between 0 and 1".into());
        }
    }
    if prob_up.is_none() && probability_source.is_some() {
        return Err("probability_source requires a numeric probability".into());
    }
    let direction = match prob_up {
        Some(value) if value > 0.5 => "bullish",
        Some(value) if value < 0.5 => "bearish",
        Some(_) => "neutral",
        None => "unavailable",
    };
    let mut map = Map::new();
    map.insert("researcher_id".into(), Value::String(researcher_id.into()));
    map.insert("result_role".into(), Value::String(result_role.into()));
    map.insert("ticker".into(), Value::String(ticker.into()));
    map.insert("horizon".into(), Value::String(horizon.into()));
    map.insert("snapshot_id".into(), Value::String(snapshot_id.into()));
    map.insert("direction".into(), Value::String(direction.into()));
    map.insert(
        "prob_up".into(),
        prob_up.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "prob_down".into(),
        prob_up
            .map(|value| Value::from(1.0 - value))
            .unwrap_or(Value::Null),
    );
    map.insert(
        "raw_probability".into(),
        raw_probability.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("calibrated_probability".into(), Value::Null);
    map.insert(
        "reportable_probability".into(),
        prob_up.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert(
        "expected_return".into(),
        expected_return.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("confidence".into(), Value::Null);
    map.insert(
        "probability_source".into(),
        probability_source
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    map.insert("evidence".into(), evidence);
    map.insert("validation".into(), validation);
    map.insert("data_used".into(), Value::Array(data_used));
    map.insert("warnings".into(), Value::Array(warnings));
    map.insert("status".into(), Value::String(status.into()));
    Ok(Value::Object(map))
}
