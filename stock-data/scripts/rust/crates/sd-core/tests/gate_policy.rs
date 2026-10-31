//! Differential tests for the gate policy.
//!
//! The Python catalog, classification order and reporting/analysis derivations are
//! frozen in `tests/fixtures/gate/`. A drift in severity, scope, veto power or
//! failure mode changes whether a probability may be published, so every catalog
//! entry and a set of combinations is compared rather than a smoke case.

use std::path::{Path, PathBuf};

use sd_core::gate_policy::{
    aggregate_gate_telemetry, analysis_status, build_report_eligibility, classify_reasons,
    reason_policy, reason_policy_value, reporting_status, REASON_CATALOG,
};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/gate")
}

fn read(name: &str) -> Value {
    let path = fixtures().join(name);
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("string array")
        .iter()
        .map(|item| item.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn every_catalog_code_matches_python_reason_policy() {
    let rows = read("reason-policy.json");
    let rows = rows.as_array().expect("catalog rows");
    assert_eq!(
        rows.len(),
        REASON_CATALOG.len() + 1,
        "catalog plus the unknown-code probe"
    );
    for row in rows {
        let code = row["code"].as_str().unwrap();
        assert_eq!(
            reason_policy_value(code),
            row["policy"],
            "reason policy differs for {code}"
        );
    }
    // The catalog count is a contract: a dropped entry would silently stop vetoing.
    assert_eq!(REASON_CATALOG.len(), 80);
    assert!(reason_policy("TARGET_SESSION_UNRESOLVED").can_veto_prediction);
    assert_eq!(
        reason_policy("TARGET_SESSION_UNRESOLVED").failure_mode,
        "correctness"
    );
    assert_eq!(
        reason_policy("SAMPLING_PHASE_UNANCHORED").failure_mode,
        "accounting"
    );
    assert_eq!(
        reason_policy("NEWS_PARTIAL_EVIDENCE").failure_mode,
        "reporting"
    );
    // Unknown codes fail closed.
    let unknown = reason_policy("NOT_A_CATALOG_CODE");
    assert_eq!(unknown.severity, "fatal");
    assert!(unknown.can_veto_prediction);
}

#[test]
fn classification_order_and_status_match_python() {
    let rows = read("classify.json");
    for row in rows.as_array().expect("classify rows") {
        let codes = strings(&row["codes"]);
        let classification = classify_reasons(codes.clone());
        assert_eq!(
            classification.to_value(),
            row["result"],
            "classification differs for {codes:?}"
        );

        let veto_codes: Vec<String> = codes
            .iter()
            .filter(|code| reason_policy(code).can_veto_prediction)
            .cloned()
            .collect();
        let warning_codes: Vec<String> = codes
            .iter()
            .filter(|code| !reason_policy(code).can_veto_prediction)
            .cloned()
            .collect();
        let status = reporting_status(veto_codes.is_empty(), &veto_codes, &warning_codes);
        assert_eq!(
            status,
            row["reporting_status"].as_str().unwrap(),
            "reporting status differs for {codes:?}"
        );
    }

    // Duplicates keep the first occurrence and never double-count.
    let classification = classify_reasons([
        "SEVERE_MISSING_BARS",
        "CALENDAR_UNVERIFIED",
        "SEVERE_MISSING_BARS",
    ]);
    assert_eq!(
        classification.reason_codes,
        vec!["SEVERE_MISSING_BARS", "CALENDAR_UNVERIFIED"]
    );
    assert_eq!(classification.status, "veto");
    // Strategy-only warnings cannot degrade the reporting state.
    let strategy_only = classify_reasons(["PBO_NOT_ASSESSED", "SURVIVORSHIP_NOT_ASSESSED"]);
    assert_eq!(strategy_only.status, "pass");
    assert!(!strategy_only.warnings.is_empty());
    // Informational codes stay informational, not warnings.
    let informational = classify_reasons(["LATEST_CLOSE_CROSS_VERIFIED"]);
    assert_eq!(informational.status, "pass");
    assert_eq!(
        informational.informational,
        vec!["LATEST_CLOSE_CROSS_VERIFIED"]
    );
}

#[test]
fn analysis_status_is_derived_from_stages_only() {
    let rows = read("analysis-status.json");
    for row in rows.as_array().expect("analysis rows") {
        let statuses = strings(&row["researcher_statuses"]);
        let result = analysis_status(
            row["may_report_direction"].as_bool().unwrap(),
            row["reporting_state"].as_str().unwrap(),
            &statuses,
            row["news_status"].as_str().unwrap(),
        );
        assert_eq!(
            result,
            row["result"].as_str().unwrap(),
            "analysis_status differs for {row}"
        );
    }
    // A dominated path or incomplete news can never be reported as complete.
    assert_eq!(
        analysis_status(true, "pass", &["failed".to_string()], "complete"),
        "degraded"
    );
    assert_eq!(
        analysis_status(true, "pass", &["success".to_string()], "partial"),
        "degraded"
    );
    assert_eq!(
        analysis_status(false, "pass", &["success".to_string()], "complete"),
        "abstain"
    );
}

#[test]
fn report_eligibility_separates_probability_direction_and_signal() {
    let rows = read("report-eligibility.json");
    for row in rows.as_array().expect("eligibility rows") {
        let result = build_report_eligibility(&row["consensus"], &row["adversarial"]);
        assert_eq!(
            result, row["result"],
            "report eligibility differs for {}",
            row["consensus"]
        );
        assert_eq!(result["trading_signal"], false);
    }
    // A numeric probability with no independent edge is a replay-only grade.
    let replay = build_report_eligibility(
        &serde_json::json!({"reportable_probability": 0.6,
                            "directional_edge_state": "positive_skill_with_estimated_interval_excluding_zero",
                            "directional_edge_supported": false}),
        &serde_json::json!({"may_report_direction": true}),
    );
    assert_eq!(
        replay["predictive_evidence_grade"],
        "replay_only_positive_skill_with_estimated_interval_excluding_zero"
    );
    assert_eq!(replay["independent_edge_supported"], false);
}

#[test]
fn telemetry_rates_match_python_rounding() {
    let rows = read("telemetry.json");
    for row in rows.as_array().expect("telemetry rows") {
        let records = row["rows"].as_array().expect("records");
        let result = aggregate_gate_telemetry(records);
        assert_eq!(result, row["result"], "telemetry differs for {records:?}");
    }
    // 3/8 = 0.375 stays exact at four decimals.
    let records = vec![
        serde_json::json!({"prediction_available": true, "reporting_status": "pass"}),
        serde_json::json!({"prediction_available": true, "reporting_status": "pass"}),
        serde_json::json!({"prediction_available": true, "reporting_status": "degraded"}),
        serde_json::json!({"prediction_available": false, "reporting_status": "veto"}),
    ];
    let result = aggregate_gate_telemetry(&records);
    assert_eq!(result["sample_count"], 4);
    assert_eq!(result["prediction_availability_rate"], 0.75);
    assert_eq!(result["abstain_rate"], 0.25);
}
