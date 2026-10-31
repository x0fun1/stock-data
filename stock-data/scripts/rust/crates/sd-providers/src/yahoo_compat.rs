//! Automatic, provenance-preserving fallback to the pre-yfinance Yahoo routes
//! (`yahoo_compat_fallback.py`).
//!
//! The orchestrator never imports the provider: it receives a [`LegacyGateway`] over the
//! seven compatibility routes, counts every gateway call against the shared transport
//! policy and always discloses which interface produced the data.

use chrono::{DateTime, Datelike, NaiveDate, TimeZone, Utc};
use serde_json::{Map, Value};

use crate::yahoo_legacy::attach_chart_events;
use crate::yfinance_normalize::normalize_history;

/// One gateway error with the Python exception name and optional policy code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegacyError {
    /// Policy code (`BudgetExceeded`, `DeadlineExceeded`, ...), when present.
    pub code: Option<String>,
    /// Python exception class name.
    pub exception_type: String,
    /// Redacted message.
    pub message: String,
}

impl LegacyError {
    /// Caller input error.
    pub fn value(message: impl Into<String>) -> Self {
        Self {
            code: None,
            exception_type: "ValueError".to_string(),
            message: message.into(),
        }
    }

    /// Provider error.
    pub fn provider(message: impl Into<String>) -> Self {
        Self {
            code: None,
            exception_type: "ProviderError".to_string(),
            message: message.into(),
        }
    }

    /// Error with a policy code.
    pub fn policy(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: Some(code.to_string()),
            exception_type: "GatewayError".to_string(),
            message: message.into(),
        }
    }
}

/// The seven legacy compatibility routes used by the fallback.
pub trait LegacyGateway {
    /// `stock_kline_yahoo`
    fn stock_kline_yahoo(
        &self,
        symbol: &str,
        interval: &str,
        range_: &str,
        include_metadata: bool,
    ) -> Result<Value, LegacyError>;
    /// `yahoo_quote_summary`
    fn yahoo_quote_summary(&self, symbol: &str, modules: &[String]) -> Result<Value, LegacyError>;
    /// `key_statistics`
    fn key_statistics(&self, symbol: &str) -> Result<Value, LegacyError>;
    /// `analyst_estimates`
    fn analyst_estimates(&self, symbol: &str) -> Result<Value, LegacyError>;
    /// `institutional_holders`
    fn institutional_holders(&self, symbol: &str) -> Result<Value, LegacyError>;
    /// `financial_statements_yahoo`
    fn financial_statements_yahoo(
        &self,
        symbol: &str,
        quarterly: bool,
    ) -> Result<Value, LegacyError>;
    /// `options_chain`
    fn options_chain(&self, symbol: &str, expiration: Option<i64>) -> Result<Value, LegacyError>;
    /// `stock_search`
    fn stock_search(&self, keyword: &str, count: i64) -> Result<Value, LegacyError>;
}

/// Shared transport budget: every legacy call reserves at the lowest level.
pub trait ReservePolicy {
    /// Reserve one gateway call; a policy exhaustion is a terminal gateway error.
    fn reserve(&mut self) -> Result<(), LegacyError>;
}

/// Zero-budget policy used when the orchestrator has no transport budget.
pub struct NoPolicy;

impl ReservePolicy for NoPolicy {
    fn reserve(&mut self) -> Result<(), LegacyError> {
        Ok(())
    }
}

/// Policy codes that must never be swallowed by a compatibility fallback.
const POLICY_ERROR_CODES: [&str; 5] = [
    "BudgetExceeded",
    "DeadlineExceeded",
    "JobCircuitOpen",
    "AccessDenied",
    "RateLimited",
];

/// One gateway raise from the compatibility layer (propagated, never wrapped).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayRaise {
    /// Policy code.
    pub code: String,
    /// Message.
    pub message: String,
}

