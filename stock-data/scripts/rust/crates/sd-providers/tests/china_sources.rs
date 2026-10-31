//! R13 differential: the Rust China adapters must reproduce the legacy requests/output.
//!
//! The fixture is produced offline by `fixtures_gateway_sources.py`: every case records the
//! exact request the legacy function made and a scripted response. The Rust side replays
//! the same script through `MockTransport`, then compares the request list and the payload
//! tree (floats 1e-12, everything else exactly).

use std::collections::BTreeMap;

use serde_json::Value;

use sd_providers::china;
use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::JobContext;

fn fixture() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/gateway-sources/r13_cases.json");
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
fn china_sources_match_the_legacy_requests_and_results() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 30, "expected the full R13 corpus");
    for (index, case) in cases.iter().enumerate() {
        let function = case["function"].as_str().expect("function");
        let params = case["params"].as_object().cloned().unwrap_or_default();
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
        let outcome = china::call(function, &params, &transport, &mut context);

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
                "case {index} request {request_index} url"
            );
            let actual_params: BTreeMap<String, String> = actual.params.iter().cloned().collect();
            assert_eq!(
                actual_params,
                map_of(&expected["params"]),
                "case {index} request {request_index} params"
            );
            let actual_headers: BTreeMap<String, String> = actual
                .headers
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            assert_eq!(
                actual_headers,
                map_of(&expected["headers"]),
                "case {index} request {request_index} headers"
            );
            assert_eq!(
                actual.timeout,
                expected["timeout"].as_f64().unwrap_or(30.0),
                "case {index} request {request_index} timeout"
            );
        }
        if case["ok"].as_bool() == Some(true) {
            let actual = outcome.expect("expected success").clone();
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
