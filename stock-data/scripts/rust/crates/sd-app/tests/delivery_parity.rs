//! Differential parity for the delivery gate (`delivery.py`).

use std::path::{Path, PathBuf};

use sd_app::delivery::{
    require_ai_news_coverage, validate_final_response, validate_response_text, validate_stages,
};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/delivery/delivery.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn fixture_dir(prefix: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/delivery")
        .join(prefix)
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
            assert_eq!(
                left.len(),
                right.len(),
                "{path}: object size differs ({:?} vs {:?})",
                left.keys().collect::<Vec<_>>(),
                right.keys().collect::<Vec<_>>()
            );
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
fn response_and_stage_contracts_match_python() {
    let document = read();
    let result = &document["inputs"]["result"];
    let valid_response = document["inputs"]["valid_response"]
        .as_str()
        .expect("response");
    validate_response_text(result, valid_response).expect("valid response");
    assert!(validate_response_text(result, &format!("<!-- hidden -->\n{valid_response}")).is_ok());

    for (name, expected) in document["response_text"].as_object().expect("cases") {
        if expected.get("ok").is_some() {
            continue;
        }
        let text = match name.as_str() {
            "missing_line" => valid_response
                .lines()
                .take(8)
                .collect::<Vec<_>>()
                .join("\n"),
            "conflicting_probability" => format!("{valid_response}\n上涨概率：99.9%"),
            "forbidden" => format!("{valid_response}\nQuant Confidence: 0.9"),
            other => panic!("unexpected response case {other}"),
        };
        assert_eq!(
            validate_response_text(result, &text).expect_err(name),
            expected.as_str().expect("error text"),
            "response case {name}"
        );
    }

    assert_json_close(
        &validate_stages(result).expect("valid stages"),
        &document["stages"]["valid"],
        "stages",
    );
    let mut other_run = result.clone();
    other_run["stages"][0]["run_id"] = Value::String("run-2".into());
    assert_eq!(
        validate_stages(&other_run).expect_err("other run"),
        document["stages"]["other_run"].as_str().expect("message")
    );
    let mut bad_status = result.clone();
    bad_status["stages"][0]["status"] = Value::String("finished".into());
    assert_eq!(
        validate_stages(&bad_status).expect_err("bad status"),
        document["stages"]["bad_status"].as_str().expect("message")
    );
    let mut missing_reason = result.clone();
    missing_reason["stages"][0]["status"] = Value::String("partial".into());
    assert_eq!(
        validate_stages(&missing_reason).expect_err("missing reason"),
        document["stages"]["missing_reason"]
            .as_str()
            .expect("message")
    );
}

#[test]
fn coverage_contract_matches_python() {
    let document = read();
    let news = &document["inputs"]["result"]["news_result"];
    for (name, expected) in document["coverage"].as_object().expect("coverage") {
        let mut candidate = news.clone();
        match name.as_str() {
            "no_candidates" => candidate["ai_lean_candidates"] = serde_json::json!([]),
            "partial_with_warnings" | "partial_without_warnings" => {
                candidate["status"] = Value::String("partial".into());
                candidate["warnings"] = if name == "partial_with_warnings" {
                    serde_json::json!(["partial coverage"])
                } else {
                    serde_json::json!([])
                };
                candidate["ai_lean"]["status"] = Value::String("partial".into());
                candidate["ai_lean"]["coverage"]["assessed_count"] = Value::from(1);
                candidate["ai_lean"]["items"] =
                    serde_json::json!([{"news_id": "event-1", "lean": "bullish"}]);
            }
            "not_assessed" => candidate["ai_lean"]["status"] = Value::String("unassessed".into()),
            "count_mismatch" => {
                candidate["ai_lean"]["coverage"]["candidate_count"] = Value::from(5)
            }
            "assessed_gap" => candidate["ai_lean"]["coverage"]["assessed_count"] = Value::from(1),
            "items_mismatch" => candidate["ai_lean"]["items"] = serde_json::json!([]),
            other => panic!("unexpected coverage case {other}"),
        }
        match require_ai_news_coverage(&candidate) {
            Ok(()) => assert!(
                expected.is_null(),
                "coverage case {name} should have been rejected"
            ),
            Err(message) => assert_eq!(
                message,
                expected.as_str().expect("message"),
                "coverage case {name}"
            ),
        }
    }
}

#[test]
fn final_response_gate_matches_python() {
    let document = read();
    let directory = fixture_dir("valid");
    let actual =
        validate_final_response(&directory, &directory.join("response.md")).expect("valid");
    let expected = document["final_valid"].clone();
    let mut actual_without_path = actual.clone();
    if let Some(map) = actual_without_path.as_object_mut() {
        map.insert("response_file".into(), Value::String("response.md".into()));
    }
    assert_json_close(&actual_without_path, &expected, "final_valid");
    assert_eq!(actual["status"], "success");
    assert_eq!(actual["delivery_status"], "validated");
    assert_eq!(actual["reporting_status"], "reportable");

    let missing = fixture_dir("missing_line");
    assert_eq!(
        validate_final_response(&missing, &missing.join("response.md")).expect_err("missing line"),
        document["final_missing_line_error"]
            .as_str()
            .expect("message")
    );
    let missing = fixture_dir("missing_freeze");
    assert_eq!(
        validate_final_response(&missing, &missing.join("response.md"))
            .expect_err("missing freeze"),
        document["final_errors"]["missing_freeze"]
            .as_str()
            .expect("message")
    );

    // The frozen file and the analysis bundle disagree: the receipt itself is valid.
    let result = &document["inputs"]["result"];
    let mut tampered_news = result["news_result"].clone();
    tampered_news["warnings"] = serde_json::json!(["tampered after freeze"]);
    let receipt = sd_news::pipeline::freeze_receipt(
        "news_result.json",
        &tampered_news,
        "2026-01-05T21:00:00Z",
    );
    let directory =
        std::env::temp_dir().join(format!("sd-delivery-tampered-{}", std::process::id()));
    std::fs::create_dir_all(&directory).expect("tmp dir");
    std::fs::write(
        directory.join("analysis.json"),
        serde_json::to_vec(result).expect("analysis"),
    )
    .expect("write analysis");
    std::fs::write(
        directory.join("news_result.json"),
        serde_json::to_vec(&tampered_news).expect("news"),
    )
    .expect("write news");
    std::fs::write(
        directory.join("news_result.freeze.json"),
        serde_json::to_vec(&receipt).expect("receipt"),
    )
    .expect("write receipt");
    std::fs::write(
        directory.join("response.md"),
        document["inputs"]["valid_response"]
            .as_str()
            .expect("response"),
    )
    .expect("write response");
    let actual = validate_final_response(&directory, &directory.join("response.md"))
        .expect_err("tampered news");
    assert_eq!(
        actual,
        document["final_errors"]["tampered"]
            .as_str()
            .expect("message")
    );
    std::fs::remove_dir_all(&directory).ok();
}