/// `_has_data`: nested empty containers are not data; genuine zero is.
pub fn has_data(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.values().any(has_data),
        Value::Array(items) => !items.is_empty(),
        Value::Null => false,
        Value::String(text) => !text.is_empty(),
        _ => true,
    }
}

fn string_param(params: &Map<String, Value>, key: &str) -> Option<String> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(flag) => (if *flag { "True" } else { "False" }).to_string(),
        Value::Number(number) => number.to_string(),
        Value::Array(_) | Value::Object(_) => sd_core::contracts::python_str(value),
    }
}

/// `_range_for`: legacy coarse range from a requested period/start/end window.
pub fn range_for(params: &Map<String, Value>, today: NaiveDate) -> String {
    let allowed = [
        "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
    ];
    if let Some(period) = string_param(params, "period") {
        if allowed.contains(&period.as_str()) {
            return period;
        }
        if ["7d", "14d", "30d", "60d", "90d"].contains(&period.as_str()) {
            return if ["7d", "14d", "30d"].contains(&period.as_str()) {
                "1mo"
            } else {
                "3mo"
            }
            .to_string();
        }
    }
    let start = string_param(params, "start");
    let end = string_param(params, "end");
    if start.is_some() && end.is_some() {
        if let Some(start_date) = start
            .as_deref()
            .and_then(|text| NaiveDate::parse_from_str(text, "%Y-%m-%d").ok())
        {
            let days = (today - start_date).num_days().max(0);
            for (limit, value) in [
                (1, "1d"),
                (5, "5d"),
                (31, "1mo"),
                (93, "3mo"),
                (186, "6mo"),
                (366, "1y"),
                (732, "2y"),
                (1827, "5y"),
                (3653, "10y"),
            ] {
                if days <= limit {
                    return value.to_string();
                }
            }
        }
    }
    "1mo".to_string()
}

/// `_adjusted_values`: legacy chart adjusted-close list (two accepted shapes).
fn adjusted_values(payload: &Value) -> Option<Vec<Value>> {
    let value = payload.get("adjclose").cloned().unwrap_or(Value::Null);
    let value = match value {
        Value::Array(items) => {
            let first = items.first()?;
            if first.is_object() {
                first.get("adjclose").cloned().unwrap_or(Value::Null)
            } else {
                Value::Array(items)
            }
        }
        Value::Object(map) => map.get("adjclose").cloned().unwrap_or(Value::Null),
        other => other,
    };
    value.as_array().cloned()
}

fn reserve(policy: &mut dyn ReservePolicy) -> Result<(), GatewayRaise> {
    policy.reserve().map_err(|error| GatewayRaise {
        code: error
            .code
            .clone()
            .unwrap_or_else(|| "GatewayError".to_string()),
        message: error.message,
    })
}

