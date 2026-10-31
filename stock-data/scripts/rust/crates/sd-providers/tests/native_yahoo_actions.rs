//! Native/fallback Yahoo history must carry chart dividend, split and capital-gain events
//! as per-row yfinance action columns.
//!
//! The wire bodies are minimal excerpts of the frozen A-PARITY-01 fixtures
//! (`audit/post-acceptance/A-PARITY-01/fixtures/us_daily_1y/main/response.bin` and
//! `hk_daily_1y/main/response.bin`): the same `chart.result[0]` shape with `meta`,
//! `timestamp`, `indicators` and `events`. They are inlined because the frozen corpus
//! lives outside the delivered Skill; the parity harness compares against the full bytes.

use chrono::{TimeZone, Utc};
use serde_json::{json, Map, Value};

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::{JobContext, JobState};
use sd_providers::yahoo_compat::{legacy_history, LegacyError, LegacyGateway, NoPolicy};
use sd_providers::yahoo_gateway;

fn chart_body() -> Value {
    json!({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": "AAPL",
                    "currency": "USD",
                    "exchangeTimezoneName": "America/New_York",
                    "gmtoffset": -14400
                },
                "timestamp": [1762785000, 1762871400, 1762957800],
                "events": {
                    "dividends": {"1762785000": {"amount": 0.26, "date": 1762785000}},
                    "splits": {"1762957800": {"date": 1762957800, "numerator": 4, "denominator": 1}},
                    "capitalGains": {"1762871400": {"amount": 0.11, "date": 1762871400}}
                },
                "indicators": {
                    "quote": [{
                        "open": [260.0, 261.0, 262.0],
                        "high": [261.0, 262.0, 263.0],
                        "low": [259.0, 260.0, 261.0],
                        "close": [260.5, 261.5, 262.5],
                        "volume": [1000, 1100, 1200]
                    }],
                    "adjclose": [{"adjclose": [259.5, 260.5, 655.0]}]
                }
            }],
            "error": null
        }
    })
}

fn hk_chart_body() -> Value {
    json!({
        "chart": {
            "result": [{
                "meta": {"symbol": "0700.HK", "currency": "HKD",
                         "exchangeTimezoneName": "Asia/Hong_Kong", "gmtoffset": 28800},
                "timestamp": [1778808600, 1779067800],
                "events": {"dividends": {"1778808600": {"amount": 5.3, "date": 1778808600}}},
                "indicators": {
                    "quote": [{
                        "open": [380.0, 381.0], "high": [382.0, 383.0],
                        "low": [379.0, 380.0], "close": [381.0, 382.0],
                        "volume": [2000, 2100]
                    }],
                    "adjclose": [{"adjclose": [380.5, 381.5]}]
                }
            }],
            "error": null
        }
    })
}

fn transport_with(body: Value) -> MockTransport {
    let mut response = HttpResponse::new(200, "OK", body.to_string().into_bytes());
    response.final_url = "https://query2.finance.yahoo.com/v8/finance/chart/AAPL".into();
    MockTransport::new(vec![response])
}

fn cache_dir() -> Option<std::path::PathBuf> {
    Some(std::path::PathBuf::from("sd-native-actions-test-cache"))
}

fn call_history(body: Value, params: Value) -> Value {
    let transport = transport_with(body);
    let job = JobState::new(JobContext::default());
    let params: Map<String, Value> = params.as_object().cloned().unwrap_or_default();
    yahoo_gateway::call_with_job("yahoo_history", &params, cache_dir(), &transport, &job)
        .expect("native history must succeed on a scripted chart body")
}

#[test]
fn native_history_carries_dividends_splits_and_capital_gains() {
    let result = call_history(
        chart_body(),
        json!({"symbol": "AAPL", "interval": "1d", "period": "5d"}),
    );
    let bars = result["data"]["bars"].as_array().expect("bars");
    assert_eq!(bars.len(), 3);
    // Dividend event at 2025-11-10 14:30Z = 09:30 America/New_York session.
    assert_eq!(bars[0]["date"], "2025-11-10");
    assert_eq!(bars[0]["dividends"], json!(0.26));
    // Capital gain event on the next session; absent split/dividend keys stay null.
    assert_eq!(bars[1]["dividends"], Value::Null);
    assert_eq!(bars[1]["capital_gains"], json!(0.11));
    // Split event maps the numerator/denominator ratio onto that session.
    assert_eq!(bars[2]["stock_splits"], json!(4.0));
    assert_eq!(bars[2]["dividends"], Value::Null);
    let actions = result["adjustment_evidence"]["actions"]
        .as_array()
        .expect("actions");
    assert_eq!(actions.len(), 3);
    let kinds: Vec<&str> = actions
        .iter()
        .filter_map(|action| action["type"].as_str())
        .collect();
    assert_eq!(kinds, vec!["dividend", "capital_gain", "split"]);
    assert_eq!(
        result["adjustment_evidence"]["actions"][0]["value"],
        json!(0.26)
    );
    // Event-free rows are not zero-filled and the status is unchanged.
    assert_eq!(result["status"], "success");
    let missing = result["data"]["bars"][1]["missing_fields"]
        .as_array()
        .expect("missing_fields");
    assert!(missing.is_empty());
}

