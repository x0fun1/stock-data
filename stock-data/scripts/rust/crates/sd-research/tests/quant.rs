//! Differential parity for the A-path prediction (`researchers/quant.py`).
//!
//! Every field of `predict_at_origin` is compared. Floats derived from the volatility
//! prior (`p_vol`, `sigma_*`, `z_score`, and the shrunk probability) depend on the
//! platform libm `erf`, so they are compared with a 1e-15 relative tolerance while all
//! structural fields, label boundaries, matching origins and reason codes are exact.

use std::path::Path;

use sd_research::quant::predict_at_origin;
use serde_json::Value;

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/quant-origin/predictions.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_str().expect("string").to_string())
        .collect()
}

fn floats(value: &Value) -> Vec<f64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_f64().expect("number"))
        .collect()
}

const TOLERANT_KEYS: [&str; 6] = [
    "p_vol",
    "sigma_daily",
    "sigma_horizon",
    "z_score",
    "mu_per_session",
    "probability",
];

fn assert_json_close(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(left), Value::Number(right)) => {
            let left_value = left.as_f64().expect("number");
            let right_value = right.as_f64().expect("number");
            let tolerant = TOLERANT_KEYS.iter().any(|key| path.ends_with(key));
            if tolerant {
                let tolerance = 1e-15 * right_value.abs().max(1.0);
                assert!(
                    (left_value - right_value).abs() <= tolerance,
                    "{path}: {left_value} vs {right_value} (tolerance {tolerance})"
                );
            } else {
                assert_eq!(left_value, right_value, "{path}: float differs");
            }
        }
        (Value::Array(left), Value::Array(right)) => {
            assert_eq!(left.len(), right.len(), "{path}: array length differs");
            for (index, (actual_item, expected_item)) in left.iter().zip(right.iter()).enumerate() {
                assert_json_close(actual_item, expected_item, &format!("{path}[{index}]"));
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            assert_eq!(left.len(), right.len(), "{path}: object size differs");
            for (key, expected_item) in right.iter() {
                let actual_item = left
                    .get(key)
                    .unwrap_or_else(|| panic!("{path}: missing key {key}"));
                assert_json_close(actual_item, expected_item, &format!("{path}/{key}"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}: value differs"),
    }
}

fn predict(
    dates: &[String],
    closes: &[f64],
    decision: usize,
    params: &Value,
) -> Result<Value, String> {
    let window_start = params["window_start_exclusive"].as_str();
    let expected = params["expected_window_sessions"].as_i64();
    let verified = params["coverage_verified"].as_bool().unwrap_or(false);
    predict_at_origin(
        dates,
        closes,
        decision,
        5,
        window_start,
        expected,
        verified,
        6,
    )
}

#[test]
fn origin_predictions_match_python() {
    let document = read();
    let dates = strings(&document["dates"]);
    let closes = floats(&document["closes"]);
    let cases = document["cases"].as_array().expect("cases");
    assert!(!cases.is_empty());
    for case in cases {
        let decision = case["decision"].as_u64().expect("decision") as usize;
        let name = case["case"].as_str().expect("case");
        let params = &case["params"];
        match predict(&dates, &closes, decision, params) {
            Ok(actual) => {
                let expected = case
                    .get("row")
                    .unwrap_or_else(|| panic!("{name}: expected row missing"));
                assert_json_close(&actual, expected, name);
            }
            Err(error) => {
                assert_eq!(
                    Some(error.as_str()),
                    case.get("error").and_then(Value::as_str),
                    "{name}: unexpected error/absence"
                );
            }
        }
    }
}

#[test]
fn every_prediction_is_reproducible() {
    let document = read();
    let dates = strings(&document["dates"]);
    let closes = floats(&document["closes"]);
    let first = predict_at_origin(&dates, &closes, 120, 5, None, None, false, 6).expect("row");
    let second = predict_at_origin(&dates, &closes, 120, 5, None, None, false, 6).expect("row");
    assert_eq!(
        first, second,
        "the same prefix must reproduce the same prediction"
    );
}

#[test]
fn degenerate_series_abstains_instead_of_inventing_probability() {
    let document = read();
    let dates = strings(&document["dates"]);
    let flat_closes = vec![100.0f64; 60];
    let actual = predict_at_origin(&dates[..60], &flat_closes, 55, 5, None, None, false, 6)
        .expect("flat row");
    assert_eq!(actual, document["flat"], "flat series row differs");
    assert_eq!(actual["reason_code"], "VOLATILITY_UNAVAILABLE");
    assert!(actual["probability"].is_null());
    let all_closes = floats(&document["closes"]);
    let short = predict_at_origin(&dates[..10], &all_closes[..10], 9, 5, None, None, false, 6)
        .expect("short row");
    assert_eq!(short, document["short"], "short-prefix row differs");
}