fn now_iso(now: DateTime<Utc>) -> String {
    let nanos = now.timestamp_subsec_nanos();
    if nanos == 0 {
        now.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        now.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

/// Warning list for the legacy chart fallback (adjusted-basis omission disclosed).
fn fallback_warnings(adjusted_missing: bool, params: &Map<String, Value>) -> Vec<Value> {
    let mut warnings = vec![
        Value::String("Legacy chart fallback uses coarse range windows; date filtering does not prove complete requested coverage.".into()),
        Value::String("Legacy Yahoo source does not confirm unit or bar closure; existing gates remain in force.".into()),
    ];
    if adjusted_missing && string_param(params, "price_basis").as_deref() == Some("adjusted") {
        warnings.push(Value::String(
            "Legacy chart response omitted adjusted-close values; provider OHLC basis retained."
                .into(),
        ));
    }
    warnings
}

/// `_legacy_history`: one compatibility chart call mapped through the normalizer.
pub fn legacy_history(
    gateway: &dyn LegacyGateway,
    symbol: &str,
    params: &Map<String, Value>,
    policy: &mut dyn ReservePolicy,
    now: DateTime<Utc>,
) -> Result<Value, LegacyError> {
    let interval = string_param(params, "interval").unwrap_or_else(|| "1d".to_string());
    reserve(policy).map_err(|error| LegacyError::policy(&error.code, error.message))?;
    let range = range_for(params, now.date_naive());
    let raw = gateway.stock_kline_yahoo(symbol, &interval, &range, true)?;
    let (rows, meta) = if raw.is_object() {
        let rows = raw
            .get("bars")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let meta = raw
            .get("meta")
            .filter(|value| value.is_object())
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new()));
        (rows, meta)
    } else {
        (Vec::new(), Value::Object(Map::new()))
    };
    let adjusted = adjusted_values(&raw);
    let mut native_rows: Vec<Value> = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        if !row.is_object() {
            continue;
        }
        let mut item = row.as_object().cloned().unwrap_or_default();
        let stamp = row
            .get("timestamp_utc")
            .or_else(|| row.get("timestamp"))
            .or_else(|| row.get("date"))
            .cloned()
            .unwrap_or(Value::Null);
        item.insert("timestamp".into(), stamp);
        if let Some(adjusted) = &adjusted {
            if let Some(value) = adjusted.get(index) {
                item.insert("Adj Close".into(), value.clone());
            }
        }
        native_rows.push(Value::Object(item));
    }
    // Keep the compatibility fallback's normalized bars consistent with the native path:
    // the legacy chart response carries the same `events` block.
    let events = raw.get("events").cloned().unwrap_or(Value::Null);
    attach_chart_events(&mut native_rows, &meta, &events);
    let mut basis = string_param(params, "price_basis").unwrap_or_else(|| "provider".to_string());
    if basis != "provider" && basis != "adjusted" {
        basis = "provider".to_string();
    }
    if basis == "adjusted" && adjusted.is_none() {
        basis = "provider".to_string();
    }
    let normalized = normalize_history(
        &Value::Array(native_rows),
        &meta,
        &basis,
        Some(symbol),
        &interval,
    )
    .map_err(LegacyError::value)?;
    let mut bars = normalized
        .get("data")
        .and_then(|data| data.get("bars"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let start = string_param(params, "start");
    let end = string_param(params, "end");
    if start.is_some() || end.is_some() {
        bars.retain(|bar| {
            let date = bar.get("date").and_then(Value::as_str).unwrap_or("");
            (start.as_deref().map(|start| date >= start).unwrap_or(true))
                && (end.as_deref().map(|end| date < end).unwrap_or(true))
        });
        let dates: Vec<String> = bars
            .iter()
            .filter_map(|bar| {
                bar.get("date")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
            })
            .collect();
        let window = serde_json::json!({
            "start": dates.iter().min().cloned().map(Value::String).unwrap_or(Value::Null),
            "end": dates.iter().max().cloned().map(Value::String).unwrap_or(Value::Null),
            "selection_rule": "legacy range window filtered to requested dates; coverage may be incomplete",
        });
        let mut data = normalized
            .get("data")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        data.insert("bars".into(), Value::Array(bars.clone()));
        data.insert("history_window".into(), window);
        let mut envelope = Map::new();
        envelope.insert(
            "status".into(),
            Value::String(if bars.is_empty() { "empty" } else { "partial" }.into()),
        );
        envelope.insert("data".into(), Value::Object(data));
        envelope.insert(
            "actual_source".into(),
            Value::String("Yahoo Finance".into()),
        );
        envelope.insert("fetched_at_utc".into(), Value::String(now_iso(now)));
        envelope.insert(
            "source_symbol".into(),
            normalized
                .get("source_symbol")
                .cloned()
                .unwrap_or(Value::Null),
        );
        envelope.insert(
            "currency".into(),
            normalized.get("currency").cloned().unwrap_or(Value::Null),
        );
        envelope.insert("unit".into(), Value::Null);
        envelope.insert("frequency".into(), Value::String(interval.clone()));
        envelope.insert(
            "source_timestamp".into(),
            bars.last()
                .and_then(|bar| bar.get("timestamp_utc").cloned())
                .unwrap_or(Value::Null),
        );
        envelope.insert(
            "source_timestamp_kind".into(),
            Value::String("bar_label_not_trade_time".into()),
        );
        envelope.insert("last_bar_closed".into(), Value::Null);
        envelope.insert("adjustment".into(), Value::String(basis.clone()));
        envelope.insert(
            "adjustment_evidence".into(),
            normalized
                .get("adjustment_evidence")
                .cloned()
                .unwrap_or(Value::Null),
        );
        envelope.insert(
            "provider_library".into(),
            Value::String("legacy_compat".into()),
        );
        envelope.insert(
            "provider_version".into(),
            Value::String("stock-data gateway".into()),
        );
        envelope.insert("fallback_attempted".into(), Value::Bool(true));
        envelope.insert("fallback_used".into(), Value::Bool(true));
        envelope.insert(
            "fallback_route".into(),
            Value::String("stock_kline_yahoo".into()),
        );
        envelope.insert("fallback_reason".into(), Value::Null);
        let mut warnings = vec![
            Value::String("Legacy chart fallback uses coarse range windows; date filtering does not prove complete requested coverage.".into()),
            Value::String("Legacy Yahoo source does not confirm unit or bar closure; existing gates remain in force.".into()),
        ];
        if adjusted.is_none() && string_param(params, "price_basis").as_deref() == Some("adjusted")
        {
            warnings.push(Value::String("Legacy chart response omitted adjusted-close values; provider OHLC basis retained.".into()));
        }
        envelope.insert("warnings".into(), Value::Array(warnings));
        envelope.insert(
            "provenance".into(),
            serde_json::json!({"library": "legacy_compat", "route": "stock_kline_yahoo",
                               "raw_http_payload_captured": false}),
        );
        return Ok(Value::Object(envelope));
    }
    let source_timestamp = bars
        .last()
        .and_then(|bar| bar.get("timestamp_utc").cloned())
        .unwrap_or(Value::Null);
    Ok(serde_json::json!({
        "status": if bars.is_empty() { "empty" } else { "partial" },
        "data": normalized.get("data").cloned().unwrap_or(Value::Null),
        "actual_source": "Yahoo Finance",
        "fetched_at_utc": now_iso(now),
        "source_symbol": normalized.get("source_symbol").cloned().unwrap_or(Value::Null),
        "currency": normalized.get("currency").cloned().unwrap_or(Value::Null),
        "unit": Value::Null,
        "frequency": interval,
        "source_timestamp": source_timestamp,
        "source_timestamp_kind": "bar_label_not_trade_time",
        "last_bar_closed": Value::Null,
        "adjustment": basis,
        "adjustment_evidence": normalized.get("adjustment_evidence").cloned().unwrap_or(Value::Null),
        "provider_library": "legacy_compat",
        "provider_version": "stock-data gateway",
        "fallback_attempted": true,
        "fallback_used": true,
        "fallback_route": "stock_kline_yahoo",
        "fallback_reason": Value::Null,
        "warnings": fallback_warnings(adjusted.is_none(), params),
        "provenance": {"library": "legacy_compat", "route": "stock_kline_yahoo",
                       "raw_http_payload_captured": false},
    }))
}

/// One mapped compatibility route.
struct Route {
    name: &'static str,
    source: &'static str,
    args: Option<Map<String, Value>>,
    warnings: Vec<String>,
}

fn route_for(function: &str, params: &Map<String, Value>) -> Option<Route> {
    let symbol = params.get("symbol").cloned().unwrap_or(Value::Null);
    match function {
        "yahoo_history" | "yahoo_history_batch" => Some(Route {
            name: "stock_kline_yahoo",
            source: "Yahoo Finance",
            args: None,
            warnings: Vec::new(),
        }),
        "yahoo_quote" | "yahoo_profile" => {
            let modules = if function == "yahoo_quote" {
                [
                    "price",
                    "summaryDetail",
                    "financialData",
                    "defaultKeyStatistics",
                ]
                .to_vec()
            } else {
                ["assetProfile", "summaryProfile", "price"].to_vec()
            };
            Some(Route {
                name: "yahoo_quote_summary",
                source: "Yahoo Finance",
                args: Some(Map::from_iter([
                    ("symbol".to_string(), symbol),
                    (
                        "modules".to_string(),
                        Value::Array(modules.into_iter().map(Value::from).collect()),
                    ),
                ])),
                warnings: vec!["Legacy quoteSummary structure is returned without the yfinance field projection.".into()],
            })
        }
        "yahoo_financials" => {
            let frequency =
                string_param(params, "frequency").unwrap_or_else(|| "yearly".to_string());
            let mut warnings = Vec::new();
            if frequency == "trailing" || frequency == "ttm" {
                warnings.push(
                    "Legacy compatibility route cannot provide a trailing-twelve-month statement."
                        .into(),
                );
            }
            Some(Route {
                name: "financial_statements_yahoo",
                source: "Yahoo Finance",
                args: Some(Map::from_iter([
                    ("symbol".to_string(), symbol),
                    (
                        "quarterly".to_string(),
                        Value::Bool(frequency == "quarterly"),
                    ),
                ])),
                warnings,
            })
        }
        "yahoo_search" => {
            if params
                .get("lookup_type")
                .map(|value| !value.is_null())
                .unwrap_or(false)
            {
                return None;
            }
            Some(Route {
                name: "stock_search",
                source: "Eastmoney search",
                args: Some(Map::from_iter([
                    (
                        "keyword".to_string(),
                        params.get("query").cloned().unwrap_or(Value::Null),
                    ),
                    (
                        "count".to_string(),
                        params.get("count").cloned().unwrap_or(Value::from(8)),
                    ),
                ])),
                warnings: vec!["Search fallback uses the legacy Eastmoney search route.".into()],
            })
        }
        "yahoo_statistics" => Some(Route {
            name: "key_statistics",
            source: "Yahoo Finance",
            args: Some(Map::from_iter([("symbol".to_string(), symbol)])),
            warnings: Vec::new(),
        }),
        "yahoo_analysis" => Some(Route {
            name: "analyst_estimates",
            source: "Yahoo Finance",
            args: Some(Map::from_iter([("symbol".to_string(), symbol)])),
            warnings: Vec::new(),
        }),
        "yahoo_holders" => Some(Route {
            name: "institutional_holders",
            source: "Yahoo Finance",
            args: Some(Map::from_iter([("symbol".to_string(), symbol)])),
            warnings: Vec::new(),
        }),
        "yahoo_options" => Some(Route {
            name: "options_chain",
            source: "Yahoo Finance",
            args: None,
            warnings: vec![
                "Legacy options route supports U.S. tickers and Unix expiry values only.".into(),
            ],
        }),
        "yahoo_earnings" => Some(Route {
            name: "yahoo_quote_summary",
            source: "Yahoo Finance",
            args: Some(Map::from_iter([
                ("symbol".to_string(), symbol),
                (
                    "modules".to_string(),
                    serde_json::json!([
                        "calendarEvents",
                        "earnings",
                        "earningsHistory",
                        "earningsTrend"
                    ]),
                ),
            ])),
            warnings: vec![
                "Legacy quoteSummary earnings fields have no module-level completeness guarantee."
                    .into(),
            ],
        }),
        "yahoo_funds" => Some(Route {
            name: "yahoo_quote_summary",
            source: "Yahoo Finance",
            args: Some(Map::from_iter([
                ("symbol".to_string(), symbol),
                (
                    "modules".to_string(),
                    serde_json::json!([
                        "fundProfile",
                        "summaryProfile",
                        "topHoldings",
                        "fundPerformance"
                    ]),
                ),
            ])),
            warnings: vec![
                "Legacy quoteSummary fund modules may be unavailable for this ticker.".into(),
            ],
        }),
        _ => None,
    }
}

fn gateway_call_for(
    gateway: &dyn LegacyGateway,
    name: &str,
    args: &Map<String, Value>,
) -> Result<Value, LegacyError> {
    match name {
        "stock_kline_yahoo" => {
            let symbol = py_str(args.get("symbol").unwrap_or(&Value::Null));
            let interval = py_str(args.get("interval").unwrap_or(&Value::from("1d")));
            let range_ = py_str(args.get("range_").unwrap_or(&Value::from("6mo")));
            gateway.stock_kline_yahoo(
                &symbol,
                &interval,
                &range_,
                args.get("include_metadata")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            )
        }
        "yahoo_quote_summary" => {
            let symbol = py_str(args.get("symbol").unwrap_or(&Value::Null));
            let modules: Vec<String> = args
                .get("modules")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(py_str).collect())
                .unwrap_or_default();
            gateway.yahoo_quote_summary(&symbol, &modules)
        }
        "key_statistics" => {
            gateway.key_statistics(&py_str(args.get("symbol").unwrap_or(&Value::Null)))
        }
        "analyst_estimates" => {
            gateway.analyst_estimates(&py_str(args.get("symbol").unwrap_or(&Value::Null)))
        }
        "institutional_holders" => {
            gateway.institutional_holders(&py_str(args.get("symbol").unwrap_or(&Value::Null)))
        }
        "financial_statements_yahoo" => {
            let quarterly = args
                .get("quarterly")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            gateway.financial_statements_yahoo(
                &py_str(args.get("symbol").unwrap_or(&Value::Null)),
                quarterly,
            )
        }
        "options_chain" => {
            let symbol = py_str(args.get("symbol").unwrap_or(&Value::Null));
            let expiration = args.get("expiration").and_then(Value::as_i64);
            gateway.options_chain(&symbol, expiration)
        }
        "stock_search" => {
            let keyword = py_str(args.get("keyword").unwrap_or(&Value::Null));
            let count = args
                .get("count")
                .and_then(|value| {
                    value
                        .as_i64()
                        .or_else(|| value.as_f64().map(|float| float as i64))
                })
                .unwrap_or(8);
            gateway.stock_search(&keyword, count)
        }
        other => Err(LegacyError::provider(format!(
            "unknown legacy route {other}"
        ))),
    }
}

