//! Gateway glue for the 20 Yahoo functions: 7 legacy routes and 13 provider capabilities.
//!
//! The native route layer issues Yahoo HTTP directly through the injected transport; a
//! capability failure falls back to the seven compatibility routes unless the provider
//! reports a terminal policy error. `yahoo_screen` and `repair=true` have no equivalent
//! route and fail explicitly with `UnsupportedCapability` (no fabricated fallback envelope).

use std::path::PathBuf;

use chrono::{NaiveDate, TimeZone, Utc};
use serde_json::{Map, Value};

use crate::http::HttpTransport;
use crate::transport::{JobContext, JobState, ProviderError, ProviderErrorKind};
use crate::yahoo_compat::{range_for, LegacyError, LegacyGateway};
use crate::yahoo_legacy::{
    analyst_estimates, attach_chart_events, financial_statements_yahoo, institutional_holders,
    key_statistics, options_chain, stock_kline_yahoo, stock_kline_yahoo_with_prepost,
    yahoo_quote_summary,
};
use crate::yahoo_provider::{
    CacheDirMode, CompatFallback, GatewayError, TransportPolicy, YahooNative, YahooProvider,
};
use crate::yahoo_session::{get_yahoo_session, YahooSession};
use crate::yfinance_normalize::normalize_history;

/// All Yahoo-backed gateway functions.
pub const FUNCTIONS: [&str; 20] = [
    "yahoo_history",
    "yahoo_history_batch",
    "yahoo_quote",
    "yahoo_profile",
    "yahoo_financials",
    "yahoo_search",
    "yahoo_statistics",
    "yahoo_analysis",
    "yahoo_holders",
    "yahoo_options",
    "yahoo_earnings",
    "yahoo_funds",
    "yahoo_screen",
    "stock_kline_yahoo",
    "yahoo_quote_summary",
    "key_statistics",
    "analyst_estimates",
    "institutional_holders",
    "financial_statements_yahoo",
    "options_chain",
];

const LEGACY_FUNCTIONS: [&str; 7] = [
    "stock_kline_yahoo",
    "yahoo_quote_summary",
    "key_statistics",
    "analyst_estimates",
    "institutional_holders",
    "financial_statements_yahoo",
    "options_chain",
];

/// True when the gateway should route this function to a Yahoo implementation.
pub fn is_function(function: &str) -> bool {
    FUNCTIONS.contains(&function)
}

/// True for the 13 yfinance-first provider capabilities.
pub fn is_provider(function: &str) -> bool {
    FUNCTIONS.contains(&function) && !LEGACY_FUNCTIONS.contains(&function)
}

/// True for the pre-yfinance compatibility functions.
pub fn is_legacy(function: &str) -> bool {
    LEGACY_FUNCTIONS.contains(&function)
}

fn iso_now() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn envelope(status: &str, data: Value, name: &str) -> Value {
    serde_json::json!({
        "status": status,
        "data": data,
        "actual_source": "Yahoo Finance",
        "fetched_at_utc": iso_now(),
        "fallback_used": false,
        "provenance": {"library": "native_rust", "route": name,
                       "archive_kind": "normalized_native_result_not_wire_payload"},
    })
}

fn nonempty_status(data: &Value) -> &'static str {
    match data {
        Value::Null => "empty",
        Value::Array(items) if items.is_empty() => "empty",
        Value::Object(map) if map.is_empty() => "empty",
        _ => "success",
    }
}

/// Extract the adjusted-close series from the legacy chart result.
///
/// The wire shape is `indicators.adjclose[0].adjclose` (a list of
/// `{"adjclose": [...]}` objects); the legacy `stock_kline_yahoo` passes that list
/// through under `adjclose`. A bare numeric array is also accepted for fixtures.
fn adjusted_close_series(raw: &Value) -> Option<Vec<Value>> {
    let value = raw.get("adjclose")?;
    if let Some(rows) = value.as_array() {
        for row in rows {
            if let Some(series) = row.get("adjclose").and_then(Value::as_array) {
                return Some(series.clone());
            }
        }
        if rows.iter().all(Value::is_number) {
            return Some(rows.clone());
        }
        return None;
    }
    value.get("adjclose").and_then(Value::as_array).cloned()
}

