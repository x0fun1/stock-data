//! Differential parity for the effectiveness statistics.
//!
//! Reference values come from `quant_research/statistics.py`. These numbers decide
//! the audit verdict, so the bootstrap block in particular must match the Python
//! stream (and Python 3.12+ compensated sums) exactly, not approximately.

use std::path::Path;

use sd_core::statistics::{
    auc, base_rate_standard_error, baseline_comparison, block_lengths_for, bootstrap_mean_interval,
    brier, effective_sample_size, mean, paired_rows, power_curve,
};
use serde_json::Value;

/// Compare JSON trees by numeric value, ignoring float spelling (`1e-05` vs `0.00001`).
fn assert_json_equal(actual: Value, expected: Value) {
    match (&actual, &expected) {
        (Value::Number(left), Value::Number(right)) => {
            assert_eq!(
                left.as_f64().expect("number"),
                right.as_f64().expect("number"),
                "numeric value differs"
            );
        }
        (Value::Array(left), Value::Array(right)) => {
            assert_eq!(left.len(), right.len(), "array length differs");
            for (actual_item, expected_item) in left.iter().zip(right.iter()) {
                assert_json_equal(actual_item.clone(), expected_item.clone());
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            assert_eq!(left.len(), right.len(), "object size differs");
            for (key, expected_item) in right.iter() {
                let actual_item = left.get(key).unwrap_or_else(|| panic!("missing key {key}"));
                assert_json_equal(actual_item.clone(), expected_item.clone());
            }
        }
        _ => assert_eq!(actual, expected),
    }
}

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/statistics/effectiveness.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn floats(value: &Value) -> Vec<f64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_f64().expect("number"))
        .collect()
}

fn opt(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

#[test]
fn paired_rows_match_python() {
    let document = read();
    let records = document["inputs"]["records"].as_array().expect("records");
    assert_json_equal(
        Value::Array(paired_rows(records)),
        document["paired_rows"].clone(),
    );
}

#[test]
fn scalar_statistics_match_python() {
    let documents = read();
    let expected_mean = documents["mean"].as_array().unwrap();
    for (index, values) in [vec![], vec![1.0, 2.0, 3.0], vec![0.5]].iter().enumerate() {
        assert_eq!(
            opt(mean(values)),
            expected_mean[index],
            "mean differs for case {index}"
        );
    }
    let expected_brier = documents["brier"].as_array().unwrap();
    assert_eq!(opt(brier(&[0.1, -0.2, 0.05])), expected_brier[0]);
    assert_eq!(opt(brier(&[])), expected_brier[1]);
    let expected_auc = &documents["auc"];
    assert_eq!(
        opt(auc(&[0.1, 0.1, 0.4, 0.8, 0.8, 0.9], &[0, 0, 0, 1, 1, 1])),
        expected_auc["ties"][0],
        "tied AUC differs"
    );
    assert_eq!(opt(auc(&[0.2, 0.4], &[0, 0])), expected_auc["one_class"][0]);
    assert_eq!(opt(auc(&[0.2, 0.4], &[1, 1])), expected_auc["one_class"][1]);
    assert_eq!(
        opt(auc(&[0.2, 0.7, 0.4, 0.9], &[0, 1, 0, 1])),
        expected_auc["plain"]
    );
    let samples = documents["effective_sample_size"].as_array().unwrap();
    assert_eq!(Value::from(effective_sample_size(57, 5)), samples[0]);
    assert_eq!(Value::from(effective_sample_size(3, 5)), samples[1]);
    assert_eq!(Value::from(effective_sample_size(10, 0)), samples[2]);
    let lengths = documents["block_lengths"].as_array().unwrap();
    assert_eq!(Value::from(block_lengths_for(57, 5)), lengths[0]);
    assert_eq!(Value::from(block_lengths_for(8, 5)), lengths[1]);
    assert_eq!(Value::from(block_lengths_for(3, 5)), lengths[2]);
    let std_errors = documents["base_rate_standard_error"].as_array().unwrap();
    assert_eq!(opt(base_rate_standard_error(Some(0.55), 11)), std_errors[0]);
    assert_eq!(opt(base_rate_standard_error(None, 11)), std_errors[1]);
    assert_eq!(opt(base_rate_standard_error(Some(0.55), 0)), std_errors[2]);
}

#[test]
fn bootstrap_mean_interval_matches_python() {
    let documents = read();
    let expected = &documents["bootstrap"];
    let improvements = floats(&documents["inputs"]["improvements"]);
    assert_json_equal(
        bootstrap_mean_interval(&improvements, 6, 100, 11),
        expected["estimated"].clone(),
    );
    assert_json_equal(
        bootstrap_mean_interval(&[], 6, 100, 11),
        expected["no_values"].clone(),
    );
    assert_json_equal(
        bootstrap_mean_interval(&[0.25; 40], 6, 100, 11),
        expected["degenerate"].clone(),
    );
    assert_json_equal(
        bootstrap_mean_interval(&improvements[..10], 6, 100, 11),
        expected["insufficient"].clone(),
    );
}

#[test]
fn baseline_comparison_matches_python() {
    let documents = read();
    let records = documents["inputs"]["records"].as_array().expect("records");
    let expected = &documents["baseline_comparison"];
    assert_json_equal(
        baseline_comparison(records, 5, None),
        expected["default"].clone(),
    );
    assert_json_equal(
        baseline_comparison(records, 5, Some(&[6])),
        expected["custom_lengths"].clone(),
    );
    assert_json_equal(baseline_comparison(&[], 5, None), expected["empty"].clone());
}

#[test]
fn power_curve_matches_python() {
    let documents = read();
    assert_json_equal(
        Value::Array(power_curve(
            &[30, 60, 90, 120, 250],
            &[0.55, 0.60, 0.63],
            0.5,
            0.05,
        )),
        documents["power_curve"]["default"].clone(),
    );
    assert_json_equal(
        Value::Array(power_curve(&[30, 120], &[0.55, 0.63], 0.5, 0.05)),
        documents["power_curve"]["custom"].clone(),
    );
}
