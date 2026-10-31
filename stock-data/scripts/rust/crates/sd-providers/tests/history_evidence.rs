//! S05: the native history route must carry the adjusted-close series and the
//! source/adjustment evidence into the gateway envelope, the collector block and batch
//! results; the chart `adjclose` array must not be mistaken for an object.

use std::path::PathBuf;

use sd_providers::collector;
use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::{JobContext, JobState, MAX_RESPONSE_BYTES};
use sd_providers::yahoo_gateway::call_with_job;
use serde_json::{json, Map, Value};

fn cache_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "sd-history-evidence-{label}-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).expect("cache");
    dir
}

fn chart_body(symbol: &str, close_base: f64) -> Vec<u8> {
    // Two sessions with close above adjusted close, exercising the Adj Close / Close ratio.
    let body = json!({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": symbol,
                    "currency": "USD",
                    "exchangeTimezoneName": "America/New_York",
                    "quoteType": "EQUITY",
                    "regularMarketTime": 1730000000
                },
                "timestamp": [1729900800, 1729987200],
                "indicators": {
                    "quote": [{
                        "open": [close_base, close_base + 1.0],
                        "high": [close_base + 2.0, close_base + 3.0],
                        "low": [close_base - 1.0, close_base],
                        "close": [close_base, close_base + 1.0],
                        "volume": [1000, 2000]
                    }],
                    "adjclose": [{
                        "adjclose": [close_base - 1.0, close_base]
                    }]
                },
                "events": {"splits": {}, "dividends": {}}
            }],
            "error": null
        }
    });
    serde_json::to_vec(&body).expect("json")
}

fn history_params(symbol: &str) -> Map<String, Value> {
    let mut params = Map::new();
    params.insert("symbol".into(), json!(symbol));
    params.insert("interval".into(), json!("1d"));
    params.insert("range".into(), json!("1mo"));
    params.insert("price_basis".into(), json!("adjusted"));
    params
}

fn job() -> JobState {
    JobState::new(JobContext::new(15.0, 120.0, 30, MAX_RESPONSE_BYTES))
}

#[test]
fn native_history_carries_adjclose_and_evidence() {
    let transport = MockTransport::new(vec![HttpResponse::new(
        200,
        "OK",
        chart_body("AAPL", 101.0),
    )]);
    let state = job();
    let envelope = call_with_job(
        "yahoo_history",
        &history_params("AAPL"),
        Some(cache_dir("single")),
        &transport,
        &state,
    )
    .expect("history");
    assert_eq!(envelope["status"], "success");

    // The adjusted series is no longer silently dropped.
    assert_eq!(envelope["adjustment"], "adjusted");
    assert_eq!(
        envelope["adjustment_evidence"]["transformation"],
        "Adj Close / Close applied to OHLC; volume unchanged"
    );
    let ratios = envelope["adjustment_evidence"]["ratios"]
        .as_array()
        .expect("ratios");
    assert!(!ratios.is_empty(), "adjusted basis must record ratios");
    assert_eq!(
        envelope["source_timestamp_kind"],
        "bar_label_not_trade_time"
    );
    let source_timestamp = envelope["source_timestamp"]
        .as_str()
        .expect("source timestamp");
    assert!(source_timestamp.ends_with('Z'), "{source_timestamp}");
    let bars = envelope["data"]["bars"].as_array().expect("bars");
    let close = bars[0]["close"].as_f64().expect("close");
    assert!(
        (close - 100.0).abs() < 1e-6,
        "adjusted close should be 100 (Adj Close/Close), got {close}"
    );
    assert_eq!(envelope["data"]["currency"], "USD");
    assert_eq!(envelope["data"]["frequency"], "1d");
}

#[test]
fn collector_block_keeps_the_evidence_chain() {
    let transport = MockTransport::new(vec![HttpResponse::new(
        200,
        "OK",
        chart_body("AAPL", 101.0),
    )]);
    let state = job();
    let envelope = call_with_job(
        "yahoo_history",
        &history_params("AAPL"),
        Some(cache_dir("collector")),
        &transport,
        &state,
    )
    .expect("history");
    let block = collector::block(&envelope);
    assert_eq!(block["status"], "complete");
    assert_eq!(block["adjustment"], "adjusted");
    assert_eq!(block["adjustment_evidence"]["source"], "Yahoo Finance");
    assert_eq!(block["source_timestamp_kind"], "bar_label_not_trade_time");
    assert!(block["source_timestamp"].as_str().is_some());
    assert_eq!(block["currency"], "USD");
    assert_eq!(block["frequency"], "1d");
    assert_eq!(block["data"]["bars"].as_array().expect("bars").len(), 2);
}