#[test]
fn native_history_maps_events_in_the_exchange_timezone() {
    let result = call_history(
        hk_chart_body(),
        json!({"symbol": "0700.HK", "interval": "1d", "period": "5d"}),
    );
    let bars = result["data"]["bars"].as_array().expect("bars");
    // 2026-05-15T01:30Z = 09:30 Asia/Hong_Kong on 2026-05-15.
    assert_eq!(bars[0]["date"], "2026-05-15");
    assert_eq!(bars[0]["dividends"], json!(5.3));
    let actions = result["adjustment_evidence"]["actions"]
        .as_array()
        .expect("actions");
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0]["date"], "2026-05-15");
}

#[test]
fn native_history_without_events_keeps_null_action_columns() {
    let mut body = chart_body();
    body["chart"]["result"][0]
        .as_object_mut()
        .expect("result object")
        .remove("events");
    let result = call_history(
        body,
        json!({"symbol": "AAPL", "interval": "1d", "period": "5d"}),
    );
    let bars = result["data"]["bars"].as_array().expect("bars");
    assert_eq!(bars[0]["dividends"], Value::Null);
    assert_eq!(bars[0]["stock_splits"], Value::Null);
    assert_eq!(bars[0]["capital_gains"], Value::Null);
    assert_eq!(result["status"], "success");
    assert_eq!(
        result["adjustment_evidence"]["actions"]
            .as_array()
            .expect("actions")
            .len(),
        0
    );
}

/// Minimal legacy gateway returning one scripted chart payload.
struct FakeChartGateway {
    payload: Value,
}

impl LegacyGateway for FakeChartGateway {
    fn stock_kline_yahoo(
        &self,
        _symbol: &str,
        _interval: &str,
        _range_: &str,
        _include_metadata: bool,
    ) -> Result<Value, LegacyError> {
        Ok(self.payload.clone())
    }

    fn yahoo_quote_summary(&self, _: &str, _: &[String]) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn key_statistics(&self, _: &str) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn analyst_estimates(&self, _: &str) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn institutional_holders(&self, _: &str) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn financial_statements_yahoo(&self, _: &str, _: bool) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn options_chain(&self, _: &str, _: Option<i64>) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }

    fn stock_search(&self, _: &str, _: i64) -> Result<Value, LegacyError> {
        Err(LegacyError::provider("unused in this test"))
    }
}

#[test]
fn compatibility_history_fallback_carries_the_same_action_columns() {
    let payload = json!({
        "bars": [{
            "date": "2025-11-10",
            "timestamp_epoch": 1762785000,
            "timestamp_utc": "2025-11-10T14:30:00Z",
            "open": 260.0, "high": 261.0, "low": 259.0, "close": 260.5, "volume": 1000
        }],
        "meta": {"symbol": "AAPL", "currency": "USD",
                 "exchangeTimezoneName": "America/New_York"},
        "adjclose": {"adjclose": [259.5]},
        "events": {"dividends": {"1762785000": {"amount": 0.26, "date": 1762785000}}},
        "price_basis": "provider_quote_OHLCV_adjustment_unverified"
    });
    let gateway = FakeChartGateway { payload };
    let now = Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap();
    let mut policy = NoPolicy;
    let envelope = legacy_history(&gateway, "AAPL", &Map::new(), &mut policy, now)
        .expect("compatibility history");
    let bars = envelope["data"]["bars"].as_array().expect("bars");
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0]["dividends"], json!(0.26));
    let actions = envelope["adjustment_evidence"]["actions"]
        .as_array()
        .expect("actions");
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0]["type"], "dividend");
}
