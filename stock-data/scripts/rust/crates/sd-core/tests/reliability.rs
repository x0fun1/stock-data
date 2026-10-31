//! Differential parity for the reliability accounting (`reliability.py`).
//!
//! Logs and the block-bootstrap interval depend on the platform libm, so numeric leaves
//! are compared with a 1e-15 relative tolerance while every structural field, count and
//! status string is exact.

use std::path::Path;

use sd_core::reliability::{
    logistic_shortfall, probabilistic_scores, qualification, sample_diagnostics,
};
use serde_json::Value;

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/reliability/reliability.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
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
fn logistic_shortfall_matches_python() {
    let document = read();
    let expected = document["logistic_shortfall"]
        .as_array()
        .expect("shortfall");
    for (index, value) in [0i64, 11, 12, 60, 119, 120].iter().enumerate() {
        assert_json_close(
            &logistic_shortfall(*value),
            &expected[index],
            &format!("shortfall[{index}]"),
        );
    }
    // The floor is 60 + 30 + 30 and the stages may not share observations.
    assert_eq!(logistic_shortfall(119)["shortfall"], 1);
    assert_eq!(logistic_shortfall(120)["status"], "sample_threshold_met");
    assert_eq!(
        logistic_shortfall(119)["status"],
        "not_applicable_insufficient_samples"
    );
}

#[test]
fn sample_diagnostics_match_python() {
    let document = read();
    let expected = &document["sample_diagnostics"];
    // The fixture stores the dates it used inside the with_dates row's basis only; rebuild
    // the same weekday calendar the Python builder used.
    let mut dates = Vec::new();
    let mut cursor = chrono::NaiveDate::from_ymd_opt(2024, 1, 2).expect("date");
    while dates.len() < 300 {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            dates.push(cursor.to_string());
        }
        cursor = cursor.succ_opt().expect("next day");
    }
    assert_json_close(
        &sample_diagnostics(300, 60, 5, Some(&dates)),
        &expected["with_dates"],
        "with_dates",
    );
    assert_json_close(
        &sample_diagnostics(300, 60, 5, None),
        &expected["legacy_spacing"],
        "legacy_spacing",
    );
    assert_json_close(
        &sample_diagnostics(4, 10, 5, None),
        &expected["too_short"],
        "too_short",
    );
}

#[test]
fn probabilistic_scores_match_python() {
    let document = read();
    let expected = &document["probabilistic_scores"];
    let build = |step: i64, count: i64| -> Vec<(i64, f64, Option<f64>, i64)> {
        (0..count)
            .map(|index| {
                let outcome = index % 2;
                (
                    100 + index * step,
                    0.4 + 0.02 * index as f64,
                    Some(0.45 + 0.01 * index as f64),
                    outcome,
                )
            })
            .collect()
    };
    assert_json_close(
        &probabilistic_scores(&build(1, 40), 5),
        &expected["daily"],
        "daily",
    );
    assert_json_close(
        &probabilistic_scores(&build(7, 40), 5),
        &expected["non_overlapping"],
        "non_overlapping",
    );
    let no_base: Vec<(i64, f64, Option<f64>, i64)> = (0..25)
        .map(|index| (100 + index, 0.5, None, index % 2))
        .collect();
    assert_json_close(
        &probabilistic_scores(&no_base, 5),
        &expected["no_base_rate"],
        "no_base_rate",
    );
    assert_json_close(&probabilistic_scores(&[], 5), &expected["empty"], "empty");
    let small = vec![(100, 0.6, Some(0.5), 1), (101, 0.4, Some(0.5), 0)];
    assert_json_close(
        &probabilistic_scores(&small, 5),
        &expected["small"],
        "small",
    );
}

#[test]
fn qualification_matches_python() {
    let document = read();
    let expected = &document["qualification"];
    assert_json_close(
        &qualification(20, 8, 3, 10),
        &expected["passing"],
        "passing",
    );
    assert_json_close(&qualification(3, 2, 1, 4), &expected["failing"], "failing");
}