#[test]
fn batch_results_keep_per_symbol_evidence_and_use_partial_status() {
    let transport = MockTransport::new(vec![
        HttpResponse::new(200, "OK", chart_body("AAPL", 101.0)),
        HttpResponse::new(200, "OK", chart_body("MSFT", 201.0)),
    ]);
    let state = job();
    let mut params = Map::new();
    params.insert("symbols".into(), json!(["AAPL", "MSFT"]));
    params.insert("interval".into(), json!("1d"));
    params.insert("range".into(), json!("1mo"));
    params.insert("price_basis".into(), json!("adjusted"));
    let envelope = call_with_job(
        "yahoo_history_batch",
        &params,
        Some(cache_dir("batch")),
        &transport,
        &state,
    )
    .expect("batch");
    assert_eq!(envelope["status"], "success");
    let symbols = envelope["data"]["symbols"].as_object().expect("symbols");
    for symbol in ["AAPL", "MSFT"] {
        let result = &symbols[symbol];
        assert_eq!(result["status"], "success", "{symbol}");
        assert_eq!(result["adjustment"], "adjusted", "{symbol}");
        assert!(
            result["adjustment_evidence"]["ratios"]
                .as_array()
                .map(|ratios| !ratios.is_empty())
                .unwrap_or(false),
            "{symbol} lost adjustment evidence: {result}"
        );
        assert!(
            result["source_timestamp"].as_str().is_some(),
            "{symbol} lost source timestamp"
        );
        assert_eq!(result["data"]["bars"].as_array().expect("bars").len(), 2);
    }
}

fn chart_body_without_adjclose(symbol: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "chart": {
            "result": [{
                "meta": {
                    "symbol": symbol,
                    "currency": "USD",
                    "exchangeTimezoneName": "America/New_York",
                    "quoteType": "EQUITY"
                },
                "timestamp": [1729900800, 1729987200],
                "indicators": {
                    "quote": [{
                        "open": [100.0, 101.0],
                        "high": [102.0, 103.0],
                        "low": [99.0, 100.0],
                        "close": [101.0, 102.0],
                        "volume": [1000, 2000]
                    }]
                },
                "events": {"splits": {}, "dividends": {}}
            }],
            "error": null
        }
    }))
    .expect("json")
}

#[test]
fn adjusted_basis_without_adjclose_is_partial_and_records_gaps() {
    let transport = MockTransport::new(vec![HttpResponse::new(
        200,
        "OK",
        chart_body_without_adjclose("AAPL"),
    )]);
    let state = job();
    let envelope = call_with_job(
        "yahoo_history",
        &history_params("AAPL"),
        Some(cache_dir("no-adjclose")),
        &transport,
        &state,
    )
    .expect("history");
    assert_eq!(envelope["status"], "partial");
    assert_eq!(envelope["adjustment"], "adjusted");
    let ratios = envelope["adjustment_evidence"]["ratios"]
        .as_array()
        .expect("ratios");
    assert_eq!(ratios.len(), 2);
    assert!(
        ratios.iter().all(|entry| entry["ratio"].is_null()),
        "no adjusted series means every ratio is null: {ratios:?}"
    );
    let warnings = envelope["warnings"].as_array().expect("warnings");
    assert!(warnings
        .iter()
        .any(|warning| warning == "adjusted_view_has_ratio_gaps"));
    let bar = &envelope["data"]["bars"][0];
    assert_eq!(bar["adjustment_gap"], "invalid_or_missing_adj_close_ratio");
    assert_eq!(bar["adjustment_ratio"], Value::Null);
    assert!(envelope["source_timestamp"].as_str().is_some());
}

#[test]
fn adjusted_basis_with_mixed_null_adjclose_marks_only_the_gaps() {
    let mut body: Value = serde_json::from_slice(&chart_body("AAPL", 101.0)).expect("json");
    body["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"] = json!([100.0, null]);
    let transport = MockTransport::new(vec![HttpResponse::new(
        200,
        "OK",
        serde_json::to_vec(&body).expect("json"),
    )]);
    let state = job();
    let envelope = call_with_job(
        "yahoo_history",
        &history_params("AAPL"),
        Some(cache_dir("mixed-adjclose")),
        &transport,
        &state,
    )
    .expect("history");
    let bars = envelope["data"]["bars"].as_array().expect("bars");
    assert_eq!(bars[0]["adjustment_gap"], Value::Null);
    assert!((bars[0]["close"].as_f64().expect("close") - 100.0).abs() < 1e-6);
    assert_eq!(
        bars[1]["adjustment_gap"],
        "invalid_or_missing_adj_close_ratio"
    );
    assert_eq!(envelope["status"], "partial");
}
