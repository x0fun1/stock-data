//! Parity for the Research B shadow factor path (`researchers/factor.py`).
//!
//! The fixture records the Python result on the frozen `complete-window` snapshot; the
//! Rust `run` receives the same loaded manifest/market (with the frozen gate verdict
//! injected into the market block, exactly as `load_snapshot` does). Floats use a 1e-12
//! relative tolerance because `ln`/`powf` come from different libm builds; every string,
//! integer, boolean, `None`, key order and array order is compared exactly.

use std::path::Path;

use sd_research::factor::run;
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
            // The legacy JSON is consumed positionally by downstream stages, so key
            // insertion order must match too, not just the key set.
            let left_keys: Vec<&String> = left.keys().collect();
            let right_keys: Vec<&String> = right.keys().collect();
            assert_eq!(left_keys, right_keys, "{path}: key order differs");
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
fn shadow_factor_path_matches_python() {
    let manifest = fixture("snapshot/complete-window/manifest.json");
    let mut market = fixture("snapshot/complete-window/market.json");
    // `load_snapshot` injects the frozen gate verdict into the market block; the Python
    // fixture was produced from the loaded snapshot, so mirror that here.
    market["_data_validation"] = manifest["data_validation"].clone();

    let actual = run(&manifest, &market).expect("factor run");
    let expected = fixture("factor/result.json");
    assert_json_close(&actual, &expected, "factor");
}

#[test]
fn shadow_contract_invariants_hold() {
    let manifest = fixture("snapshot/complete-window/manifest.json");
    let mut market = fixture("snapshot/complete-window/market.json");
    market["_data_validation"] = manifest["data_validation"].clone();
    let result = run(&manifest, &market).expect("factor run");
    let validation = &result["validation"];
    let route = &validation["qualification"];

    // B is a shadow candidate: never production-eligible, never a production path.
    assert_eq!(result["researcher_id"], "factor");
    assert_eq!(result["result_role"], "forecast");
    assert_eq!(route["production_eligible"], false);
    assert_eq!(route["shadow_only"], true);
    assert_eq!(route["route"], "shadow_candidate");
    assert_eq!(route["route_reason"], "post_selection_bucket_budget_met");
    assert_eq!(route["checks"]["minimum_per_bucket"], 6);

    // Qualification is the authority: a numeric probability is reportable only when the
    // route says shadow_candidate, and then the source label is explicit.
    assert!(result["prob_up"].is_number());
    assert_eq!(
        result["probability_source"],
        "out_of_sample_factor_score_bucket_frequency"
    );
    assert_eq!(route["legacy_probability_available"], true);
    assert!(validation["unqualified_estimate"].is_null());

    // Selection is restricted: dependency-aware metric, 3 factors, max one per family.
    let selection = &validation["selection"];
    assert_eq!(selection["metric"], "overlapping_block");
    assert_eq!(selection["profile"], "restricted");
    assert_eq!(selection["max_factors"], 3);
    assert_eq!(selection["max_factors_per_family"], 1);
    assert_eq!(selection["overlap_treated_as_dependence"], true);
    let rejected: Vec<(&str, &str)> = selection["rejected"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row["factor"].as_str().unwrap(),
                row["reason"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        rejected,
        vec![
            ("overnight_gap", "family_limit"),
            ("momentum_5", "family_limit"),
        ]
    );
    let selected: Vec<&str> = validation["selected_factors"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["factor_name"].as_str().unwrap())
        .collect();
    assert_eq!(
        selected,
        vec!["momentum_60", "reversal_1", "range_position_20"]
    );

    // Evidence groups the selected factors by the sign of their current contribution.
    let positive = result["evidence"]["positive"].as_array().unwrap();
    let negative = result["evidence"]["negative"].as_array().unwrap();
    assert_eq!(positive.len(), 1);
    assert_eq!(negative.len(), 2);
    assert_eq!(result["status"], "partial");
    for item in positive {
        assert!(item["current_contribution"].as_f64().unwrap() >= 0.0);
        assert!(item["family"].is_string());
        assert!(item["factor_name"].is_string());
        assert!(item["formula"].is_string());
        assert!(item["training_rank_ic"].is_number());
        assert!(item["training_direction"].is_string());
    }
    for item in negative {
        assert!(item["current_contribution"].as_f64().unwrap() < 0.0);
    }

    // The bucket used for the reported probability is the honest post-selection candidate.
    assert_eq!(
        validation["honest_calibration_candidate"]["used_for_reported_probability"],
        true
    );
    assert_eq!(
        validation["honest_calibration_candidate"]["status"],
        "candidate_available"
    );
    assert_eq!(validation["bucket_mode"], "two");
    assert_eq!(validation["selection_profile"], "restricted");
    assert_eq!(
        validation["leakage_audit"]["oos_inference_did_not_select_or_modify_probability"],
        true
    );

    // Unsupported options are refused rather than silently defaulted.
    assert!(sd_research::factor::run_with_options(
        &manifest,
        &market,
        &sd_research::factor::FactorOptions {
            bucket_mode: "four",
            ..Default::default()
        }
    )
    .is_err());
}
