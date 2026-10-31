//! Gateway function registry.
//!
//! The data table is generated from the frozen R00 fixture; this module is the
//! hand-written API around it. R12 adds parameter validation and execution.

use serde_json::{Map, Value};

use crate::registry_generated::{FunctionSpec, FUNCTIONS, FUNCTION_COUNT};

/// Every registered function, sorted by name.
pub fn functions() -> &'static [FunctionSpec] {
    FUNCTIONS
}

/// Look up one function by name.
pub fn find(name: &str) -> Option<&'static FunctionSpec> {
    FUNCTIONS.iter().find(|entry| entry.function == name)
}

/// Number of registered functions (must stay 56).
pub fn function_count() -> usize {
    FUNCTION_COUNT
}

/// Legacy `--list` order (`FUNCTION_SOURCES` insertion order in the Python module).
///
/// The frozen R00 registry fixture is sorted by name, but hosts diff the `--list` payload
/// in order, so the public list command must keep the legacy order.
pub const LEGACY_ORDER: [&str; 56] = [
    "yahoo_quote_summary",
    "us_stock_quote_sina",
    "us_stock_quote_tencent",
    "hk_stock_quote_tencent",
    "hk_stock_quote_sina",
    "stock_quote_eastmoney",
    "us_stock_kline_sina",
    "stock_kline_yahoo",
    "calc_ma",
    "calc_macd",
    "calc_rsi",
    "calc_kdj",
    "calc_boll",
    "financial_statements_eastmoney",
    "key_indicators_eastmoney",
    "key_statistics",
    "analyst_estimates",
    "institutional_holders",
    "financial_statements_yahoo",
    "fund_flow_daily",
    "parse_osi",
    "options_chain_cboe",
    "filter_expiry",
    "unusual_activity",
    "chain_summary",
    "cboe_quote",
    "options_chain",
    "sec_filings",
    "sec_xbrl_facts",
    "stock_search",
    "ticker_to_cik",
    "market_stock_list",
    "short_volume_all",
    "short_volume_symbol",
    "short_volume_ranking",
    "daily_filings",
    "fulltext_search",
    "market_frame",
    "frame_ranking",
    "frame_screen",
    "treasury_yield_curve",
    "cftc_cot",
    "earnings_calendar",
    "yahoo_profile",
    "yahoo_options",
    "yahoo_quote",
    "yahoo_financials",
    "yahoo_history",
    "yahoo_earnings",
    "yahoo_analysis",
    "yahoo_holders",
    "yahoo_funds",
    "yahoo_screen",
    "yahoo_history_batch",
    "yahoo_statistics",
    "yahoo_search",
];

/// `gateway --list` payload entries, in the legacy shape:
/// `{"function": ..., "source": ..., "parameters": [{"name","required","type","default"}]}`.
///
/// The parameter objects come verbatim from the frozen legacy list so optional
/// parameters keep both their (possibly absent) `type` and their `default` value.
pub fn list_entries() -> Vec<Value> {
    LEGACY_ORDER
        .iter()
        .filter_map(|name| find(name))
        .map(|entry| {
            let mut item = Map::new();
            item.insert("function".into(), Value::String(entry.function.into()));
            item.insert("source".into(), Value::String(entry.source.into()));
            let parameters: Value = serde_json::from_str(entry.parameters_json)
                .unwrap_or_else(|_| Value::Array(Vec::new()));
            item.insert("parameters".into(), parameters);
            Value::Object(item)
        })
        .collect()
}
