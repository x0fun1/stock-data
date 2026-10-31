//! Differential parity for the single-path consensus (`consensus.py`).
//!
//! The consensus payload is what every renderer and delivery check reads, so the whole
//! tree is compared: assessment, evidence, shadow isolation, diagnostics and the veto
//! redaction. Numeric leaves use a 1e-15 relative tolerance for platform libm calls.

use std::path::Path;

use sd_app::consensus::{
    build_consensus, economic_viability, forecast_assessment, manifest_policy_reasons,
    oos_support_sessions, reason_text, redact_publishable_prediction, AssessmentInput,
    ConsensusInput,
};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/consensus/consensus.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn floats_ok(actual: &Value, expected: &Value, path: &str) {
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
                floats_ok(actual_item, expected_item, &format!("{path}[{index}]"));
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            assert_eq!(left.len(), right.len(), "{path}: object size differs");
            for (key, expected_item) in right.iter() {
                let actual_item = left
                    .get(key)
                    .unwrap_or_else(|| panic!("{path}: missing key {key}"));
                floats_ok(actual_item, expected_item, &format!("{path}/{key}"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}: value differs"),
    }
}

#[test]
fn assessment_and_consensus_match_python() {
    let document = read();
    let cases = document["cases"].as_array().expect("cases");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["case"].as_str().expect("case");
        let input = &case["input"];
        let researchers = input["researchers"].as_array().expect("researchers");
        let bars = case["bars"].as_array().expect("bars");
        let result = researchers.first().cloned().unwrap_or(Value::Null);
        let assessment = forecast_assessment(&AssessmentInput {
            result: &result,
            manifest: &input["manifest"],
            oos_evidence_audit: None,
            bars: Some(bars),
            sessions: None,
            prospective_records: &[],
        });
        floats_ok(
            &assessment,
            &case["assessment"],
            &format!("{name}/assessment"),
        );

        let consensus = build_consensus(&ConsensusInput {
            researchers,
            manifest: &input["manifest"],
            assessments: None,
            bars: Some(bars),
            sessions: None,
            prospective_records: &[],
        });
        floats_ok(&consensus, &case["consensus"], &format!("{name}/consensus"));
        assert_eq!(consensus["aggregation_method"], "single_path");
        assert_eq!(consensus["production_path_count"], 1);

        let mut redacted = consensus.clone();
        redact_publishable_prediction(
            &mut redacted,
            &serde_json::json!({"reason_codes": ["CALENDAR_UNVERIFIED", "OOS_SUPPORT_INSUFFICIENT"]}),
        );
        floats_ok(&redacted, &case["redacted"], &format!("{name}/redacted"));
        // A veto must leave no publishable probability or direction anywhere.
        assert_eq!(redacted["status"], "unavailable");
        assert_eq!(redacted["prob_up"], Value::Null);
        assert_eq!(redacted["direction"], "unavailable");
        assert_eq!(redacted["prediction"]["prob_up"], Value::Null);
        assert_eq!(redacted["production"]["prob_up"], Value::Null);
        assert!(redacted["production"]
            .get("probability_transform")
            .is_none());
        assert_eq!(redacted["audit_only"]["publishable"], false);
        assert_eq!(redacted["redaction"]["publishable"], false);
    }
}

#[test]
fn helpers_match_python() {
    let document = read();
    let edge = &document["edge"];
    assert_eq!(oos_support_sessions(), edge["empty"]);
    let codes: Vec<String> = [
        "POLICY_VERSION_MISMATCH",
        "CURRENT_MATCHING_SAMPLES_INSUFFICIENT",
        "OOS_SUPPORT_INSUFFICIENT",
        "PROSPECTIVE_SUPPORT_INSUFFICIENT",
        "TARGET_SESSION_UNRESOLVED",
        "UNKNOWN_CODE",
    ]
    .iter()
    .map(|code| (*code).to_string())
    .collect();
    assert_eq!(
        Value::Array(reason_text(&codes).into_iter().map(Value::String).collect()),
        edge["reason-text"]
    );
    let manifest = &document["cases"][0]["input"]["manifest"];
    assert_eq!(
        Value::Array(
            manifest_policy_reasons(manifest)
                .into_iter()
                .map(Value::String)
                .collect()
        ),
        edge["manifest-reasons"]
    );
    let researchers = document["cases"][0]["input"]["researchers"]
        .as_array()
        .unwrap();
    assert_eq!(
        economic_viability(&researchers[..1]),
        edge["economic-viability"]
    );
    assert_eq!(economic_viability(&[]), edge["economic-unavailable"]);
}
