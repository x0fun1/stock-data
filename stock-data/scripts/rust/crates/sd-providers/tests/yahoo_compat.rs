//! R15 differential: the compatibility fallback must reproduce legacy envelopes and errors.
//!
//! Fixtures are generated with a scripted gateway module and a fixed clock; the Rust test
//! drives the same scenarios through a [`LegacyGateway`] implementation.

use std::cell::RefCell;
use std::collections::BTreeMap;

use chrono::{TimeZone, Utc};
use serde_json::{Map, Value};

use sd_providers::yahoo_compat::{fallback, LegacyError, LegacyGateway, NoPolicy, ReservePolicy};

struct FakeGateway {
    responses: BTreeMap<String, Value>,
    calls: RefCell<usize>,
}

impl FakeGateway {
    fn new(responses: &Value) -> Self {
        Self {
            responses: responses
                .as_object()
                .map(|map| {
                    map.iter()
                        .map(|(key, value)| (key.clone(), value.clone()))
                        .collect()
                })
                .unwrap_or_default(),
            calls: RefCell::new(0),
        }
    }

    fn call(&self, name: &str) -> Result<Value, LegacyError> {
        *self.calls.borrow_mut() += 1;
        let Some(spec) = self.responses.get(name) else {
            return Ok(serde_json::json!({"ok": true}));
        };
        if let Some(raised) = spec.get("raise") {
            let exception_type = raised["type"]
                .as_str()
                .unwrap_or("RuntimeError")
                .to_string();
            return Err(LegacyError {
                code: None,
                exception_type,
                message: raised["message"].as_str().unwrap_or("").to_string(),
            });
        }
        Ok(spec.get("return").cloned().unwrap_or(Value::Null))
    }
}

impl LegacyGateway for FakeGateway {
    fn stock_kline_yahoo(
        &self,
        _symbol: &str,
        _interval: &str,
        _range: &str,
        _include_metadata: bool,
    ) -> Result<Value, LegacyError> {
        self.call("stock_kline_yahoo")
    }

    fn yahoo_quote_summary(
        &self,
        _symbol: &str,
        _modules: &[String],
    ) -> Result<Value, LegacyError> {
        self.call("yahoo_quote_summary")
    }

    fn key_statistics(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.call("key_statistics")
    }

    fn analyst_estimates(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.call("analyst_estimates")
    }

    fn institutional_holders(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.call("institutional_holders")
    }

    fn financial_statements_yahoo(
        &self,
        _symbol: &str,
        _quarterly: bool,
    ) -> Result<Value, LegacyError> {
        self.call("financial_statements_yahoo")
    }

    fn options_chain(&self, _symbol: &str, _expiration: Option<i64>) -> Result<Value, LegacyError> {
        self.call("options_chain")
    }

    fn stock_search(&self, _keyword: &str, _count: i64) -> Result<Value, LegacyError> {
        self.call("stock_search")
    }
}

struct FakePolicy {
    limit: usize,
    calls: usize,
}

impl ReservePolicy for FakePolicy {
    fn reserve(&mut self) -> Result<(), LegacyError> {
        self.calls += 1;
        if self.calls > self.limit {
            return Err(LegacyError::policy("BudgetExceeded", "budget exhausted"));
        }
        Ok(())
    }
}

fn fixture() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/yahoo/fallback_cases.json");
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

fn fixed_now() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap()
}

#[test]
fn compatibility_fallback_matches_legacy_envelopes() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "expected the fallback corpus");
    for (index, case) in cases.iter().enumerate() {
        let function = case["function"].as_str().expect("function");
        let params: Map<String, Value> = case["params"].as_object().cloned().unwrap_or_default();
        let reason = case["reason"].clone();
        let gateway = FakeGateway::new(&case["gateway"]);
        let limit = case["policy_limit"].as_u64().unwrap_or(99) as usize;
        let mut policy = FakePolicy { limit, calls: 0 };
        let circuit = case["circuit"].as_array().map(|items| {
            (
                items
                    .first()
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                items
                    .get(1)
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            )
        });
        let circuit_ref = circuit
            .as_ref()
            .map(|(code, message)| (code.as_str(), message.as_str()));
        let outcome = if limit > 0 {
            fallback(
                &gateway,
                function,
                &params,
                &reason,
                &mut policy,
                fixed_now(),
                circuit_ref,
            )
        } else {
            fallback(
                &gateway,
                function,
                &params,
                &reason,
                &mut NoPolicy,
                fixed_now(),
                circuit_ref,
            )
        };
        assert_eq!(
            policy.calls,
            case["policy_calls"].as_u64().unwrap_or(0) as usize,
            "case {index} {function}: policy calls"
        );
        if case["ok"].as_bool() == Some(true) {
            let envelope = outcome.unwrap_or_else(|error| {
                panic!("case {index} {function}: unexpected raise {error:?}")
            });
            assert!(
                close(&envelope, &case["data"]),
                "case {index} {function} mismatch\nexpected: {}\nactual: {}",
                case["data"],
                envelope
            );
        } else {
            let raised = outcome.expect_err(&format!("case {index} {function}: expected raise"));
            assert_eq!(
                raised.code,
                case["raised"]["code"].as_str().unwrap_or_default(),
                "case {index} {function} raise code"
            );
            assert_eq!(
                raised.message,
                case["raised"]["message"].as_str().unwrap_or_default(),
                "case {index} {function} raise message"
            );
        }
    }
}