/// `_call_legacy`: dispatch one capability to its compatibility route.
pub fn call_legacy(
    gateway: &dyn LegacyGateway,
    function: &str,
    params: &Map<String, Value>,
    policy: &mut dyn ReservePolicy,
    now: DateTime<Utc>,
) -> Result<(Value, &'static str, &'static str, Vec<String>), LegacyError> {
    match function {
        "yahoo_history" => {
            let symbol = py_str(params.get("symbol").unwrap_or(&Value::Null));
            let data = legacy_history(gateway, &symbol, params, policy, now)?;
            Ok((data, "Yahoo Finance", "stock_kline_yahoo", Vec::new()))
        }
        "yahoo_history_batch" => {
            let symbols: Vec<String> = params
                .get("symbols")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(py_str).collect())
                .unwrap_or_default();
            let mut results: Map<String, Value> = Map::new();
            for symbol in symbols {
                match legacy_history(gateway, &symbol, params, &mut *policy, now) {
                    Ok(data) => {
                        results.insert(symbol, data);
                    }
                    Err(error) => {
                        if let Some(code) = error.code.clone() {
                            if POLICY_ERROR_CODES.contains(&code.as_str()) {
                                return Err(error);
                            }
                        }
                        results.insert(
                            symbol,
                            serde_json::json!({
                                "status": "failed",
                                "error": {"category": error.exception_type},
                            }),
                        );
                    }
                }
            }
            let any_data = results
                .values()
                .any(|item| has_data(item.get("data").unwrap_or(&Value::Null)));
            Ok((
                serde_json::json!({
                    "status": if any_data { "partial" } else { "empty" },
                    "data": {"symbols": results},
                }),
                "Yahoo Finance",
                "stock_kline_yahoo",
                vec![
                    "Legacy batch fallback performs bounded per-symbol compatibility calls.".into(),
                ],
            ))
        }
        "yahoo_options" => {
            let symbol = py_str(params.get("symbol").unwrap_or(&Value::Null));
            let expiration = string_param(params, "expiration");
            let data = match expiration {
                None => {
                    reserve(policy)
                        .map_err(|error| LegacyError::policy(&error.code, error.message))?;
                    gateway.options_chain(&symbol, None)?
                }
                Some(expiration) => {
                    let target =
                        NaiveDate::parse_from_str(&expiration, "%Y-%m-%d").map_err(|_| {
                            LegacyError::value("expiration has an invalid date format or value.")
                        })?;
                    reserve(policy)
                        .map_err(|error| LegacyError::policy(&error.code, error.message))?;
                    let listing = gateway.options_chain(&symbol, None)?;
                    let expiries = listing
                        .get("expiration_dates")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let matching = expiries.iter().find(|value| {
                        let seconds = value
                            .as_i64()
                            .or_else(|| value.as_f64().map(|float| float as i64));
                        seconds
                            .and_then(|seconds| Utc.timestamp_opt(seconds, 0).single())
                            .map(|stamp| stamp.date_naive() == target)
                            .unwrap_or(false)
                    });
                    let Some(matched) = matching else {
                        return Err(LegacyError::value(
                            "Legacy options route did not list the requested expiration.",
                        ));
                    };
                    reserve(policy)
                        .map_err(|error| LegacyError::policy(&error.code, error.message))?;
                    gateway.options_chain(&symbol, matched.as_i64())?
                }
            };
            Ok((
                data,
                "Yahoo Finance",
                "options_chain",
                vec!["Legacy option-chain field names and coverage differ from yfinance.".into()],
            ))
        }
        _ => {
            let Some(route) = route_for(function, params) else {
                return Err(LegacyError::provider(
                    "No legacy compatibility route is registered for this yfinance capability.",
                ));
            };
            let Some(args) = route.args else {
                return Err(LegacyError::provider(
                    "Legacy compatibility parameters cannot be mapped safely.",
                ));
            };
            reserve(policy).map_err(|error| LegacyError::policy(&error.code, error.message))?;
            let data = gateway_call_for(gateway, route.name, &args)?;
            Ok((data, route.source, route.name, route.warnings))
        }
    }
}

