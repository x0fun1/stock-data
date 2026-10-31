//! Differential parity for purged CV, factor selection and feasibility disclosures.

use std::path::Path;

use sd_research::feasibility::{build_forecast_feasibility, build_timing};
use sd_research::purged_cv::{
    causal_walk_forward, count_interval_overlaps, folds_to_value, purged_kfold, SampleInterval,
    WalkForwardOptions,
};
use sd_research::selection::{
    multiple_testing_disclosure, overlapping_ic, shift_p_values, stability_verdict,
};
use serde_json::Value;

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/research-tooling/toolkit.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
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

fn intervals(count: i64) -> Vec<SampleInterval> {
    (0..count)
        .map(|index| SampleInterval {
            decision_session: index,
            information_start: index,
            label_end: index + 5,
        })
        .collect()
}

fn fold_values(folds: &[sd_research::purged_cv::Fold]) -> Value {
    folds_to_value(folds)
}

#[test]
fn purged_cv_matches_python() {
    let document = read();
    let data = &document["purged_kfold"];
    assert_json_close(
        &fold_values(&purged_kfold(&intervals(30), 5, 0).expect("folds")),
        &data["no_embargo"],
        "no_embargo",
    );
    assert_json_close(
        &fold_values(&purged_kfold(&intervals(30), 5, 2).expect("folds")),
        &data["embargo_2"],
        "embargo_2",
    );
    assert_eq!(
        purged_kfold(&intervals(30), 40, 0).expect("too few").len(),
        0
    );
    assert_eq!(
        purged_kfold(&intervals(30), 5, -1).expect_err("negative embargo"),
        data["negative_embargo"].as_str().unwrap()
    );

    let walk = &document["walk_forward"];
    assert_json_close(
        &fold_values(
            &causal_walk_forward(&intervals(300), &WalkForwardOptions::default()).expect("walk"),
        ),
        &walk["default"],
        "walk/default",
    );
    let block_options = WalkForwardOptions {
        n_splits: 5,
        minimum_train: 20,
        pre_test_gap_sessions: 0,
        first_test_floor_fraction: 0.5,
        block_sessions: Some(7),
    };
    assert_json_close(
        &fold_values(&causal_walk_forward(&intervals(50), &block_options).expect("walk")),
        &walk["block_7"],
        "walk/block_7",
    );
    let gap_options = WalkForwardOptions {
        pre_test_gap_sessions: 2,
        minimum_train: 20,
        ..WalkForwardOptions::default()
    };
    assert_json_close(
        &fold_values(&causal_walk_forward(&intervals(50), &gap_options).expect("walk")),
        &walk["gap_2"],
        "walk/gap_2",
    );
    assert_eq!(
        causal_walk_forward(
            &intervals(30),
            &WalkForwardOptions {
                first_test_floor_fraction: 1.5,
                ..WalkForwardOptions::default()
            }
        )
        .expect_err("bad fraction"),
        walk["bad_fraction"].as_str().unwrap()
    );

    let overlaps = &document["overlaps"];
    assert_eq!(
        Value::from(count_interval_overlaps(
            &intervals(30),
            &(0..10).collect::<Vec<_>>(),
            &(20..30).collect::<Vec<_>>()
        )),
        overlaps["count"]
    );
    assert_eq!(
        Value::from(count_interval_overlaps(
            &intervals(30),
            &(0..5).collect::<Vec<_>>(),
            &(25..30).collect::<Vec<_>>()
        )),
        overlaps["disjoint"]
    );
}

fn optional_floats(value: &Value) -> Vec<Option<f64>> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_f64())
        .collect()
}

#[test]
fn selection_matches_python() {
    let document = read();
    let data = &document["selection"];
    let values = optional_floats(&document["selection_values"]);
    let labels = optional_floats(&document["selection_labels"]);
    let indices: Vec<i64> = (5..70).collect();
    assert_json_close(
        &overlapping_ic(&values, &labels, &indices, 5, 100, 7),
        &data["metrics"],
        "metrics",
    );
    assert_json_close(
        &overlapping_ic(&values, &labels, &(5..12).collect::<Vec<_>>(), 5, 500, 0),
        &data["insufficient"],
        "insufficient",
    );
    let metrics = overlapping_ic(&values, &labels, &indices, 5, 100, 7);
    assert_json_close(
        &stability_verdict(&metrics, 0.05, 0.5),
        &data["verdict"]["metrics"],
        "verdict",
    );
    assert_eq!(
        stability_verdict(
            &serde_json::json!({"status": "insufficient_samples"}),
            0.05,
            0.5
        ),
        data["verdict_insufficient"]
    );
    let p_values = vec![
        ("factor_a".to_string(), Some(0.01)),
        ("factor_b".to_string(), Some(0.2)),
        ("factor_c".to_string(), None),
    ];
    assert_json_close(
        &multiple_testing_disclosure(&p_values),
        &data["disclosure"],
        "disclosure",
    );
    let shift = shift_p_values(&values, &labels, &indices, 5);
    let actual: Value = Value::Array(
        shift
            .into_iter()
            .map(|(name, value)| serde_json::json!({name: value}))
            .collect(),
    );
    let expected: Value = Value::Array(
        data["shift"]
            .as_array()
            .map(|_| Vec::new())
            .unwrap_or_default(),
    );
    let _ = expected;
    // The fixture stores a dict; compare the single p-value object directly.
    let mut map = serde_json::Map::new();
    for (name, value) in shift_p_values(&values, &labels, &indices, 5) {
        map.insert(name, value.map(Value::from).unwrap_or(Value::Null));
    }
    assert_json_close(&Value::Object(map), &data["shift"], "shift");
    let _ = actual;
}

#[test]
fn feasibility_matches_python() {
    let document = read();
    let data = &document["feasibility"];
    let bars = document["feasibility_bars"]
        .as_array()
        .expect("bars")
        .clone();
    let manifest = &document["feasibility_manifest"];
    let results = document["feasibility_results"]
        .as_object()
        .expect("results")
        .clone();
    assert_json_close(
        &build_forecast_feasibility(manifest, &bars, Some(&results)),
        &data["with_results"],
        "with_results",
    );
    assert_json_close(
        &build_forecast_feasibility(manifest, &bars, None),
        &data["without_results"],
        "without_results",
    );
    assert_json_close(&build_timing(manifest, &bars), &data["timing"], "timing");
    assert_json_close(
        &build_forecast_feasibility(&serde_json::json!({"horizon": "5D"}), &bars[..70], None),
        &data["short_window"],
        "short_window",
    );
}
