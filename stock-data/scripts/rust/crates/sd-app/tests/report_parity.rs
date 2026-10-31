//! Differential parity for the fixed report block, agent summary budget and synthesis.

use std::path::Path;

use sd_app::report::{
    build_agent_summary, enforce_agent_summary_budget, render_final_response, render_markdown,
    reporting_status_for, required_response_lines, AGENT_SUMMARY_BUDGET,
};
use sd_app::synthesis::{build_synthesis, reportable_quant_fields};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/report/report.json");
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

fn lines_value(lines: &serde_json::Map<String, Value>) -> Value {
    Value::Object(lines.clone())
}

#[test]
fn report_block_matches_python() {
    let document = read();
    for name in ["reportable", "unavailable"] {
        let result = &document["inputs"][name];
        let expected = &document[name];
        assert_eq!(
            Value::String(reporting_status_for(result).to_string()),
            expected["reporting_status"],
            "{name}: reporting status"
        );
        assert_json_close(
            &lines_value(&required_response_lines(result)),
            &expected["lines"],
            &format!("{name}/lines"),
        );
        assert_json_close(
            &build_agent_summary(result),
            &expected["agent_summary"],
            &format!("{name}/agent_summary"),
        );
        assert_eq!(
            Value::String(render_final_response(result)),
            expected["final_response"],
            "{name}: final response"
        );
        assert_eq!(
            Value::String(render_markdown(result)),
            expected["markdown"],
            "{name}: markdown"
        );
        assert_eq!(
            Value::String(reporting_status_for(result).to_string()),
            expected["reporting_status"]
        );
    }
    // The three News states are disclosed separately; partial is assessed evidence.
    for status in ["complete", "partial", "not_assessed"] {
        let adjusted = &document["inputs"][format!("news_{status}")];
        let expected = &document["news_states"][status];
        assert_json_close(
            &lines_value(&required_response_lines(adjusted)),
            &expected["lines"],
            &format!("news_states/{status}/lines"),
        );
        assert_eq!(
            Value::String(render_final_response(adjusted)),
            expected["final_response"],
            "news_states/{status}: final response"
        );
        assert_eq!(
            Value::String(render_markdown(adjusted)),
            expected["markdown"],
            "news_states/{status}: markdown"
        );
    }
}

#[test]
fn summary_budget_matches_python() {
    let document = read();
    let padded = &document["inputs"]["pad_result"];
    let summary = build_agent_summary(padded);
    assert_json_close(
        &enforce_agent_summary_budget(summary.clone(), 2000),
        &document["budget"]["trimmed"],
        "budget/trimmed",
    );
    let base = &document["inputs"]["reportable"];
    assert_json_close(
        &enforce_agent_summary_budget(build_agent_summary(base), AGENT_SUMMARY_BUDGET),
        &document["budget"]["untouched"],
        "budget/untouched",
    );
}

#[test]
fn synthesis_matches_python() {
    let document = read();
    for name in ["reportable", "unavailable"] {
        let result = &document["inputs"][name];
        let news = &result["news_result"];
        assert_json_close(
            &build_synthesis(result, news).expect("synthesis"),
            &document[name]["synthesis"],
            &format!("{name}/synthesis"),
        );
        assert_json_close(
            &reportable_quant_fields(result),
            &document[name]["quant_fields"],
            &format!("{name}/quant_fields"),
        );
    }
    let mismatch = build_synthesis(
        &document["inputs"]["reportable"],
        &document["inputs"]["mismatch_news"],
    )
    .expect_err("identity mismatch must be rejected");
    assert_eq!(
        mismatch,
        document["synthesis_mismatch"]
            .as_str()
            .expect("fixture message")
    );
}