fn error_envelope(
    now: DateTime<Utc>,
    category: &str,
    exception_type: &str,
    message: &str,
    route: Option<&str>,
    reason: &Value,
    warnings: Vec<Value>,
) -> Value {
    serde_json::json!({
        "status": "error",
        "data": Value::Null,
        "actual_source": "Yahoo Finance",
        "fetched_at_utc": now_iso(now),
        "fallback_attempted": true,
        "fallback_used": false,
        "fallback_route": route.map(Value::from).unwrap_or(Value::Null),
        "fallback_reason": reason.clone(),
        "error": {"category": category, "exception_type": exception_type, "message": message},
        "warnings": warnings,
    })
}

/// `fallback`: try one registered compatibility route after a provider failure.
#[allow(clippy::too_many_arguments)]
pub fn fallback(
    gateway: &dyn LegacyGateway,
    function: &str,
    params: &Map<String, Value>,
    reason: &Value,
    policy: &mut dyn ReservePolicy,
    now: DateTime<Utc>,
    circuit: Option<(&str, &str)>,
) -> Result<Value, GatewayRaise> {
    if let Some((code, _message)) = circuit {
        return Ok(error_envelope(
            now,
            code,
            "JobCircuitOpen",
            "Yahoo job circuit is open; legacy fallback is not permitted.",
            None,
            reason,
            vec![Value::String(
                "Automatic legacy fallback was blocked by an open Yahoo job circuit.".into(),
            )],
        ));
    }
    let special = ["yahoo_history", "yahoo_history_batch", "yahoo_options"];
    if !special.contains(&function) && route_for(function, params).is_none() {
        return Ok(error_envelope(
            now,
            "NoLegacyRoute",
            "LookupError",
            "No semantically equivalent legacy compatibility route is registered.",
            None,
            reason,
            vec![Value::String(
                "Automatic legacy fallback was evaluated; no equivalent route is available.".into(),
            )],
        ));
    }
    match call_legacy(gateway, function, params, policy, now) {
        Ok((data, source, route, warnings)) => {
            if data.get("fallback_used").and_then(Value::as_bool) == Some(true) {
                let mut result = data;
                if let Some(object) = result.as_object_mut() {
                    object.insert("fallback_reason".into(), reason.clone());
                    let mut existing = object
                        .get("warnings")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    existing.extend(
                        warnings
                            .iter()
                            .map(|warning| Value::String(warning.clone())),
                    );
                    object.insert("warnings".into(), Value::Array(existing));
                }
                return Ok(result);
            }
            let (status, payload) =
                if data.is_object() && data.get("status").is_some() && data.get("data").is_some() {
                    let status = data.get("status").cloned().unwrap_or(Value::Null);
                    let payload = data.get("data").cloned().unwrap_or(Value::Null);
                    let resolved = if status.as_str() == Some("empty") || payload.is_null() {
                        "empty"
                    } else {
                        "partial"
                    };
                    (resolved, payload)
                } else if has_data(&data) {
                    ("partial", data.clone())
                } else {
                    ("empty", data.clone())
                };
            let mut all_warnings = vec![Value::String(
                "yfinance request failed; legacy compatibility route was used.".into(),
            )];
            all_warnings.extend(warnings.into_iter().map(Value::String));
            Ok(serde_json::json!({
                "status": status,
                "data": if status == "empty" { Value::Null } else { payload },
                "actual_source": source,
                "fetched_at_utc": now_iso(now),
                "provider_library": "legacy_compat",
                "provider_version": "stock-data gateway",
                "fallback_attempted": true,
                "fallback_used": true,
                "fallback_route": route,
                "fallback_reason": reason.clone(),
                "warnings": all_warnings,
                "provenance": {"library": "legacy_compat", "route": route,
                               "raw_http_payload_captured": false},
            }))
        }
        Err(error) => {
            if let Some(code) = &error.code {
                if POLICY_ERROR_CODES.contains(&code.as_str()) {
                    return Err(GatewayRaise {
                        code: code.clone(),
                        message: error.message,
                    });
                }
            }
            let category = if error.exception_type == "LookupError" {
                "NoLegacyRoute"
            } else {
                "LegacyFallbackFailed"
            };
            let attempted_route = if ["yahoo_history", "yahoo_history_batch"].contains(&function) {
                "stock_kline_yahoo"
            } else if function == "yahoo_options" {
                "options_chain"
            } else {
                route_for(function, params)
                    .map(|route| route.name)
                    .unwrap_or("")
            };
            Ok(error_envelope(
                now,
                category,
                &error.exception_type,
                "Legacy compatibility fallback did not return data; details redacted.",
                Some(attempted_route).filter(|name| !name.is_empty()),
                reason,
                vec![Value::String(
                    "yfinance request failed; automatic legacy fallback was attempted.".into(),
                )],
            ))
        }
    }
}

#[allow(dead_code)]
fn _keep_datelike(now: DateTime<Utc>) -> i32 {
    now.year()
}
