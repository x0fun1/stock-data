//! R15 differential: the seven legacy Yahoo routes must match the legacy requests/output.
//!
//! Fixtures are produced offline with a scripted transport (`fixtures_yahoo.py`). Each case
//! records the exact requests (URL/params/headers/timeout), the scripted responses and the
//! produced payload or error; the Rust side replays them through `MockTransport`.

use std::collections::BTreeMap;

use chrono::Utc;
use serde_json::Value;

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::JobContext;
use sd_providers::yahoo_legacy;
use sd_providers::yahoo_session::{get_yahoo_session, YahooSession};

fn fixture() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/yahoo/legacy_cases.json");
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

fn session_for(case: &Value) -> YahooSession {
    YahooSession {
        crumb: case["crumb"].as_str().unwrap_or("stub-crumb").to_string(),
        headers: map_of(&case["session_headers"]),
        cookies: BTreeMap::new(),
    }
}

fn run_case(
    function: &str,
    case: &Value,
    transport: &MockTransport,
    context: &mut JobContext,
) -> Result<Value, (String, String)> {
    let session = session_for(case);
    let params = case["params"].as_object().cloned().unwrap_or_default();
    let arg = |name: &str| params.get(name);
    let map_error = |error: sd_providers::transport::ProviderError| {
        (error.kind.error_type().to_string(), error.message)
    };
    match function {
        "__get_yahoo_session__" => get_yahoo_session(transport, context)
            .map(|session| serde_json::json!({"crumb": session.crumb}))
            .map_err(map_error),
        "stock_kline_yahoo" => yahoo_legacy::stock_kline_yahoo(
            arg("symbol"),
            arg("interval"),
            arg("range_"),
            arg("include_metadata")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            transport,
            context,
        )
        .map_err(map_error),
        "yahoo_quote_summary" => yahoo_legacy::yahoo_quote_summary(
            arg("symbol"),
            arg("modules"),
            &session,
            transport,
            context,
        )
        .map_err(map_error),
        "key_statistics" => {
            yahoo_legacy::key_statistics(arg("symbol"), &session, transport, context)
                .map_err(map_error)
        }
        "analyst_estimates" => {
            yahoo_legacy::analyst_estimates(arg("symbol"), &session, transport, context)
                .map_err(map_error)
        }
        "institutional_holders" => {
            yahoo_legacy::institutional_holders(arg("symbol"), &session, transport, context)
                .map_err(map_error)
        }
        "financial_statements_yahoo" => yahoo_legacy::financial_statements_yahoo(
            arg("symbol"),
            arg("quarterly"),
            &session,
            transport,
            context,
        )
        .map_err(map_error),
        "options_chain" => yahoo_legacy::options_chain(
            arg("symbol"),
            arg("expiration"),
            &session,
            transport,
            context,
        )
        .map_err(map_error),
        other => panic!("unknown function {other}"),
    }
}

#[test]
fn legacy_yahoo_routes_match_legacy_requests_and_output() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "expected the legacy Yahoo corpus");
    for (index, case) in cases.iter().enumerate() {
        let function = case["function"].as_str().expect("function");
        let responses: Vec<HttpResponse> = case["responses"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .map(|item| {
                        let mut headers = BTreeMap::new();
                        if let Some(cookies) = item.get("set_cookie").and_then(Value::as_object) {
                            for (name, value) in cookies {
                                headers.insert(
                                    "set-cookie".to_string(),
                                    format!("{name}={}", value.as_str().unwrap_or("")),
                                );
                            }
                        }
                        HttpResponse {
                            status: item["status"].as_u64().unwrap_or(200) as u16,
                            reason: item["reason"].as_str().unwrap_or("OK").to_string(),
                            body: item["body"]
                                .as_str()
                                .unwrap_or_default()
                                .as_bytes()
                                .to_vec(),
                            headers,
                            final_url: String::new(),
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        let transport = MockTransport::new(responses);
        let mut context = JobContext::default();
        let outcome = run_case(function, case, &transport, &mut context);
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
            assert_eq!(
                actual.headers,
                map_of(&expected["headers"]) as BTreeMap<String, String>,
                "case {index} {function} request {request_index} headers"
            );
            assert_eq!(
                actual.timeout,
                expected["timeout"].as_f64().unwrap_or(30.0),
                "case {index} {function} request {request_index} timeout"
            );
        }
        if case["ok"].as_bool() == Some(true) {
            let actual = outcome.unwrap_or_else(|error| {
                panic!("case {index} {function}: unexpected error {error:?}")
            });
            assert!(
                close(&actual, &case["data"]),
                "case {index} {function} mismatch\nexpected: {}\nactual: {}",
                case["data"],
                actual
            );
        } else {
            let (error_type, message) =
                outcome.expect_err(&format!("case {index} {function}: expected failure"));
            assert_eq!(
                error_type,
                case["error_type"].as_str().unwrap_or_default(),
                "case {index} {function} error type"
            );
            assert_eq!(
                message,
                case["error_message"].as_str().unwrap_or_default(),
                "case {index} {function} error message"
            );
        }
        let _ = Utc::now();
    }
}