fn string_param(params: &Map<String, Value>, key: &str) -> Option<String> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn int_param(params: &Map<String, Value>, key: &str, default: i64) -> i64 {
    params
        .get(key)
        .and_then(|value| {
            value
                .as_i64()
                .or_else(|| value.as_f64().map(|float| float as i64))
        })
        .unwrap_or(default)
}

/// Native Yahoo capability routes.
pub struct NativeTransport<'a> {
    transport: &'a dyn HttpTransport,
    job: JobState,
    session: Option<YahooSession>,
}

impl<'a> NativeTransport<'a> {
    /// Build the native route layer for one gateway invocation. All routes share `job`.
    pub fn new(transport: &'a dyn HttpTransport, job: JobState) -> Self {
        Self {
            transport,
            job,
            session: None,
        }
    }

    fn session(&mut self) -> Result<&YahooSession, GatewayError> {
        if self.session.is_none() {
            let session = get_yahoo_session(self.transport, &mut self.job.borrow_mut())
                .map_err(native_gateway_error)?;
            self.session = Some(session);
        }
        Ok(self.session.as_ref().expect("session"))
    }

    fn history(&mut self, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        let symbol =
            string_param(params, "symbol").ok_or_else(|| GatewayError::invalid("symbol"))?;
        let interval = string_param(params, "interval").unwrap_or_else(|| "1d".to_string());
        let range = range_for(params, Utc::now().date_naive());
        // `repair` is a yfinance-library re-download/comparison; the chart wire API has no
        // equivalent, so it is rejected explicitly instead of being accepted and ignored.
        if params
            .get("repair")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Err(GatewayError::new(
                "UnsupportedCapability",
                "repair=true is not available on the native chart route; the yfinance repair comparison is a development reference only",
            ));
        }
        let prepost = params
            .get("prepost")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let raw = stock_kline_yahoo_with_prepost(
            Some(&Value::String(symbol.clone())),
            Some(&Value::String(interval.clone())),
            Some(&Value::String(range)),
            true,
            prepost,
            self.transport,
            &mut self.job.borrow_mut(),
        )
        .map_err(native_gateway_error)?;
        let rows = raw
            .get("bars")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let meta = raw
            .get("meta")
            .filter(|value| value.is_object())
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));
        let adjusted = adjusted_close_series(&raw);
        let mut native_rows: Vec<Value> = Vec::new();
        for (index, row) in rows.iter().enumerate() {
            let Some(mut item) = row.as_object().cloned() else {
                continue;
            };
            item.insert(
                "timestamp".into(),
                row.get("timestamp_utc")
                    .or_else(|| row.get("timestamp"))
                    .or_else(|| row.get("date"))
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            if let Some(adjusted) = &adjusted {
                if let Some(value) = adjusted.get(index) {
                    item.insert("Adj Close".into(), value.clone());
                }
            }
            native_rows.push(Value::Object(item));
        }
        // The chart `events` block carries dividends/splits/capital gains; normalize_history
        // reads them as per-row action columns, so an actionless history must not happen.
        let events = raw.get("events").cloned().unwrap_or(Value::Null);
        attach_chart_events(&mut native_rows, &meta, &events);
        let basis = string_param(params, "price_basis").unwrap_or_else(|| "provider".to_string());
        let mut normalized = normalize_history(
            &Value::Array(native_rows),
            &meta,
            if basis == "adjusted" {
                "adjusted"
            } else {
                "provider"
            },
            Some(&symbol),
            &interval,
        )
        .map_err(GatewayError::invalid)?;
        let start = string_param(params, "start");
        let end = string_param(params, "end");
        if start.is_some() || end.is_some() {
            let mut data = normalized
                .get("data")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut bars = data
                .get("bars")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            bars.retain(|bar| {
                let date = bar.get("date").and_then(Value::as_str).unwrap_or("");
                start.as_deref().map(|start| date >= start).unwrap_or(true)
                    && end.as_deref().map(|end| date < end).unwrap_or(true)
            });
            let dates: Vec<String> = bars
                .iter()
                .filter_map(|bar| {
                    bar.get("date")
                        .and_then(Value::as_str)
                        .map(ToString::to_string)
                })
                .collect();
            data.insert("bars".into(), Value::Array(bars));
            data.insert(
                "history_window".into(),
                serde_json::json!({
                    "start": dates.iter().min().cloned().map(Value::String).unwrap_or(Value::Null),
                    "end": dates.iter().max().cloned().map(Value::String).unwrap_or(Value::Null),
                    "selection_rule": "native range filtered to requested dates; coverage may be incomplete",
                }),
            );
            if let Some(object) = normalized.as_object_mut() {
                object.insert("data".into(), Value::Object(data));
            }
        }
        let data = normalized.get("data").cloned().unwrap_or(Value::Null);
        let bars = data
            .get("bars")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        // The normalizer marks adjusted views with ratio gaps as `partial`; the gateway
        // envelope must not upgrade that to `success`.
        let normalized_status = normalized
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("success");
        let status = if bars.is_empty() {
            "empty"
        } else {
            normalized_status
        };
        let mut result = envelope(status, data, "yahoo_history");
        if let Some(object) = result.as_object_mut() {
            // Python `_history` merges the normalizer envelope (minus `data`) into the
            // gateway envelope, so the collector can persist adjustment/source evidence.
            if let Some(normalized_object) = normalized.as_object() {
                for (key, value) in normalized_object {
                    if key != "data" && key != "status" {
                        object.insert(key.clone(), value.clone());
                    }
                }
            }
            object.insert(
                "adjustment".into(),
                Value::String(
                    string_param(params, "price_basis").unwrap_or_else(|| "provider".to_string()),
                ),
            );
            object.insert(
                "source_timestamp_kind".into(),
                Value::String("bar_label_not_trade_time".into()),
            );
            let last_label = bars
                .last()
                .and_then(|bar| {
                    bar.get("timestamp_utc")
                        .or_else(|| bar.get("timestamp"))
                        .or_else(|| bar.get("date"))
                })
                .cloned()
                .unwrap_or(Value::Null);
            if !last_label.is_null() {
                object.insert("source_timestamp".into(), last_label);
            }
            object.insert("last_bar_closed".into(), Value::Null);
            object.insert(
                "native_payload_kind".into(),
                Value::String("native_chart_normalized_from_wire".into()),
            );
            object.insert(
                "wire_route".into(),
                Value::String(format!(
                    "query2.finance.yahoo.com/v8/finance/chart/{symbol}"
                )),
            );
            object.insert(
                "request_options".into(),
                serde_json::json!({"prepost": prepost, "repair": false}),
            );
            object.insert("history".into(), normalized);
        }
        Ok(result)
    }

    fn batch(&mut self, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        let symbols = params
            .get("symbols")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut results: Map<String, Value> = Map::new();
        for symbol in symbols {
            let Some(name) = symbol.as_str() else {
                continue;
            };
            let mut single = params.clone();
            single.remove("symbols");
            single.insert("symbol".into(), Value::String(name.to_string()));
            match self.history(&single) {
                Ok(envelope) => {
                    // Python's batch result is the full normalized history result: status,
                    // bars under `data`, and adjustment/source evidence at the top level.
                    let mut symbol_result = envelope.clone();
                    if let Some(object) = symbol_result.as_object_mut() {
                        object.remove("history");
                        object.remove("actual_source");
                        object.remove("fetched_at_utc");
                        object.remove("fallback_used");
                        object.remove("provenance");
                    }
                    results.insert(name.to_string(), symbol_result);
                }
                Err(error) => {
                    results.insert(
                        name.to_string(),
                        serde_json::json!({"status": "failed", "error": {"category": error.code}}),
                    );
                }
            }
        }
        let ok = results
            .values()
            .filter(|value| {
                matches!(
                    value.get("status").and_then(Value::as_str),
                    Some("success") | Some("partial")
                )
            })
            .count();
        let complete = !results.is_empty()
            && results
                .values()
                .all(|value| value.get("status").and_then(Value::as_str) == Some("success"));
        let status = if results.is_empty() {
            "empty"
        } else if complete {
            "success"
        } else if ok > 0 {
            "partial"
        } else {
            "empty"
        };
        Ok(envelope(
            status,
            serde_json::json!({"symbols": results}),
            "yahoo_history_batch",
        ))
    }

    fn quote_summary_modules(
        &mut self,
        params: &Map<String, Value>,
        modules: &[&str],
        name: &str,
    ) -> Result<Value, GatewayError> {
        let symbol =
            string_param(params, "symbol").ok_or_else(|| GatewayError::invalid("symbol"))?;
        let session = self.session()?.clone();
        let modules: Vec<Value> = modules.iter().map(|module| Value::from(*module)).collect();
        let data = yahoo_quote_summary(
            Some(&Value::String(symbol)),
            Some(&Value::Array(modules)),
            &session,
            self.transport,
            &mut self.job.borrow_mut(),
        )
        .map_err(native_gateway_error)?;
        let status = nonempty_status(&data);
        Ok(envelope(status, data, name))
    }

    fn options(&mut self, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        let symbol =
            string_param(params, "symbol").ok_or_else(|| GatewayError::invalid("symbol"))?;
        let expiration = string_param(params, "expiration");
        let session = self.session()?.clone();
        let mut unix: Option<i64> = None;
        if let Some(expiration) = expiration {
            let target = NaiveDate::parse_from_str(&expiration, "%Y-%m-%d").map_err(|_| {
                GatewayError::invalid("expiration has an invalid date format or value.")
            })?;
            let listing = options_chain(
                Some(&Value::String(symbol.clone())),
                None,
                &session,
                self.transport,
                &mut self.job.borrow_mut(),
            )
            .map_err(native_gateway_error)?;
            let matched = listing
                .get("expiration_dates")
                .and_then(Value::as_array)
                .and_then(|dates| {
                    dates.iter().find(|value| {
                        value
                            .as_i64()
                            .and_then(|seconds| Utc.timestamp_opt(seconds, 0).single())
                            .map(|stamp| stamp.date_naive() == target)
                            .unwrap_or(false)
                    })
                })
                .and_then(Value::as_i64);
            let Some(matched) = matched else {
                return Err(GatewayError::new(
                    "DataUnavailable",
                    "No listed expiry matches the requested expiration.",
                ));
            };
            unix = Some(matched);
        }
        let data = options_chain(
            Some(&Value::String(symbol)),
            unix.map(Value::from).as_ref(),
            &session,
            self.transport,
            &mut self.job.borrow_mut(),
        )
        .map_err(native_gateway_error)?;
        Ok(envelope(nonempty_status(&data), data, "yahoo_options"))
    }

    fn financials(&mut self, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        let symbol =
            string_param(params, "symbol").ok_or_else(|| GatewayError::invalid("symbol"))?;
        let frequency = string_param(params, "frequency").unwrap_or_else(|| "yearly".to_string());
        if frequency == "trailing" || frequency == "ttm" {
            return Err(GatewayError::new(
                "UnsupportedCapability",
                "Trailing financial statements are not available on the native route",
            ));
        }
        let session = self.session()?.clone();
        let data = financial_statements_yahoo(
            Some(&Value::String(symbol)),
            Some(&Value::Bool(frequency == "quarterly")),
            &session,
            self.transport,
            &mut self.job.borrow_mut(),
        )
        .map_err(native_gateway_error)?;
        Ok(envelope(nonempty_status(&data), data, "yahoo_financials"))
    }

    fn search(&mut self, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        let query = string_param(params, "query").ok_or_else(|| GatewayError::invalid("query"))?;
        let count = int_param(params, "count", 8);
        let session = self.session()?.clone();
        let response = session
            .get(
                self.transport,
                &mut self.job.borrow_mut(),
                "https://query2.finance.yahoo.com/v1/finance/search",
                &[
                    ("q", query),
                    ("quotesCount", count.to_string()),
                    ("newsCount", "0".to_string()),
                ],
                15.0,
            )
            .map_err(native_gateway_error)?;
        response.raise_for_status().map_err(native_gateway_error)?;
        let document = response.json().map_err(native_gateway_error)?;
        let quotes: Vec<Value> = document
            .get("quotes")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|item| {
                        serde_json::json!({
                            "symbol": item.get("symbol").cloned().unwrap_or(Value::Null),
                            "name": item.get("shortname")
                                .or_else(|| item.get("longname"))
                                .cloned()
                                .unwrap_or(Value::Null),
                            "exchange": item.get("exchange").cloned().unwrap_or(Value::Null),
                            "quote_type": item.get("quoteType").cloned().unwrap_or(Value::Null),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        let data = serde_json::json!({"quotes": quotes, "total": quotes.len()});
        Ok(envelope(nonempty_status(&data), data, "yahoo_search"))
    }
}

impl YahooNative for NativeTransport<'_> {
    fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        match name {
            "yahoo_history" => self.history(params),
            "yahoo_history_batch" => self.batch(params),
            "yahoo_quote" => self.quote_summary_modules(
                params,
                &[
                    "price",
                    "summaryDetail",
                    "financialData",
                    "defaultKeyStatistics",
                ],
                "yahoo_quote",
            ),
            "yahoo_profile" => self.quote_summary_modules(
                params,
                &["assetProfile", "summaryProfile", "price"],
                "yahoo_profile",
            ),
            "yahoo_financials" => self.financials(params),
            "yahoo_search" => self.search(params),
            "yahoo_statistics" => {
                let symbol = string_param(params, "symbol")
                    .ok_or_else(|| GatewayError::invalid("symbol"))?;
                let session = self.session()?.clone();
                let data = key_statistics(
                    Some(&Value::String(symbol)),
                    &session,
                    self.transport,
                    &mut self.job.borrow_mut(),
                )
                .map_err(native_gateway_error)?;
                Ok(envelope(nonempty_status(&data), data, "yahoo_statistics"))
            }
            "yahoo_analysis" => {
                let symbol = string_param(params, "symbol")
                    .ok_or_else(|| GatewayError::invalid("symbol"))?;
                let session = self.session()?.clone();
                let data = analyst_estimates(
                    Some(&Value::String(symbol)),
                    &session,
                    self.transport,
                    &mut self.job.borrow_mut(),
                )
                .map_err(native_gateway_error)?;
                Ok(envelope(nonempty_status(&data), data, "yahoo_analysis"))
            }
            "yahoo_holders" => {
                let symbol = string_param(params, "symbol")
                    .ok_or_else(|| GatewayError::invalid("symbol"))?;
                let session = self.session()?.clone();
                let data = institutional_holders(
                    Some(&Value::String(symbol)),
                    &session,
                    self.transport,
                    &mut self.job.borrow_mut(),
                )
                .map_err(native_gateway_error)?;
                Ok(envelope(nonempty_status(&data), data, "yahoo_holders"))
            }
            "yahoo_options" => self.options(params),
            "yahoo_earnings" => self.quote_summary_modules(
                params,
                &[
                    "calendarEvents",
                    "earnings",
                    "earningsHistory",
                    "earningsTrend",
                ],
                "yahoo_earnings",
            ),
            "yahoo_funds" => self.quote_summary_modules(
                params,
                &[
                    "fundProfile",
                    "summaryProfile",
                    "topHoldings",
                    "fundPerformance",
                ],
                "yahoo_funds",
            ),
            "yahoo_screen" => Err(GatewayError::new(
                "UnsupportedCapability",
                "Yahoo screener route is not implemented natively",
            )),
            other => Err(GatewayError::new(
                "UnsupportedCapability",
                format!("Unregistered Yahoo capability {other}"),
            )),
        }
    }
}

