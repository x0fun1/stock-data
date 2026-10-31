//! Differential parity for the composite momentum feature (`momentum.py`).
//!
//! The score is a disclosed feature, never a probability, but it is quoted in reports
//! and used by the descriptive D path, so factor values, statuses and the all-or-nothing
//! assembly rule are compared exactly. `math.fsum` is exact rounding, so numeric leaves
//! are compared bit-for-bit except where libm functions (`ln`, `tanh`, `sqrt`) dominate;
//! those use a 1e-15 relative tolerance.

use std::path::Path;

use sd_research::momentum::{compute, rank_ic_diagnostic, score_at};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/momentum/momentum.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

/// One recorded compute variant: (case, benchmark, length, symbol, price field).
type ComputeCase<'a> = (
    &'a str,
    Option<&'a Value>,
    usize,
    Option<&'a str>,
    Option<&'a str>,
);

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

fn assert_json_close(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(left), Value::Number(right)) => {
            let left_value = left.as_f64().expect("number");
            let right_value = right.as_f64().expect("number");
            let tolerance = 1e-15 * right_value.abs().max(1.0);
            assert!(
                (left_value - right_value).abs() <= tolerance,
                "{path}: {left_value} vs {right_value} (tolerance {tolerance})"
            );
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

#[test]
fn compute_matches_python() {
    let document = read();
    let dates = strings(&document["dates"]);
    let closes = floats(&document["closes"]);
    let benchmark = document["benchmark"].clone();
    let partial = document["partial_benchmark"].clone();
    // (case, benchmark, length, benchmark symbol, price field)
    let cases: Vec<ComputeCase> = vec![
        (
            "aligned",
            Some(&benchmark),
            closes.len(),
            Some("SPY"),
            Some("close"),
        ),
        ("no_benchmark", None, closes.len(), None, None),
        (
            "partial_benchmark",
            Some(&partial),
            closes.len(),
            Some("SPY"),
            None,
        ),
        ("short", None, 40, None, None),
    ];
    for (name, bench, length, symbol, price_field) in cases {
        let actual = compute(
            &closes[..length],
            &dates[..length],
            bench,
            "SYN",
            symbol,
            "declared",
            price_field,
            None,
        )
        .expect("compute");
        assert_json_close(&actual, &document["compute"][name], name);
    }
    // Constant prices: ROC is exactly flat, so the volatility factors are degenerate.
    let flat_closes = vec![100.0; 120];
    let actual = compute(
        &flat_closes,
        &dates[..120],
        None,
        "SYN",
        None,
        "declared",
        None,
        None,
    )
    .expect("compute");
    assert_json_close(&actual, &document["compute"]["flat"], "flat");
    // A benchmark identical to the stock must be flagged as low economic signal.
    let self_benchmark: Value = Value::Object(
        dates
            .iter()
            .zip(closes.iter())
            .map(|(date, close)| (date.clone(), Value::from(*close)))
            .collect(),
    );
    let actual = compute(
        &closes,
        &dates,
        Some(&self_benchmark),
        "SYN",
        Some("SELF"),
        "declared",
        None,
        None,
    )
    .expect("compute");
    assert_json_close(
        &actual,
        &document["compute"]["near_identical"],
        "near_identical",
    );
    // An identical benchmark has zero tracking error: the factor is invalid, not silent.
    assert_eq!(
        actual["components"]["relative_strength"]["reason"],
        "degenerate_tracking_error"
    );
}

#[test]
fn score_at_matches_python() {
    let document = read();
    let dates = strings(&document["dates"]);
    let closes = floats(&document["closes"]);
    let benchmark = document["benchmark"].clone();
    let expected = document["score_at"].as_array().expect("score_at");
    for (index, item) in [0usize, 30, 100, 199].iter().enumerate() {
        let actual = score_at(&closes, &dates, Some(&benchmark), *item, None);
        assert_json_close(&actual, &expected[index], &format!("score_at[{item}]"));
    }
}

#[test]
fn rank_ic_matches_python() {
    let document = read();
    let dates = strings(&document["dates"]);
    let closes = floats(&document["closes"]);
    let benchmark = document["benchmark"].clone();
    for (name, bench, length) in [
        ("with_benchmark", Some(&benchmark), closes.len()),
        ("without_benchmark", None, closes.len()),
        ("short", None, 40),
    ] {
        let actual = rank_ic_diagnostic(&closes[..length], &dates[..length], bench, 5, None, 20);
        assert_json_close(&actual, &document["rank_ic"][name], name);
    }
}
