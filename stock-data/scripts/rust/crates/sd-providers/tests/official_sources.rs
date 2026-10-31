//! R14 differential: official-source adapters must reproduce legacy requests/output.
//!
//! `fixtures_gateway_sources.py` records each legacy request (with SEC_CONTACT /
//! CBOE_AUTHORIZED applied) and a scripted response, with the clock pinned to
//! 2026-01-09T15:00:00Z. The Rust side replays the script through `MockTransport` and
//! compares the request list and payload/error byte-for-byte (floats 1e-12).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde_json::Value;

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::official::ProviderEnv;
use sd_providers::official_sources;
use sd_providers::transport::JobContext;

fn fixture() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/gateway-sources/r14_cases.json");
    serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json")
}

fn close(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => match (left.as_f64(), right.as_f64()) {
            (Some(left), Some(right)) => {
                (left - right).abs() <= 1e-12 * left.abs().max(right.abs()).max(1.0)
            }
            _ => left == right,
        },
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(l, r)| close(l, r))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .map(|other| close(value, other))
                        .unwrap_or(false)
                })
        }
        _ => left == right,
    }
}

fn map_of(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .map(|map| {
            map.iter()
                .map(|(key, item)| {
                    let text = match item {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    };
                    (key.clone(), text)
                })
                .collect()
        })
        .unwrap_or_default()
}

#[test]
fn official_sources_match_the_legacy_requests_and_results() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 30, "expected the full R14 corpus");
    for (index, case) in cases.iter().enumerate() {
        let function = case["function"].as_str().expect("function");
        let params = case["params"].as_object().cloned().unwrap_or_default();
        let env_value = case["env"].as_object().cloned().unwrap_or_default();
        let env = ProviderEnv {
            sec_contact: env_value
                .get("SEC_CONTACT")
                .and_then(Value::as_str)
                .map(ToString::to_string),
            cboe_authorized: env_value.get("CBOE_AUTHORIZED").and_then(Value::as_str) == Some("1"),
        };
        let now: DateTime<Utc> = case["now"]
            .as_str()
            .and_then(|text| DateTime::parse_from_rfc3339(text).ok())
            .map(|value| value.with_timezone(&Utc))
            .expect("fixed clock");
        let responses: Vec<HttpResponse> = case["responses"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .map(|item| {
                        use base64::Engine;
                        let body = base64::engine::general_purpose::STANDARD
                            .decode(item["body_b64"].as_str().unwrap_or_default())
                            .expect("base64 body");
                        HttpResponse {
                            status: item["status"].as_u64().unwrap_or(200) as u16,
                            reason: item["reason"].as_str().unwrap_or("OK").to_string(),
                            body,
                            headers: BTreeMap::new(),
                            final_url: String::new(),
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        let transport = MockTransport::new(responses);
        let mut context = JobContext::default();
        let outcome =
            official_sources::call(function, &params, &env, now, &transport, &mut context);

        let expected_requests = case["requests"].as_array().expect("requests");
        let actual_requests = transport.requests();
        assert_eq!(
            actual_requests.len(),
            expected_requests.len(),
            "case {index} {function}: request count"
        );
        for (request_index, (actual, expected)) in
            actual_requests.iter().zip(expected_requests).enumerate()
        {
            assert_eq!(
                actual.url,
                expected["url"].as_str().unwrap_or_default(),
                "case {index} {function} request {request_index} url"
            );
            let actual_params: BTreeMap<String, String> = actual.params.iter().cloned().collect();
            assert_eq!(
                actual_params,
                map_of(&expected["params"]),
                "case {index} {function} request {request_index} params"
            );
            let actual_headers: BTreeMap<String, String> = actual.headers.clone();
            assert_eq!(
                actual_headers,
                map_of(&expected["headers"]),
                "case {index} {function} request {request_index} headers"
            );
            assert_eq!(
                actual.timeout,
                expected["timeout"].as_f64().unwrap_or(30.0),
                "case {index} {function} request {request_index} timeout"
            );
        }
        if case["ok"].as_bool() == Some(true) {
            let actual = outcome
                .unwrap_or_else(|error| panic!("case {index} {function}: {}", error.message));
            assert!(
                close(&actual, &case["data"]),
                "case {index} {function} data mismatch\nexpected: {}\nactual: {}",
                case["data"],
                actual
            );
        } else {
            let error = outcome.expect_err(&format!("case {index} {function}: expected failure"));
            assert_eq!(
                error.kind.error_type(),
                case["error_type"].as_str().unwrap_or_default(),
                "case {index} {function} error type"
            );
            assert_eq!(
                error.message,
                case["error_message"].as_str().unwrap_or_default(),
                "case {index} {function} error message"
            );
        }
    }
}