/// Compatibility gateway over the seven legacy routes.
///
/// The provider's fallback layer only needs `&self`, so the mutable transport context and
/// the lazily bootstrapped session live behind `RefCell` (one session per invocation).
pub struct TransportLegacy<'a> {
    transport: &'a dyn HttpTransport,
    job: JobState,
    session: std::cell::RefCell<Option<YahooSession>>,
}

impl<'a> TransportLegacy<'a> {
    /// Build the compatibility layer for one gateway invocation. It shares the same job
    /// budget/deadline as the native routes.
    pub fn new(transport: &'a dyn HttpTransport, job: JobState) -> Self {
        Self {
            transport,
            job,
            session: std::cell::RefCell::new(None),
        }
    }

    fn session(&self) -> Result<YahooSession, LegacyError> {
        if self.session.borrow().is_none() {
            let session = get_yahoo_session(self.transport, &mut self.job.borrow_mut())
                .map_err(legacy_error)?;
            *self.session.borrow_mut() = Some(session);
        }
        Ok(self.session.borrow().clone().expect("session"))
    }
}

fn legacy_error(error: ProviderError) -> LegacyError {
    LegacyError {
        code: None,
        exception_type: error.kind.error_type().to_string(),
        message: error.message,
    }
}

impl LegacyGateway for TransportLegacy<'_> {
    fn stock_kline_yahoo(
        &self,
        symbol: &str,
        interval: &str,
        range_: &str,
        include_metadata: bool,
    ) -> Result<Value, LegacyError> {
        let mut context = self.job.borrow_mut();
        stock_kline_yahoo(
            Some(&Value::String(symbol.to_string())),
            Some(&Value::String(interval.to_string())),
            Some(&Value::String(range_.to_string())),
            include_metadata,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn yahoo_quote_summary(&self, symbol: &str, modules: &[String]) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let modules: Vec<Value> = modules
            .iter()
            .map(|module| Value::from(module.clone()))
            .collect();
        let mut context = self.job.borrow_mut();
        yahoo_quote_summary(
            Some(&Value::String(symbol.to_string())),
            Some(&Value::Array(modules)),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn key_statistics(&self, symbol: &str) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        key_statistics(
            Some(&Value::String(symbol.to_string())),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn analyst_estimates(&self, symbol: &str) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        analyst_estimates(
            Some(&Value::String(symbol.to_string())),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn institutional_holders(&self, symbol: &str) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        institutional_holders(
            Some(&Value::String(symbol.to_string())),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn financial_statements_yahoo(
        &self,
        symbol: &str,
        quarterly: bool,
    ) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        financial_statements_yahoo(
            Some(&Value::String(symbol.to_string())),
            Some(&Value::Bool(quarterly)),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn options_chain(&self, symbol: &str, expiration: Option<i64>) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        options_chain(
            Some(&Value::String(symbol.to_string())),
            expiration.map(Value::from).as_ref(),
            &session,
            self.transport,
            &mut context,
        )
        .map_err(legacy_error)
    }

    fn stock_search(&self, keyword: &str, count: i64) -> Result<Value, LegacyError> {
        let session = self.session()?;
        let mut context = self.job.borrow_mut();
        let response = session
            .get(
                self.transport,
                &mut context,
                "https://query2.finance.yahoo.com/v1/finance/search",
                &[
                    ("q", keyword.to_string()),
                    ("quotesCount", count.to_string()),
                ],
                15.0,
            )
            .map_err(legacy_error)?;
        response.raise_for_status().map_err(legacy_error)?;
        response.json().map_err(legacy_error)
    }
}

fn map_gateway_error(error: GatewayError) -> (String, String) {
    (error.code, error.message)
}

/// Gateway error for one native-route transport failure.
///
/// The reference implementation (`yfinance_provider.py`) turns HTTP 429 into a terminal
/// `RateLimited` and HTTP 403 into a terminal `AccessDenied`: both open the job circuit and
/// no legacy fallback is attempted. Status errors are recognised from the established
/// `raise_for_status` message prefix so the exception class of every other failure is kept.
fn native_gateway_error(error: ProviderError) -> GatewayError {
    let code = match error.kind {
        ProviderErrorKind::HttpError if error.message.starts_with("429 ") => "RateLimited",
        ProviderErrorKind::HttpError if error.message.starts_with("403 ") => "AccessDenied",
        _ => error.kind.error_type(),
    };
    GatewayError::new(code, error.message)
}

/// Execute one of the 13 provider capabilities with an explicit shared job budget.
pub fn call_with_job(
    function: &str,
    params: &Map<String, Value>,
    cache_dir: Option<PathBuf>,
    transport: &dyn HttpTransport,
    job: &JobState,
) -> Result<Value, (String, String)> {
    let mut filtered = Map::new();
    for (key, value) in params {
        if !value.is_null() {
            filtered.insert(key.clone(), value.clone());
        }
    }
    let mode = CacheDirMode::resolve(cache_dir);
    let legacy = TransportLegacy::new(transport, job.clone());
    let mut fallback = CompatFallback {
        gateway: &legacy,
        now: Utc::now(),
    };
    let mut native = NativeTransport::new(transport, job.clone());
    let mut provider = YahooProvider::new(
        mode,
        TransportPolicy::from_job(job.clone()),
        &mut native,
        &mut fallback,
    );
    provider
        .call(function, &filtered)
        .map_err(map_gateway_error)
}

/// Execute one capability as its own bounded job (single gateway CLI call).
pub fn call(
    function: &str,
    params: &Map<String, Value>,
    cache_dir: Option<PathBuf>,
    transport: &dyn HttpTransport,
    _context: &mut JobContext,
) -> Result<Value, (String, String)> {
    let timeout = params
        .get("timeout")
        .and_then(Value::as_f64)
        .filter(|value| *value > 0.0)
        .unwrap_or(15.0);
    let job = JobState::new(JobContext::new(
        timeout,
        120.0,
        30,
        crate::transport::MAX_RESPONSE_BYTES,
    ));
    call_with_job(function, params, cache_dir, transport, &job)
}

/// Execute one of the seven legacy functions through the compatibility gateway.
pub fn call_legacy(
    function: &str,
    params: &Map<String, Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let session = get_yahoo_session(transport, context)?;
    let empty = Map::new();
    let args = if params.is_empty() { &empty } else { params };
    let symbol = args.get("symbol");
    match function {
        "stock_kline_yahoo" => stock_kline_yahoo(
            symbol,
            args.get("interval"),
            args.get("range_"),
            args.get("include_metadata")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            transport,
            context,
        ),
        "yahoo_quote_summary" => {
            yahoo_quote_summary(symbol, args.get("modules"), &session, transport, context)
        }
        "key_statistics" => key_statistics(symbol, &session, transport, context),
        "analyst_estimates" => analyst_estimates(symbol, &session, transport, context),
        "institutional_holders" => institutional_holders(symbol, &session, transport, context),
        "financial_statements_yahoo" => {
            financial_statements_yahoo(symbol, args.get("quarterly"), &session, transport, context)
        }
        "options_chain" => {
            options_chain(symbol, args.get("expiration"), &session, transport, context)
        }
        other => Err(ProviderError::provider(format!(
            "unknown Yahoo compatibility function {other}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn status_error(code: u16, reason: &str) -> ProviderError {
        ProviderError::new(
            ProviderErrorKind::HttpError,
            format!(
                "{code} Client Error: {reason} for url: https://query2.finance.yahoo.com/v8/finance/chart/AAPL"
            ),
        )
    }

    #[test]
    fn native_http_429_is_a_terminal_rate_limit() {
        let error = native_gateway_error(status_error(429, "Too Many Requests"));
        assert_eq!(error.code, "RateLimited");
        assert!(error.message.starts_with("429 Client Error"));
    }

    #[test]
    fn native_http_403_is_a_terminal_access_denial() {
        let error = native_gateway_error(status_error(403, "Forbidden"));
        assert_eq!(error.code, "AccessDenied");
    }

    #[test]
    fn other_status_errors_keep_the_legacy_http_error_class() {
        let error = native_gateway_error(status_error(500, "Internal Server Error"));
        assert_eq!(error.code, "HTTPError");
        // Non-HTTP provider failures keep their own provider exception class.
        let timeout = native_gateway_error(ProviderError::provider(
            "HTTP request failed (timeout): operation timed out",
        ));
        assert_eq!(timeout.code, "ProviderError");
    }
}
