//! Differential tests: the Rust request contract against the Python fixtures
//! frozen in R00.
//!
//! Every accepted fixture must normalize to **exactly** the Python-normalized
//! JSON (including key order, because readable JSON is an on-disk contract).
//! Every rejected fixture must fail with the same error class, the same policy code
//! when there is one, and a message that contains the legacy wording.

use std::path::{Path, PathBuf};

use sd_core::contracts::normalize_request;
use serde_json::Value;

fn fixtures_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn read_json(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

fn case_directories(group: &str) -> Vec<PathBuf> {
    let mut directories: Vec<PathBuf> = std::fs::read_dir(fixtures_root().join(group))
        .unwrap_or_else(|error| panic!("cannot list {group} fixtures: {error}"))
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_dir()
                && !path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .starts_with('_')
        })
        .collect();
    directories.sort();
    directories
}

#[test]
fn accepted_requests_match_the_python_normalization_exactly() {
    let mut compared = 0;
    for directory in case_directories("request") {
        let expected_path = directory.join("expected.json");
        if !expected_path.exists() {
            continue;
        }
        let input = read_json(&directory.join("input.json"));
        let expected = read_json(&expected_path);
        let normalized = normalize_request(&input)
            .unwrap_or_else(|error| panic!("{} must be accepted: {error}", directory.display()));
        assert_eq!(
            normalized.to_json(),
            expected,
            "normalized request differs from Python for {}",
            directory.display()
        );
        compared += 1;
    }
    assert_eq!(
        compared, 9,
        "every accepted request fixture must be compared"
    );
}

#[test]
fn rejected_requests_match_the_python_error_class_and_message() {
    let mut compared = 0;
    for directory in case_directories("request") {
        let expected_path = directory.join("expected-error.json");
        if !expected_path.exists() {
            continue;
        }
        let input = read_json(&directory.join("input.json"));
        let expected = read_json(&expected_path);
        let error = normalize_request(&input)
            .err()
            .unwrap_or_else(|| panic!("{} must be rejected", directory.display()));
        let expected_type = expected["error_type"].as_str().unwrap_or_default();
        assert_eq!(
            error.error_type(),
            expected_type,
            "error class differs for {}",
            directory.display()
        );
        if let Some(code) = expected["code"].as_str() {
            assert_eq!(
                error.code(),
                Some(code),
                "policy code differs for {}",
                directory.display()
            );
        }
        let expected_message = expected["message"].as_str().unwrap_or_default();
        assert_eq!(
            error.message(),
            expected_message,
            "error message differs for {}",
            directory.display()
        );
        compared += 1;
    }
    assert_eq!(
        compared, 14,
        "every rejected request fixture must be compared"
    );
}

#[test]
fn forecast_horizon_must_be_five_sessions() {
    for horizon in ["1D", "20D"] {
        let input = serde_json::json!({"ticker": "SYN", "horizon": horizon, "intent": "forecast"});
        let error = normalize_request(&input).expect_err("a forecast must reject a non-5D horizon");
        assert_eq!(error.error_type(), "PolicyError");
        assert_eq!(error.code(), Some("UNSUPPORTED_FORECAST_HORIZON"));
    }
    let accepted = normalize_request(
        &serde_json::json!({"ticker": "SYN", "horizon": "20D", "intent": "descriptive"}),
    )
    .expect("descriptive keeps range support");
    assert_eq!(accepted.horizon, "20D");
}

#[test]
fn descriptive_intent_keeps_every_horizon() {
    for horizon in ["1D", "5D", "20D"] {
        let input =
            serde_json::json!({"ticker": "MU", "intent": "descriptive", "horizon": horizon});
        let accepted = normalize_request(&input).expect("descriptive horizons are accepted");
        assert_eq!(accepted.horizon, horizon);
    }
}

#[test]
fn absent_null_false_and_zero_stay_distinct() {
    // `asof: null` and an absent asof both normalize to null, but `asof: ""` does too;
    // the request object always carries the key.
    let normalized =
        normalize_request(&serde_json::json!({"ticker": "MU"})).expect("minimal request");
    assert!(normalized
        .to_json()
        .as_object()
        .unwrap()
        .contains_key("asof"));
    assert_eq!(normalized.asof, None);

    // `intent: false` is not a string and must be rejected instead of coerced.
    let error = normalize_request(&serde_json::json!({"ticker": "MU", "intent": false}))
        .expect_err("a boolean intent is not a valid intent");
    assert_eq!(error.error_type(), "ValueError");

    // `history_start: ""` is not a date; the legacy contract rejects it.
    let error = normalize_request(&serde_json::json!({"ticker": "MU", "history_start": ""}))
        .expect_err("empty date");
    assert_eq!(error.error_type(), "ValueError");
}

#[test]
fn policy_block_is_only_attached_to_forecasts() {
    let forecast = normalize_request(&serde_json::json!({"ticker": "MU", "intent": "forecast"}))
        .expect("forecast");
    let policy = forecast.policy.expect("forecasts carry the policy block");
    assert_eq!(policy["policy_version"], "fixed_52w_5d_v1");
    assert_eq!(policy["estimator"]["k"], 50);
    assert_eq!(policy["estimator"]["mu_annual"], 0.06);

    let descriptive =
        normalize_request(&serde_json::json!({"ticker": "MU", "intent": "descriptive"}))
            .expect("descriptive");
    assert!(
        descriptive.policy.is_none(),
        "descriptive requests must not claim a forecast policy"
    );
}

#[test]
fn unknown_fields_are_rejected_with_a_sorted_list() {
    let error = normalize_request(&serde_json::json!({"ticker": "MU", "zeta": 1, "alpha": 2}))
        .expect_err("unknown fields are rejected");
    assert_eq!(error.message(), "unsupported request fields: alpha, zeta");
}
