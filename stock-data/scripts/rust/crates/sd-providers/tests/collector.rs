//! R16 differential: the native collector must reproduce the legacy `collect()` output.

use std::collections::BTreeMap;

use chrono::{TimeZone, Utc};
use serde_json::{Map, Value};

use sd_providers::collector::{collect, CollectProvider};
use sd_providers::transport::ProviderError;

struct FakeProvider {
    responses: Vec<Value>,
    calls: Vec<Value>,
}

impl FakeProvider {
    fn new(responses: &Value) -> Self {
        Self {
            responses: responses.as_array().cloned().unwrap_or_default(),
            calls: Vec::new(),
        }
    }
}

impl CollectProvider for FakeProvider {
    fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, ProviderError> {
        self.calls.push(serde_json::json!({
            "name": name,
            "params": Value::Object(params.clone()),
        }));
        Ok(self.responses.remove(0))
    }
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
fn collector_matches_legacy() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/collector/cases.json");
    let fixture: Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json");
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 12, "expected the collector corpus");
    for (index, case) in cases.iter().enumerate() {
        let request = case["request"].clone();
        let domains: Vec<String> = case["domains"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default();
        let now = case["now"]
            .as_str()
            .and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok())
            .map(|value| value.with_timezone(&Utc))
            .unwrap_or_else(|| Utc.with_ymd_and_hms(2026, 3, 2, 22, 0, 0).unwrap());
        let schedule = case
            .get("schedule")
            .filter(|value| !value.is_null())
            .cloned();
        let mut provider = FakeProvider::new(&case["responses"]);
        let timeout = case["timeout"].as_f64().unwrap_or(15.0);
        let deadline = case["deadline"].as_f64().unwrap_or(120.0);
        let budget = case["request_budget"].as_u64().unwrap_or(30) as u32;
        let outcome = collect(
            &request,
            &domains,
            &mut provider,
            schedule.as_ref(),
            Some(now),
            timeout,
            deadline,
            budget,
        );
        if case["ok"].as_bool() == Some(true) {
            let actual =
                outcome.unwrap_or_else(|error| panic!("case {index}: unexpected error {error}"));
            assert!(
                close(&actual, &case["data"]),
                "case {index} mismatch\nexpected: {}\nactual: {}",
                case["data"],
                actual
            );
        } else {
            let error = outcome.expect_err(&format!("case {index}: expected failure"));
            assert_eq!(
                error,
                case["error_message"].as_str().unwrap_or_default(),
                "case {index} error message"
            );
        }
        let expected_calls = case["calls"].as_array().cloned().unwrap_or_default();
        assert_eq!(
            provider.calls.len(),
            expected_calls.len(),
            "case {index}: provider call count"
        );
        for (call_index, (actual, expected)) in
            provider.calls.iter().zip(expected_calls.iter()).enumerate()
        {
            assert_eq!(
                actual["name"], expected["name"],
                "case {index} call {call_index} name"
            );
            assert_eq!(
                map_of(&actual["params"]),
                map_of(&expected["params"]),
                "case {index} call {call_index} params"
            );
        }
    }
}
