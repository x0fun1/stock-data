//! Parity for the Research D descriptive factor diagnostics (`factor_backtest.py`).

use std::path::Path;

use sd_research::factor_backtest::run;
use serde_json::Value;

fn fixture(path: &str) -> Value {
    let full = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(path);
    serde_json::from_str(
        &std::fs::read_to_string(&full).unwrap_or_else(|error| panic!("{full:?}: {error}")),
    )
    .expect("json")
}

fn assert_json_close(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(left), Value::Number(right)) => {
            let left_value = left.as_f64().expect("number");
            let right_value = right.as_f64().expect("number");
            let tolerance = 1e-12 * right_value.abs().max(1.0);
            assert!(
                (left_value - right_value).abs() <= tolerance,
                "{path}: {left_value} vs {right_value}"
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
fn descriptive_backtest_matches_python() {
    let manifest = fixture("snapshot/complete-window/manifest.json");
    let mut market = fixture("snapshot/complete-window/market.json");
    market["_data_validation"] = manifest["data_validation"].clone();
    let actual = run(&manifest, &market).expect("run");
    let expected = fixture("factor-backtest/descriptive.json");
    assert_json_close(&actual, &expected, "descriptive");

    // Research D can never publish a probability or promote itself.
    assert_eq!(actual["researcher_id"], "factor_backtest");
    assert_eq!(actual["result_role"], "diagnostic");
    assert!(actual["prob_up"].is_null());
    assert!(actual["evidence"]["positive"]
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(actual["validation"]["descriptive_only"], true);
    assert_eq!(
        actual["validation"]["leakage_audit"]["future_returns_used_only_as_labels"],
        true
    );
    assert_eq!(
        actual["validation"]["full_cross_sectional_backtest"]["status"],
        "not_assessed"
    );
}
