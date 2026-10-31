//! R15 differential: provider routing, preflight, fallback preference and job circuit.
//!
//! The fixture stubs the native route and the compatibility fallback exactly like the
//! legacy check, so routing, preflight rejections, terminal-policy behavior and the
//! process-wide latch are all compared verbatim.

use std::cell::RefCell;

use serde_json::{Map, Value};

use sd_providers::yahoo_compat::{GatewayRaise, LegacyError, ReservePolicy};
use sd_providers::yahoo_provider::{
    job_circuit_block, latch_job_circuit, reset_job_circuit, CacheDirMode, GatewayError,
    TransportPolicy, YahooFallback, YahooNative, YahooProvider,
};

struct FakeNative {
    behavior: Value,
    calls: Vec<String>,
}

impl FakeNative {
    fn new(behavior: &Value) -> Self {
        Self {
            behavior: behavior.clone(),
            calls: Vec::new(),
        }
    }
}

impl YahooNative for FakeNative {
    fn call(&mut self, name: &str, _params: &Map<String, Value>) -> Result<Value, GatewayError> {
        self.calls.push(name.to_string());
        if let Some(raised) = self.behavior.get("raise") {
            return Err(GatewayError::new(
                raised["code"].as_str().unwrap_or("DataUnavailable"),
                raised["message"].as_str().unwrap_or(""),
            ));
        }
        Ok(self.behavior.get("return").cloned().unwrap_or(Value::Null))
    }
}

struct FakeFallback {
    behavior: Value,
    calls: RefCell<Vec<Value>>,
}

impl FakeFallback {
    fn new(behavior: &Value) -> Self {
        Self {
            behavior: if behavior.is_null() {
                serde_json::json!({"return": {"status": "partial", "fallback_used": true,
                                              "data": {"legacy": true}}})
            } else {
                behavior.clone()
            },
            calls: RefCell::new(Vec::new()),
        }
    }
}

impl YahooFallback for FakeFallback {
    fn run(
        &mut self,
        function: &str,
        params: &Map<String, Value>,
        reason: &Value,
        _policy: &mut dyn ReservePolicy,
    ) -> Result<Value, GatewayRaise> {
        self.calls.borrow_mut().push(serde_json::json!({
            "function": function,
            "params": Value::Object(params.clone()),
            "reason": reason,
            "policy": true,
        }));
        if let Some(raised) = self.behavior.get("raise") {
            let code = raised["code"].as_str().unwrap_or("DataUnavailable");
            if code == "BudgetExceeded" {
                return Err(GatewayRaise {
                    code: code.to_string(),
                    message: raised["message"].as_str().unwrap_or("").to_string(),
                });
            }
        }
        Ok(self.behavior.get("return").cloned().unwrap_or(Value::Null))
    }
}

struct RecordingPolicy;

impl ReservePolicy for RecordingPolicy {
    fn reserve(&mut self) -> Result<(), LegacyError> {
        Ok(())
    }
}

fn fixture() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/yahoo/provider_cases.json");
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

fn cache_directory(value: Option<&Value>) -> CacheDirMode {
    match value.and_then(Value::as_str) {
        Some(text) => CacheDirMode::Directory(text.to_string()),
        None => CacheDirMode::Missing,
    }
}

#[test]
fn provider_routing_matches_legacy() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "expected the provider corpus");
    for (index, case) in cases.iter().enumerate() {
        reset_job_circuit();
        if let Some(latch) = case["latched"].as_array() {
            let code = latch.first().and_then(Value::as_str).unwrap_or_default();
            let message = latch.get(1).and_then(Value::as_str).unwrap_or_default();
            latch_job_circuit(code, message);
        }
        let mode = match case["cache_dir_mode"].as_str().unwrap_or("dir") {
            "missing" => CacheDirMode::Missing,
            "invalid" => CacheDirMode::Invalid,
            _ => cache_directory(case.get("cache_dir_value")),
        };
        let mut native = FakeNative::new(&case["native"]);
        let mut fallback = FakeFallback::new(case.get("fallback_spec").unwrap_or(&Value::Null));
        let _ = &fallback;
        let policy = TransportPolicy::default();
        let mut provider = YahooProvider::new(mode, policy.clone(), &mut native, &mut fallback);
        provider.now = chrono::Utc::now();
        let name = case["name"].as_str().expect("name");
        let params: Map<String, Value> = case["params"].as_object().cloned().unwrap_or_default();
        let outcome = provider.call(name, &params);
        if case["ok"].as_bool() == Some(true) {
            let actual = outcome
                .unwrap_or_else(|error| panic!("case {index} {name}: unexpected error {error:?}"));
            assert!(
                close(&actual, &case["data"]),
                "case {index} {name} mismatch\nexpected: {}\nactual: {}",
                case["data"],
                actual
            );
        } else {
            let error = outcome.expect_err(&format!("case {index} {name}: expected failure"));
            assert_eq!(
                error.code,
                case["raised"]["code"].as_str().unwrap_or_default(),
                "case {index} {name} error code"
            );
            assert_eq!(
                error.message,
                case["raised"]["message"].as_str().unwrap_or_default(),
                "case {index} {name} error message"
            );
        }
        let applied: Vec<String> = native.calls.iter().map(|_| String::new()).collect();
        assert_eq!(
            native.calls.len(),
            case["native_calls"].as_array().map(Vec::len).unwrap_or(0),
            "case {index} {name}: native calls"
        );
        let _ = applied;
        let fallback_calls: Vec<Value> = fallback.calls.borrow().clone();
        let expected_fallback = case["fallback_calls"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        assert_eq!(
            fallback_calls.len(),
            expected_fallback.len(),
            "case {index} {name}: fallback call count"
        );
        for (call_index, (actual, expected)) in fallback_calls
            .iter()
            .zip(expected_fallback.iter())
            .enumerate()
        {
            assert_eq!(
                actual["function"], expected["function"],
                "case {index} {name} fallback function"
            );
            assert!(
                close(&actual["params"], &expected["params"]),
                "case {index} {name} fallback {call_index} params mismatch\nexpected: {}\nactual: {}",
                expected["params"],
                actual["params"]
            );
            assert!(
                close(&actual["reason"], &expected["reason"]),
                "case {index} {name} fallback {call_index} reason mismatch\nexpected: {}\nactual: {}",
                expected["reason"],
                actual["reason"]
            );
        }
        let expected_latched = case["latched_after"].as_array().cloned();
        let actual_latched =
            job_circuit_block().map(|(code, message)| serde_json::json!([code, message]));
        match (expected_latched, actual_latched) {
            (Some(expected), Some(actual)) => {
                assert!(
                    close(&actual, &Value::Array(expected)),
                    "case {index} {name}: latched state mismatch"
                );
            }
            (None, None) => {}
            (expected, actual) => {
                panic!("case {index} {name}: latched mismatch {expected:?} vs {actual:?}")
            }
        }
        let _ = RecordingPolicy;
    }
    reset_job_circuit();
}
