//! R15 checks: provider preflight/counting, options Unix expiries, cache-dir normalization.
//!
//! Ports of `check_f08_preflight_and_counting`, `check_f11_options_expiry_unix_seconds`
//! and `check_f12_cache_dir_pathlike`. Every assertion drives the real Rust provider,
//! compatibility orchestrator or legacy route; none of them inspects Python.

use std::cell::RefCell;
use std::collections::BTreeMap;

use chrono::{TimeZone, Utc};
use serde_json::{Map, Value};

use sd_providers::http::{HttpResponse, MockTransport};
use sd_providers::transport::JobContext;
use sd_providers::yahoo_compat::{fallback, LegacyError, LegacyGateway, ReservePolicy};
use sd_providers::yahoo_legacy::options_chain;
use sd_providers::yahoo_provider::{
    reset_job_circuit, CacheDirMode, GatewayError, TransportPolicy, YahooFallback, YahooNative,
    YahooProvider,
};
use sd_providers::yahoo_session::YahooSession;

struct MapGateway {
    chart: Value,
    expiries: Vec<i64>,
    hits: RefCell<usize>,
}

impl MapGateway {
    fn new(expiries: Vec<i64>) -> Self {
        Self {
            chart: serde_json::json!({
                "bars": [{"date": "2026-01-02", "timestamp_utc": "2026-01-02T14:30:00Z",
                          "Open": 100.0, "High": 103.0, "Low": 99.5, "Close": 102.5,
                          "Volume": 1000}],
                "meta": {"symbol": "AAPL", "currency": "USD",
                         "exchangeTimezoneName": "America/New_York"},
                "adjclose": {"adjclose": [101.25]},
            }),
            expiries,
            hits: RefCell::new(0),
        }
    }

    fn bump(&self) {
        *self.hits.borrow_mut() += 1;
    }
}

impl LegacyGateway for MapGateway {
    fn stock_kline_yahoo(
        &self,
        _symbol: &str,
        _interval: &str,
        _range: &str,
        _include_metadata: bool,
    ) -> Result<Value, LegacyError> {
        self.bump();
        Ok(self.chart.clone())
    }

    fn yahoo_quote_summary(
        &self,
        _symbol: &str,
        _modules: &[String],
    ) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"price": {"regularMarketPrice": {"raw": 101.5}}}))
    }

    fn key_statistics(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"current_price": 101.5}))
    }

    fn analyst_estimates(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"eps_trend": []}))
    }

    fn institutional_holders(&self, _symbol: &str) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"top_holders": []}))
    }

    fn financial_statements_yahoo(
        &self,
        _symbol: &str,
        _quarterly: bool,
    ) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"income": []}))
    }

    fn options_chain(&self, _symbol: &str, expiration: Option<i64>) -> Result<Value, LegacyError> {
        self.bump();
        if let Some(expiration) = expiration {
            if self.expiries.contains(&expiration) {
                return Ok(serde_json::json!({"expiration_dates": self.expiries,
                                             "calls": [], "puts": []}));
            }
            return Err(LegacyError::value("unknown expiry"));
        }
        Ok(serde_json::json!({"expiration_dates": self.expiries, "calls": [], "puts": []}))
    }

    fn stock_search(&self, _keyword: &str, _count: i64) -> Result<Value, LegacyError> {
        self.bump();
        Ok(serde_json::json!({"quotes": []}))
    }
}

struct CountingPolicy {
    limit: usize,
    calls: usize,
}

impl ReservePolicy for CountingPolicy {
    fn reserve(&mut self) -> Result<(), LegacyError> {
        self.calls += 1;
        if self.calls > self.limit {
            return Err(LegacyError::policy("BudgetExceeded", "budget exhausted"));
        }
        Ok(())
    }
}

struct StubNative;

impl YahooNative for StubNative {
    fn call(&mut self, _name: &str, _params: &Map<String, Value>) -> Result<Value, GatewayError> {
        Ok(serde_json::json!({"status": "success", "data": {"sentinel": true}}))
    }
}

struct StubFallback;

impl YahooFallback for StubFallback {
    fn run(
        &mut self,
        _function: &str,
        _params: &Map<String, Value>,
        _reason: &Value,
        _policy: &mut dyn ReservePolicy,
    ) -> Result<Value, sd_providers::yahoo_compat::GatewayRaise> {
        Ok(serde_json::json!({"status": "empty", "fallback_used": true}))
    }
}

fn params(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(key, value)| ((*key).to_string(), value.clone()))
        .collect()
}

fn provider_with(mode: CacheDirMode) -> YahooProvider<'static> {
    let native: &'static mut StubNative = Box::leak(Box::new(StubNative));
    let fallback: &'static mut StubFallback = Box::leak(Box::new(StubFallback));
    YahooProvider::new(mode, TransportPolicy::default(), native, fallback)
}

