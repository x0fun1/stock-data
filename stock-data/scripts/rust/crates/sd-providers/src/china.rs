//! China source dispatch (R13): the 11 Sina/Tencent/Eastmoney gateway functions.

use serde_json::{Map, Value};

use crate::http::HttpTransport;
use crate::transport::{JobContext, ProviderError};

/// Functions implemented by R13.
pub const FUNCTIONS: [&str; 11] = [
    "us_stock_quote_sina",
    "us_stock_quote_tencent",
    "hk_stock_quote_tencent",
    "hk_stock_quote_sina",
    "stock_quote_eastmoney",
    "us_stock_kline_sina",
    "financial_statements_eastmoney",
    "key_indicators_eastmoney",
    "fund_flow_daily",
    "stock_search",
    "market_stock_list",
];

/// Whether one gateway function is implemented by the China sources.
pub fn is_function(function: &str) -> bool {
    FUNCTIONS.contains(&function)
}

fn arg<'a>(params: &'a Map<String, Value>, name: &str) -> Option<&'a Value> {
    params.get(name)
}

/// Dispatch one R13 function with an injected transport.
pub fn call(
    function: &str,
    params: &Map<String, Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    match function {
        "us_stock_quote_sina" => {
            crate::sina::us_stock_quote_sina(arg(params, "ticker"), transport, context)
        }
        "hk_stock_quote_sina" => {
            crate::sina::hk_stock_quote_sina(arg(params, "code"), transport, context)
        }
        "us_stock_kline_sina" => crate::sina::us_stock_kline_sina(
            arg(params, "ticker"),
            arg(params, "num"),
            transport,
            context,
        ),
        "us_stock_quote_tencent" => {
            crate::tencent::us_stock_quote_tencent(arg(params, "ticker"), transport, context)
        }
        "hk_stock_quote_tencent" => {
            crate::tencent::hk_stock_quote_tencent(arg(params, "code"), transport, context)
        }
        "stock_quote_eastmoney" => crate::eastmoney::stock_quote_eastmoney(
            arg(params, "ticker_or_code"),
            arg(params, "secid_prefix"),
            transport,
            context,
        ),
        "financial_statements_eastmoney" => crate::eastmoney::financial_statements_eastmoney(
            arg(params, "secucode"),
            arg(params, "statement"),
            arg(params, "page_size"),
            transport,
            context,
        ),
        "key_indicators_eastmoney" => crate::eastmoney::key_indicators_eastmoney(
            arg(params, "secucode"),
            arg(params, "page_size"),
            transport,
            context,
        ),
        "fund_flow_daily" => crate::eastmoney::fund_flow_daily(
            arg(params, "ticker_or_code"),
            arg(params, "secid_prefix"),
            arg(params, "limit"),
            transport,
            context,
        ),
        "stock_search" => crate::eastmoney::stock_search(
            arg(params, "keyword"),
            arg(params, "count"),
            transport,
            context,
        ),
        "market_stock_list" => crate::eastmoney::market_stock_list(
            arg(params, "market"),
            arg(params, "sort_field"),
            arg(params, "sort_desc"),
            arg(params, "page"),
            arg(params, "page_size"),
            transport,
            context,
        ),
        other => Err(ProviderError::value(format!(
            "Function is not in the allowed list; use --list. ({other})"
        ))),
    }
}
