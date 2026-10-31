//! Full A-path parity: `quant.run()` against the recorded Python result.
//!
//! The fixture snapshot (complete-window) is the exact input the legacy fixture used, so
//! the Rust result must match the recorded one field for field. Floats are compared with
//! a 1e-12 relative tolerance because `erf`/`ln` come from different libm builds.

use std::path::Path;

use sd_research::quant::run;
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
            assert_eq!(
                left.len(),
                right.len(),
                "{path}: object size differs (keys {left:?} vs {right:?})"
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
fn quant_run_matches_the_recorded_python_result() {
    let manifest = fixture("snapshot/complete-window/manifest.json");
    // `load_snapshot` injects the frozen gate verdict into the market block; the legacy
    // fixture was produced from the loaded snapshot, so mirror that here.
    let mut market = fixture("snapshot/complete-window/market.json");
    market["_data_validation"] = manifest["data_validation"].clone();
    let actual = run(&manifest, &market).expect("run");
    let expected = fixture("quant/synthetic-260/result.json");
    assert_json_close(&actual, &expected, "result");
    // High-value invariants pinned independently of the full-tree comparison.
    assert_eq!(actual["researcher_id"], "quant");
    assert_eq!(actual["result_role"], "forecast");
    assert_eq!(actual["status"], "success");
    assert_eq!(
        actual["probability_source"],
        "drift_shrunk_causal_base_rate"
    );
    let validation = &actual["validation"];
    assert_eq!(
        validation["probability_transform"]["version"],
        "drift_shrinkage_v2"
    );
    assert_eq!(validation["probability_transform"]["k"], 50);
    assert_eq!(
        validation["leakage_audit"]["future_returns_used_only_as_labels"],
        true
    );
    assert!(validation["oos_forecasts"].as_array().unwrap().len() > 100);
    // The frozen snapshot carries no benchmark block, so the relative-strength factor
    // is unavailable and the all-or-nothing rule yields no score (disclosed, not zero).
    assert_eq!(validation["momentum_20d"]["score_status"], "unavailable");
    assert!(validation["momentum_20d"]["momentum_score_20d"].is_null());
    assert_eq!(
        validation["momentum_20d"]["status_reasons"][0],
        "relative_strength:benchmark_unavailable"
    );
}