fn expect_invalid(
    provider: &mut YahooProvider<'_>,
    name: &str,
    values: Map<String, Value>,
) -> Result<(), String> {
    match provider.call(name, &values) {
        Err(error) if error.code == "InvalidParameters" => Ok(()),
        Err(error) => Err(format!(
            "expected InvalidParameters, got {}: {}",
            error.code, error.message
        )),
        Ok(_) => Err("expected InvalidParameters, call succeeded".to_string()),
    }
}

/// F08: preflight validation and lowest-level fallback counting.
pub fn legacy_f08() -> Result<(), String> {
    reset_job_circuit();
    let mut provider = provider_with(CacheDirMode::Directory("/tmp/sd-cache".into()));
    let many: Vec<Value> = (0..11)
        .map(|index| Value::from(format!("S{index}")))
        .collect();
    expect_invalid(
        &mut provider,
        "yahoo_history_batch",
        params(&[
            ("symbols", Value::Array(many)),
            ("period", Value::from("1y")),
        ]),
    )?;
    expect_invalid(
        &mut provider,
        "yahoo_history_batch",
        params(&[
            ("symbols", serde_json::json!(["AAPL", "AAPL"])),
            ("period", Value::from("1y")),
        ]),
    )?;
    expect_invalid(
        &mut provider,
        "yahoo_history_batch",
        params(&[
            ("symbols", serde_json::json!(["AAPL", "0700.HK"])),
            ("period", Value::from("1y")),
        ]),
    )?;
    expect_invalid(
        &mut provider,
        "yahoo_history_batch",
        params(&[
            ("symbols", serde_json::json!([123])),
            ("period", Value::from("1y")),
        ]),
    )?;
    expect_invalid(
        &mut provider,
        "yahoo_history",
        params(&[("period", Value::from("1mo"))]),
    )?;

    let mut no_cache = provider_with(CacheDirMode::Missing);
    expect_invalid(
        &mut no_cache,
        "yahoo_quote",
        params(&[("symbol", Value::from("AAPL"))]),
    )?;

    let result = provider
        .call(
            "yahoo_history_batch",
            &params(&[
                ("symbols", serde_json::json!(["AAPL", "MSFT"])),
                ("period", Value::from("1y")),
            ]),
        )
        .map_err(|error| error.message)?;
    if result.get("status").and_then(Value::as_str) != Some("success") {
        return Err(format!(
            "valid batch did not reach the native route: {result}"
        ));
    }

    let gateway = MapGateway::new(vec![]);
    let reason = serde_json::json!({"category": "DataUnavailable"});
    let mut policy = CountingPolicy {
        limit: 99,
        calls: 0,
    };
    fallback(
        &gateway,
        "yahoo_history_batch",
        &params(&[("symbols", serde_json::json!(["A", "B", "C"]))]),
        &reason,
        &mut policy,
        Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap(),
        None,
    )
    .map_err(|raised| raised.message)?;
    if policy.calls != 3 {
        return Err(format!(
            "batch fallback reserved {} calls, expected 3",
            policy.calls
        ));
    }
    let mut policy = CountingPolicy {
        limit: 99,
        calls: 0,
    };
    fallback(
        &gateway,
        "yahoo_history",
        &params(&[("symbol", Value::from("A"))]),
        &reason,
        &mut policy,
        Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap(),
        None,
    )
    .map_err(|raised| raised.message)?;
    if policy.calls != 1 {
        return Err(format!(
            "history fallback reserved {} calls, expected 1",
            policy.calls
        ));
    }
    let expiries = vec![Utc
        .with_ymd_and_hms(2026, 1, 2, 0, 0, 0)
        .unwrap()
        .timestamp()];
    let options_gateway = MapGateway::new(expiries.clone());
    let mut policy = CountingPolicy {
        limit: 99,
        calls: 0,
    };
    fallback(
        &options_gateway,
        "yahoo_options",
        &params(&[
            ("symbol", Value::from("A")),
            ("expiration", Value::from("2026-01-02")),
        ]),
        &reason,
        &mut policy,
        Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap(),
        None,
    )
    .map_err(|raised| raised.message)?;
    if policy.calls != 2 {
        return Err(format!(
            "options fallback reserved {} calls, expected 2",
            policy.calls
        ));
    }
    let mut policy = CountingPolicy { limit: 2, calls: 0 };
    match fallback(
        &gateway,
        "yahoo_history_batch",
        &params(&[("symbols", serde_json::json!(["A", "B", "C"]))]),
        &reason,
        &mut policy,
        Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap(),
        None,
    ) {
        Err(raised) if raised.code == "BudgetExceeded" && policy.calls == 3 => Ok(()),
        Err(raised) => Err(format!(
            "unexpected budget error {} ({})",
            raised.code, raised.message
        )),
        Ok(_) => Err("budget failure did not propagate".to_string()),
    }?;
    reset_job_circuit();
    Ok(())
}

