//! Differential parity for the independent transform audit (`validation/probability_quality.py`).
//!
//! Every tampering verdict, OOS audit count and directional-edge reason is compared.
//! Numeric leaves that depend on libm (`p_vol`, `z_score`, ...) use a 1e-15 relative
//! tolerance; everything else must match exactly.

use std::path::Path;

use chrono::NaiveDate;
use sd_audit::probability_quality::{
    audit_current_prediction, audit_identity_record, audit_oos_result, audit_shrinkage_record,
    build_probability_validation, directional_edge_assessment, path_has_detected_leakage,
    unique_research_paths,
};
use serde_json::{Map, Value};

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/probability-quality/audit.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn bars(document: &Value) -> Vec<Value> {
    document["bars"].as_array().expect("bars").clone()
}

fn dates(document: &Value) -> Vec<String> {
    document["dates"]
        .as_array()
        .expect("dates")
        .iter()
        .map(|item| item.as_str().expect("date").to_string())
        .collect()
}

fn closes(document: &Value) -> Vec<f64> {
    document["closes"]
        .as_array()
        .expect("closes")
        .iter()
        .map(|item| item.as_f64().expect("close"))
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

fn row(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().expect("row object")
}

#[test]
fn transform_tampering_verdicts_match_python() {
    let document = read();
    let dates = dates(&document);
    let closes = closes(&document);
    for case in document["tamper"].as_array().expect("tamper cases") {
        let name = case["case"].as_str().expect("case");
        let actual = audit_shrinkage_record(&row(&case["input"]), &dates, &closes, false);
        assert_json_close(&actual, &case["result"], name);
    }
    // The valid row must pass and each single-field tamper must name its own reason.
    let valid = audit_shrinkage_record(&row(&document["transform-row"]), &dates, &closes, false);
    assert_eq!(valid["ok"], true);
    let k_case = document["tamper"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["case"] == "k")
        .expect("k case");
    let result = audit_shrinkage_record(&row(&k_case["input"]), &dates, &closes, false);
    assert_eq!(result["reasons"][0], "PRIOR_STRENGTH_TAMPERED");
}

#[test]
fn legacy_and_identity_records_match_python() {
    let document = read();
    let dates = dates(&document);
    let closes = closes(&document);
    for case in document["legacy"].as_array().expect("legacy cases") {
        let actual = audit_shrinkage_record(&row(&case["input"]), &dates, &closes, false);
        assert_json_close(&actual, &case["result"], case["case"].as_str().unwrap());
    }
    for case in document["identity"].as_array().expect("identity cases") {
        let actual = audit_identity_record(&case["input"]);
        assert_json_close(&actual, &case["result"], case["case"].as_str().unwrap());
    }
}

#[test]
fn oos_and_current_audit_match_python() {
    let document = read();
    let bars = bars(&document);
    let oos = &document["oos"];
    let sessions: Vec<NaiveDate> = oos["sessions"]
        .as_array()
        .expect("sessions")
        .iter()
        .map(|item| NaiveDate::parse_from_str(item.as_str().unwrap(), "%Y-%m-%d").expect("session"))
        .collect();
    assert_json_close(
        &audit_oos_result(&oos["input"], Some(&bars), Some(&sessions)),
        &oos["with_bars"],
        "oos/with_bars",
    );
    assert_json_close(
        &audit_oos_result(&oos["input"], None, None),
        &oos["without_bars"],
        "oos/without_bars",
    );
    let current = &document["current"];
    assert_json_close(
        &audit_current_prediction(&current["valid-input"], Some(&bars)),
        &current["valid"],
        "current/valid",
    );
    assert_json_close(
        &audit_current_prediction(&serde_json::json!({"prob_up": null}), Some(&bars)),
        &current["abstained"],
        "current/abstained",
    );
    assert_json_close(
        &audit_current_prediction(&serde_json::json!({"prob_up": 0.5}), None),
        &current["without_bars"],
        "current/without_bars",
    );
}

#[test]
fn directional_edge_and_helpers_match_python() {
    let document = read();
    let edge_inputs = &document["edge-inputs"];
    let replay = edge_inputs["replay"].as_array().expect("replay").clone();
    let prospective = edge_inputs["prospective"]
        .as_array()
        .expect("prospective")
        .clone();
    for case in ["empty", "replay"] {
        let replay_records = if case == "empty" {
            Vec::new()
        } else {
            replay.clone()
        };
        let actual = directional_edge_assessment(&replay_records, &prospective);
        assert_json_close(&actual, &document["edge"][case], case);
    }
    let helpers = &document["helpers"];
    let researchers = vec![
        serde_json::json!({"researcher_id": "quant"}),
        serde_json::json!({"researcher_id": "ml"}),
        serde_json::json!({"researcher_id": "quant"}),
        serde_json::json!({}),
    ];
    assert_eq!(
        Value::Array(
            unique_research_paths(&researchers)
                .into_iter()
                .map(Value::String)
                .collect()
        ),
        helpers["unique_paths"]
    );
    assert_eq!(
        Value::Bool(path_has_detected_leakage(&serde_json::json!({
            "validation": {"leakage_audit": {"a": false, "b": true}}
        }))),
        helpers["leakage-true"]
    );
    assert_eq!(
        Value::Bool(path_has_detected_leakage(&serde_json::json!({
            "validation": {"leakage_audit": {"a": true}}
        }))),
        helpers["leakage-false"]
    );
    let build_input = document["build_validation"]["input"]
        .as_array()
        .expect("build input")
        .clone();
    let (quality, skill) = build_probability_validation(&build_input, None, 1e-9);
    assert_json_close(
        &quality,
        &document["build_validation"]["quality"],
        "build_validation/quality",
    );
    assert_eq!(skill, document["build_validation"]["skill"]);
}
