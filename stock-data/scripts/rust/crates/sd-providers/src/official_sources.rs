//! Official/specialty source dispatch (R14): SEC, FINRA, CBOE, Treasury, CFTC, Nasdaq.

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use crate::http::HttpTransport;
use crate::official::ProviderEnv;
use crate::transport::{JobContext, ProviderError};

/// Functions implemented by R14.
pub const FUNCTIONS: [&str; 13] = [
    "sec_filings",
    "sec_xbrl_facts",
    "ticker_to_cik",
    "daily_filings",
    "fulltext_search",
    "market_frame",
    "short_volume_all",
    "short_volume_symbol",
    "cboe_quote",
    "options_chain_cboe",
    "treasury_yield_curve",
    "cftc_cot",
    "earnings_calendar",
];

/// Whether one gateway function is implemented by the official sources.
pub fn is_function(function: &str) -> bool {
    FUNCTIONS.contains(&function)
}

fn arg<'a>(params: &'a Map<String, Value>, name: &str) -> Option<&'a Value> {
    params.get(name)
}

/// Dispatch one R14 function with injected transport, configuration and clock.
pub fn call(
    function: &str,
    params: &Map<String, Value>,
    env: &ProviderEnv,
    now: DateTime<Utc>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    match function {
        "sec_filings" => crate::sec::sec_filings(
            arg(params, "cik"),
            arg(params, "form_type"),
            env,
            transport,
            context,
        ),
        "sec_xbrl_facts" => crate::sec::sec_xbrl_facts(
            arg(params, "cik"),
            arg(params, "metrics"),
            env,
            transport,
            context,
        ),
        "ticker_to_cik" => {
            crate::sec::ticker_to_cik(arg(params, "ticker"), env, transport, context)
        }
        "daily_filings" => crate::sec::daily_filings(
            arg(params, "date"),
            arg(params, "forms"),
            now,
            env,
            transport,
            context,
        ),
        "fulltext_search" => crate::sec::fulltext_search(
            arg(params, "query"),
            arg(params, "forms"),
            arg(params, "date_from"),
            arg(params, "date_to"),
            arg(params, "limit"),
            env,
            transport,
            context,
        ),
        "market_frame" => crate::sec::market_frame(
            arg(params, "tag"),
            arg(params, "year"),
            arg(params, "quarter"),
            arg(params, "unit"),
            arg(params, "instant"),
            env,
            transport,
            context,
        ),
        "short_volume_all" => crate::finra::short_volume_all(
            arg(params, "date"),
            arg(params, "market"),
            now,
            env,
            transport,
            context,
        ),
        "short_volume_symbol" => crate::finra::short_volume_symbol(
            arg(params, "symbol"),
            arg(params, "days"),
            arg(params, "market"),
            now,
            env,
            transport,
            context,
        ),
        "cboe_quote" => crate::cboe::cboe_quote(arg(params, "ticker"), env, transport, context),
        "options_chain_cboe" => {
            crate::cboe::options_chain_cboe(arg(params, "ticker"), env, transport, context)
        }
        "treasury_yield_curve" => {
            crate::treasury::treasury_yield_curve(arg(params, "year"), now, env, transport, context)
        }
        "cftc_cot" => crate::cftc::cftc_cot(
            arg(params, "limit"),
            arg(params, "market_contains"),
            env,
            transport,
            context,
        ),
        "earnings_calendar" => {
            crate::nasdaq::earnings_calendar(arg(params, "date"), now, env, transport, context)
        }
        other => Err(ProviderError::value(format!(
            "Function is not in the allowed list; use --list. ({other})"
        ))),
    }
}
