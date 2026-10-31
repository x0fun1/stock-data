//! Differential parity for the four registered R11 experiments.

use std::path::Path;

use sd_app::experiments::{momentum_ab, research_b_ab, research_candidates, selection_ab};
use serde_json::Value;

fn fixture(path: &str) -> Value {
    let full = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/experiments")
        .join(path);
    serde_json::from_str(
        &std::fs::read_to_string(&full).unwrap_or_else(|error| panic!("{full:?}: {error}")),
    )
    .expect("json")
}

fn fixture_text(path: &str) -> String {
    let full = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/experiments")
        .join(path);
    std::fs::read_to_string(&full).unwrap_or_else(|error| panic!("{full:?}: {error}"))
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
fn momentum_ab_matches_python() {
    let inputs = fixture("momentum/inputs.json");
    let payloads = inputs["payloads"].as_object().expect("payloads").clone();
    let manifest = inputs["pool_manifest"]
        .as_object()
        .expect("manifest")
        .clone();
    let panel = momentum_ab::load_panel(&payloads, Some(&manifest)).expect("panel");
    // The panel is a Rust struct; compare through its report instead of re-encoding fields.
    let report = momentum_ab::build_report(&panel);
    assert_json_close(&report, &fixture("momentum/report.json"), "momentum/report");
    assert_eq!(
        momentum_ab::render_markdown(&report),
        fixture_text("momentum/report.md"),
        "momentum/report.md"
    );
    let panel_reference = fixture("momentum/panel.json");
    assert_eq!(
        report["pool"]["sessions"],
        panel_reference["dates"]
            .as_array()
            .map(Vec::len)
            .unwrap_or(0)
    );
    assert_eq!(
        report["pool"]["symbol_count"],
        panel_reference["closes"]
            .as_object()
            .map(|map| map.len())
            .unwrap_or(0)
    );
    // The registered verdict can never claim production impact.
    assert!(matches!(
        report["verdict"].as_str(),
        Some("composite_better" | "composite_worse" | "indistinguishable")
    ));
    assert_eq!(report["preregistration"]["protocol"], "momentum-ab-v1");
}

#[test]
fn selection_ab_matches_python() {
    let inputs = fixture("selection_ab/inputs.json");
    let pool = selection_ab::load_pool(
        &inputs["pool"],
        inputs["collections"].as_object().expect("collections"),
        inputs["asof"].as_str().expect("asof"),
    )
    .expect("pool");
    assert_eq!(
        pool.len(),
        3,
        "all registered symbols must survive the gates"
    );
    let report = selection_ab::evaluate(&pool);
    let expected: Value =
        serde_json::from_str(&fixture_text("selection_ab/report.json")).expect("report");
    assert_json_close(&report, &expected, "selection_ab/report");
    assert_eq!(
        selection_ab::render_markdown(&report),
        fixture_text("selection_ab/report.md"),
        "selection_ab/report.md"
    );
}

#[test]
fn research_b_ab_matches_python() {
    let inputs = fixture("research_b_ab/inputs.json");
    let pool = research_b_ab::load_pool(
        &inputs["pool"],
        inputs["collections"].as_object().expect("collections"),
        inputs["asof"].as_str().expect("asof"),
    )
    .expect("pool");
    assert_eq!(pool.len(), 3);
    let report = research_b_ab::evaluate(&pool).expect("evaluate");
    let expected: Value =
        serde_json::from_str(&fixture_text("research_b_ab/report.json")).expect("report");
    assert_json_close(&report, &expected, "research_b_ab/report");
    assert_eq!(
        research_b_ab::render_markdown(&report),
        fixture_text("research_b_ab/report.md"),
        "research_b_ab/report.md"
    );
    assert_eq!(report["production_unchanged"], Value::Bool(true));
}

#[test]
fn research_candidates_matches_python() {
    let inputs = fixture("research_candidates/inputs.json");
    let bars = inputs["bars"].as_array().expect("bars").clone();
    let ml = research_candidates::compare_ml(&bars, 5).expect("ml comparison");
    assert_json_close(
        &ml,
        &fixture("research_candidates/ml.json"),
        "candidates/ml",
    );
    // The registered quant arm hits the same legacy price-basis defect as Python.
    let expected_error = fixture_text("research_candidates/evaluate_error.txt")
        .trim()
        .to_string();
    let quant_error = research_candidates::compare_quant(&bars, 5).expect_err("legacy defect");
    assert_eq!(quant_error, expected_error);
    let evaluate_error = research_candidates::evaluate(&bars, 5).expect_err("legacy defect");
    assert_eq!(evaluate_error, expected_error);
    // The protocol itself must stay frozen and non-switching.
    let prereg = research_candidates::preregistration();
    assert_eq!(prereg["minimum_sessions"], 120);
    assert_eq!(
        prereg["switch_policy"],
        "never automatic; a supported candidate still requires a separate prospective confirmation"
    );
}