fn options_response(unix: i64) -> Vec<HttpResponse> {
    let body = serde_json::json!({"optionChain": {"error": null, "result": [{
        "expirationDates": [unix],
        "options": [{"calls": [], "puts": []}],
        "quote": {"regularMarketPrice": {"raw": 101.5}},
    }]}});
    vec![HttpResponse {
        status: 200,
        reason: "OK".to_string(),
        body: body.to_string().into_bytes(),
        headers: BTreeMap::new(),
        final_url: String::new(),
    }]
}

/// F11: option expiries stay Unix seconds end to end.
pub fn legacy_f11() -> Result<(), String> {
    reset_job_circuit();
    let unix = Utc
        .with_ymd_and_hms(2026, 11, 20, 0, 0, 0)
        .unwrap()
        .timestamp();
    let transport = MockTransport::new(options_response(unix));
    let session = YahooSession {
        crumb: "stub".into(),
        ..YahooSession::default()
    };
    let mut context = JobContext::default();
    let chain = options_chain(
        Some(&Value::from("AAPL")),
        Some(&Value::from(unix)),
        &session,
        &transport,
        &mut context,
    )
    .map_err(|error| format!("{}: {}", error.kind.error_type(), error.message))?;
    let request = transport.requests().into_iter().next();
    let Some(request) = request else {
        return Err("options route sent no request".to_string());
    };
    let date_param = request
        .params
        .iter()
        .find(|(key, _)| key == "date")
        .map(|(_, value)| value.clone());
    if date_param != Some(unix.to_string()) {
        return Err(format!(
            "date parameter was {date_param:?}, expected {unix}"
        ));
    }
    if chain.get("expiration_dates") != Some(&serde_json::json!([unix])) {
        return Err(format!("expiration_dates changed: {chain}"));
    }
    for bad in [
        Value::from(0),
        Value::from(-1),
        Value::from(4_102_444_801i64),
        Value::from("2026-11-20"),
    ] {
        let transport = MockTransport::new(options_response(unix));
        let outcome = options_chain(
            Some(&Value::from("AAPL")),
            Some(&bad),
            &session,
            &transport,
            &mut context,
        );
        if outcome.is_ok() {
            return Err(format!("expiration {bad} was accepted"));
        }
    }

    let gateway = MapGateway::new(vec![unix]);
    let reason = serde_json::json!({"category": "DependencyUnavailable"});
    let mut policy = CountingPolicy {
        limit: 99,
        calls: 0,
    };
    let envelope = fallback(
        &gateway,
        "yahoo_options",
        &params(&[
            ("symbol", Value::from("AAPL")),
            ("expiration", Value::from("2026-11-20")),
        ]),
        &reason,
        &mut policy,
        Utc.with_ymd_and_hms(2026, 1, 9, 15, 0, 0).unwrap(),
        None,
    )
    .map_err(|raised| raised.message)?;
    if envelope.get("status").and_then(Value::as_str) != Some("partial") {
        return Err(format!("fallback status: {envelope}"));
    }
    if envelope.get("fallback_used") != Some(&Value::Bool(true))
        || envelope.get("fallback_route").and_then(Value::as_str) != Some("options_chain")
    {
        return Err(format!("fallback provenance wrong: {envelope}"));
    }
    let dates = envelope
        .get("data")
        .and_then(|data| data.get("expiration_dates"))
        .cloned();
    if dates != Some(serde_json::json!([unix])) {
        return Err(format!("fallback expiration_dates wrong: {dates:?}"));
    }
    Ok(())
}

/// F12: PathLike cache dirs normalize to strings and non-paths are rejected.
pub fn legacy_f12() -> Result<(), String> {
    reset_job_circuit();
    let directory = std::env::temp_dir().join("sd-selfcheck-f12");
    let mode = CacheDirMode::resolve(Some(directory.clone()));
    let CacheDirMode::Directory(normalized) = &mode else {
        return Err(format!("PathLike cache_dir did not normalize: {mode:?}"));
    };
    if normalized.is_empty() {
        return Err("normalized cache_dir is empty".to_string());
    }
    let mut provider = provider_with(mode);
    provider
        .call("yahoo_quote", &params(&[("symbol", Value::from("AAPL"))]))
        .map_err(|error| error.message)?;
    let mut invalid = provider_with(CacheDirMode::Invalid);
    match invalid.call("yahoo_quote", &params(&[("symbol", Value::from("AAPL"))])) {
        Err(error) if error.code == "InvalidParameters" => Ok(()),
        Err(error) => Err(format!(
            "non-path cache_dir error was {}: {}",
            error.code, error.message
        )),
        Ok(_) => Err("non-path cache_dir was accepted".to_string()),
    }
}

/// One migrated legacy check.
pub type CheckFn = fn() -> Result<(), String>;

/// Registry entries for this package.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("F08", legacy_f08),
        ("F11", legacy_f11),
        ("F12", legacy_f12),
    ]
}
