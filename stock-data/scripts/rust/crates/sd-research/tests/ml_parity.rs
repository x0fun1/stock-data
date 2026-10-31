//! Parity for the Research C shadow ML path (`researchers/ml.py`).
//!
//! The frozen complete-window snapshot is the exact input the Python reference used. Every
//! structural/string/boolean leaf must match exactly; floats use a 1e-12 relative tolerance
//! because `exp`/`ln` come from separate libm builds. Two wall-clock leaves are inherently
//! nondeterministic and are checked for type/non-negativity instead of equality.

use std::path::Path;

use sd_research::ml::run;
use serde_json::Value;

/// Wall-clock measurements cannot match across processes; they are type-checked instead.
const WALL_CLOCK_PATHS: [&str; 2] = [
    "validation/model_training_performance/wall_seconds",
    "validation/model_training_performance/replay_wall_seconds",
];

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
    let normalized = path.trim_start_matches('/');
    if WALL_CLOCK_PATHS.contains(&normalized) {
        let value = actual
            .as_f64()
            .unwrap_or_else(|| panic!("{path}: expected a number, got {actual}"));
        assert!(
            value >= 0.0,
            "{path}: wall clock must be non-negative, got {value}"
        );
        return;
    }
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
                "{path}: object size differs (actual keys {left:?} vs expected {right:?})"
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
fn ml_shadow_result_matches_python() {
    let manifest = fixture("snapshot/complete-window/manifest.json");
    let mut market = fixture("snapshot/complete-window/market.json");
    market["_data_validation"] = manifest["data_validation"].clone();
    let actual = run(&manifest, &market).expect("run");
    let expected = fixture("ml/result.json");
    assert_json_close(&actual, &expected, "");

    // Pinned invariants: Research C is a shadow diagnostic, never production. The legacy
    // envelope role is `forecast` (shadow semantics live in model_selection/calibration),
    // exactly as Python emits it.
    assert_eq!(actual["researcher_id"], "ml");
    assert_eq!(actual["result_role"], "forecast");
    assert_eq!(
        actual["validation"]["model_selection"]["boosted_models_enabled"],
        false
    );
    assert_eq!(actual["status"], "success");
    assert_eq!(
        actual["probability_source"],
        "logistic_predict_proba_uncalibrated"
    );
    assert!(actual["calibrated_probability"].is_null());
    let validation = &actual["validation"];
    assert_eq!(
        validation["leakage_audit"]["calibration_disabled_by_policy"],
        true
    );
    assert_eq!(
        validation["leakage_audit"]["training_labels_matured_by_origin"],
        true
    );
    assert_eq!(
        validation["probability_calibration"]["status"],
        "disabled_by_policy"
    );
    assert_eq!(
        validation["current_prediction_calibration"]["status"],
        "disabled_by_policy"
    );
    assert_eq!(
        validation["current_prediction_calibration"]["applied"],
        false
    );
    assert_eq!(
        validation["model_hyperparameters"]["version"],
        "ml_params_v3"
    );
    assert_eq!(validation["model_hyperparameters"]["iterations"], 60);
    assert_eq!(validation["model_hyperparameters"]["l2"], 1.5);
    // Every replay row is an uncalibrated shadow probability; Platt is disclosure only.
    let rows = validation["oos_forecasts"].as_array().expect("oos rows");
    assert!(!rows.is_empty());
    assert!(rows.iter().all(|row| row["calibration_applied"] == false));
    assert!(rows
        .iter()
        .all(|row| row["calibration_status"] == "disabled_by_policy"));
    assert!(rows
        .iter()
        .all(|row| row["label_end_index"].as_i64() > row["decision_index"].as_i64()));
}
