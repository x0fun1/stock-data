//! S06: native Yahoo parameter semantics — prepost forwarded, repair/screen rejected
//! explicitly (no invented fallback), batch failure isolation, honest provenance label.

use std::path::PathBuf;

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::{JobContext, JobState, MAX_RESPONSE_BYTES};
use sd_providers::yahoo_gateway::call_with_job;
use serde_json::{json, Map, Value};

fn cache_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "sd-native-semantics-{label}-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).expect("cache");
    dir
}

fn chart_body(symbol: &str) -> Vec<u8> {
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
                    }],
                    "adjclose": [{"adjclose": [100.0, 101.0]}]
                },
                "events": {"splits": {}, "dividends": {}}
            }],
            "error": null
        }
    }))
    .expect("json")
}

fn params(symbol: &str) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("symbol".into(), json!(symbol));
    map.insert("interval".into(), json!("1d"));
    map.insert("range".into(), json!("1mo"));
    map
}

fn job() -> JobState {
    JobState::new(JobContext::new(15.0, 120.0, 30, MAX_RESPONSE_BYTES))
}

#[test]
fn prepost_is_forwarded_to_the_wire_and_recorded() {
    for prepost in [true, false] {
        let transport = MockTransport::new(vec![HttpResponse::new(200, "OK", chart_body("AAPL"))]);
        let mut request = params("AAPL");
        request.insert("prepost".into(), json!(prepost));
        let envelope = call_with_job(
            "yahoo_history",
            &request,
            Some(cache_dir(if prepost { "prepost" } else { "noprepost" })),
            &transport,
            &job(),
        )
        .expect("history");
        assert_eq!(envelope["request_options"]["prepost"], prepost);
        assert_eq!(envelope["request_options"]["repair"], false);
        let recorded = &transport.requests()[0];
        let flag = recorded
            .params
            .iter()
            .find(|(name, _)| name == "includePrePost");
        if prepost {
            assert_eq!(
                flag.map(|(_, value)| value.as_str()),
                Some("true"),
                "prepost must reach the wire"
            );
        } else {
            assert!(flag.is_none(), "prepost=false must not add the flag");
        }
    }
}

#[test]
fn repair_is_rejected_before_any_request() {
    let transport = MockTransport::new(vec![HttpResponse::new(200, "OK", chart_body("AAPL"))]);
    let mut request = params("AAPL");
    request.insert("repair".into(), json!(true));
    let error = call_with_job(
        "yahoo_history",
        &request,
        Some(cache_dir("repair")),
        &transport,
        &job(),
    )
    .expect_err("repair must not be silently ignored");
    assert_eq!(error.0, "UnsupportedCapability", "{error:?}");
    assert!(error.1.contains("repair"), "{error:?}");
    assert_eq!(transport.requests().len(), 0);
}

#[test]
fn screener_is_an_explicit_capability_gap_not_a_fake_fallback() {
    let transport = MockTransport::new(vec![HttpResponse::new(200, "OK", chart_body("AAPL"))]);
    let error = call_with_job(
        "yahoo_screen",
        &params("AAPL"),
        Some(cache_dir("screen")),
        &transport,
        &job(),
    )
    .expect_err("screener has no native or legacy route");
    assert_eq!(error.0, "UnsupportedCapability", "{error:?}");
    assert!(
        !error.1.contains("NoLegacyRoute"),
        "must not be reported as an invented fallback: {error:?}"
    );
    assert_eq!(transport.requests().len(), 0);
}

#[test]
fn native_provenance_is_not_labelled_as_yfinance_processed() {
    let transport = MockTransport::new(vec![HttpResponse::new(200, "OK", chart_body("AAPL"))]);
    let envelope = call_with_job(
        "yahoo_history",
        &params("AAPL"),
        Some(cache_dir("label")),
        &transport,
        &job(),
    )
    .expect("history");
    assert_eq!(
        envelope["native_payload_kind"],
        "native_chart_normalized_from_wire"
    );
    assert_eq!(
        envelope["wire_route"],
        "query2.finance.yahoo.com/v8/finance/chart/AAPL"
    );
    assert_ne!(
        envelope["native_payload_kind"],
        "yfinance_processed_result_not_raw_http"
    );
}

#[test]
fn batch_isolates_a_failed_symbol_and_reports_partial() {
    let transport = MockTransport::new(vec![
        HttpResponse::new(200, "OK", chart_body("AAPL")),
        HttpResponse::new(500, "Server Error", b"{\"error\":true}".to_vec()),
    ]);
    let mut request = Map::new();
    request.insert("symbols".into(), json!(["AAPL", "MSFT"]));
    request.insert("interval".into(), json!("1d"));
    request.insert("range".into(), json!("1mo"));
    let envelope = call_with_job(
        "yahoo_history_batch",
        &request,
        Some(cache_dir("batch-failure")),
        &transport,
        &job(),
    )
    .expect("batch envelope");
    assert_eq!(
        envelope["status"], "partial",
        "one failed symbol must not poison the batch"
    );
    let symbols = envelope["data"]["symbols"].as_object().expect("symbols");
    assert_eq!(symbols["AAPL"]["status"], "success");
    assert_eq!(symbols["AAPL"]["adjustment"], "provider");
    assert_eq!(symbols["MSFT"]["status"], "failed");
    assert_eq!(symbols["MSFT"]["error"]["category"], "HTTPError");
    // The failure is isolated: both symbols were attempted.
    assert_eq!(transport.requests().len(), 2);
}
