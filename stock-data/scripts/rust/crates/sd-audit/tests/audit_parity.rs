//! Differential parity for the adversarial audit (`audit.py`).
//!
//! The audit decides whether a direction may be published, so every veto/warning path
//! is compared exactly: reason codes (sorted), messages, checks block, eligibility flags
//! and the News attachment must all match the legacy implementation.

use std::path::Path;

use sd_audit::audit::{attach_news_observation, audit, AuditInput};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/audit/scenarios.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

#[test]
fn audit_scenarios_match_python() {
    let document = read();
    let cases = document.as_array().expect("cases");
    assert!(cases.len() >= 20, "the fixture must cover every veto path");
    for case in cases {
        let name = case["case"].as_str().expect("case");
        let input = &case["input"];
        let expected = &case["result"];
        let researchers = input["researchers"].as_array().expect("researchers");
        let prospective = input["prospective_records"]
            .as_array()
            .expect("prospective records");
        let news = input.get("news").filter(|value| !value.is_null());
        let actual = audit(&AuditInput {
            manifest: &input["manifest"],
            researchers,
            consensus: &input["consensus"],
            news,
            prospective_records: Some(prospective),
        });
        assert_eq!(&actual, expected, "audit result differs for {name}");
    }
    // A specific high-value veto is pinned by code, not only by the whole document.
    let approved = cases
        .iter()
        .find(|case| case["case"] == "approved-base")
        .expect("approved case");
    assert_eq!(approved["result"]["status"], "approved");
    assert_eq!(approved["result"]["may_report_probability"], true);
    assert_eq!(
        approved["result"]["reason_codes"],
        serde_json::json!(["DIRECTIONAL_EDGE_UNPROVEN"])
    );
    let unresolved = cases
        .iter()
        .find(|case| case["case"] == "target-unresolved")
        .expect("target case");
    assert_eq!(unresolved["result"]["vetoes"].as_array().unwrap().len(), 1);
    assert_eq!(unresolved["result"]["may_report_probability"], false);
}

#[test]
fn news_attachment_never_changes_reportability() {
    let document = read();
    let cases = document.as_array().unwrap();
    let approved = cases
        .iter()
        .find(|case| case["case"] == "approved-base")
        .unwrap();
    let mut result = approved["result"].clone();
    let before = result["may_report_probability"].clone();
    attach_news_observation(
        &mut result,
        Some(&serde_json::json!({"status": "unavailable", "warnings": []})),
    );
    assert_eq!(result["may_report_probability"], before);
    assert_eq!(result["status"], "approved");
    assert_eq!(result["checks"]["news_state"], "not_assessed");
    assert!(result["reason_codes"]
        .as_array()
        .unwrap()
        .iter()
        .any(|code| code == "NEWS_NOT_ASSESSED"));
    let partial = cases
        .iter()
        .find(|case| case["case"] == "news-partial")
        .unwrap();
    assert_eq!(partial["result"]["checks"]["news_warning_count"], 2);
    assert_eq!(partial["result"]["checks"]["news_state"], "partial");
    let legacy = cases
        .iter()
        .find(|case| case["case"] == "news-legacy-valid")
        .unwrap();
    assert_eq!(legacy["result"]["checks"]["news_state"], "complete");
}
