//! Sina market adapters (R13): US quote, HK quote and US daily K-line JSONP.

use serde_json::{Map, Value};

use crate::common::{
    assert_us_ticker, hk_code, int_text, jsonp_array, put_int, put_number, put_text,
    quoted_payload, symbol_text,
};
use crate::http::{HttpRequest, HttpTransport};
use crate::transport::{JobContext, ProviderError};

const SINA_REFERER: &str = "https://finance.sina.com.cn/";
const UA: &str = crate::transport::USER_AGENT;

/// `us_stock_quote_sina`: 36-field Sina US quote (`gb_<ticker>`).
pub fn us_stock_quote_sina(
    ticker: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let ticker = assert_us_ticker(ticker)?;
    let url = format!("https://hq.sinajs.cn/list=gb_{}", ticker.to_lowercase());
    let request = HttpRequest::new(url, &[], 10.0)
        .with_header("Referer", SINA_REFERER)
        .with_header("User-Agent", UA);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let text = response.gbk_text();
    let Some(payload) = quoted_payload(&text) else {
        return Ok(Value::Object(Map::new()));
    };
    let fields: Vec<&str> = payload.split(',').collect();
    if fields.len() < 30 {
        return Ok(Value::Object(Map::new()));
    }
    let mut map = Map::new();
    put_text(&mut map, "name", fields[0]);
    put_number(&mut map, "price", super::common::num_text(fields[1]));
    put_number(&mut map, "change_pct", super::common::num_text(fields[2]));
    put_text(&mut map, "timestamp", fields[3]);
    put_number(&mut map, "prev_close", super::common::num_text(fields[26]));
    put_number(&mut map, "open", super::common::num_text(fields[5]));
    put_number(&mut map, "high", super::common::num_text(fields[6]));
    put_number(&mut map, "low", super::common::num_text(fields[7]));
    put_number(&mut map, "volume", super::common::num_text(fields[10]));
    put_number(&mut map, "high_52w", super::common::num_text(fields[8]));
    put_number(&mut map, "low_52w", super::common::num_text(fields[9]));
    put_number(&mut map, "market_cap", super::common::num_text(fields[12]));
    put_number(&mut map, "eps", super::common::num_text(fields[13]));
    put_number(&mut map, "pe", super::common::num_text(fields[14]));
    Ok(Value::Object(map))
}

/// `hk_stock_quote_sina`: 25-field Sina HK quote (`rt_hk<code>`).
pub fn hk_stock_quote_sina(
    code: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let code = hk_code(code)?;
    let url = format!("https://hq.sinajs.cn/list=rt_hk{code}");
    let request = HttpRequest::new(url, &[], 10.0)
        .with_header("Referer", SINA_REFERER)
        .with_header("User-Agent", UA);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let text = response.gbk_text();
    let Some(payload) = quoted_payload(&text) else {
        return Ok(Value::Object(Map::new()));
    };
    let fields: Vec<&str> = payload.split(',').collect();
    if fields.len() < 15 {
        return Ok(Value::Object(Map::new()));
    }
    let mut map = Map::new();
    put_text(&mut map, "name_en", fields[0]);
    put_text(&mut map, "name", fields[1]);
    put_number(&mut map, "open", super::common::num_text(fields[2]));
    put_number(&mut map, "prev_close", super::common::num_text(fields[3]));
    put_number(&mut map, "high", super::common::num_text(fields[4]));
    put_number(&mut map, "low", super::common::num_text(fields[5]));
    put_number(&mut map, "price", super::common::num_text(fields[6]));
    put_number(&mut map, "change", super::common::num_text(fields[7]));
    put_number(&mut map, "change_pct", super::common::num_text(fields[8]));
    put_number(&mut map, "volume", super::common::num_text(fields[12]));
    put_number(&mut map, "amount", super::common::num_text(fields[11]));
    Ok(Value::Object(map))
}

/// `us_stock_kline_sina`: daily JSONP K-line (`NUM` bars).
pub fn us_stock_kline_sina(
    ticker: Option<&Value>,
    num: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let num = match num {
        None => 120,
        Some(value) => crate::common::positive_int(Some(value), "num")?,
    };
    let ticker = assert_us_ticker(ticker)?;
    let url =
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK";
    let request = HttpRequest::new(
        url,
        &[("symbol", ticker.clone()), ("num", num.to_string())],
        15.0,
    )
    .with_header("Referer", SINA_REFERER);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let text = response.text();
    let Some(items) = jsonp_array(&text) else {
        return Ok(Value::Array(Vec::new()));
    };
    let mut rows = Vec::with_capacity(items.len());
    for item in items {
        let mut row = Map::new();
        row.insert("date".into(), item.get("d").cloned().unwrap_or(Value::Null));
        put_number(
            &mut row,
            "open",
            item.get("o").and_then(super::common::num_value),
        );
        put_number(
            &mut row,
            "high",
            item.get("h").and_then(super::common::num_value),
        );
        put_number(
            &mut row,
            "low",
            item.get("l").and_then(super::common::num_value),
        );
        put_number(
            &mut row,
            "close",
            item.get("c").and_then(super::common::num_value),
        );
        put_int(
            &mut row,
            "volume",
            item.get("v").and_then(super::common::int_value),
        );
        rows.push(Value::Object(row));
    }
    Ok(Value::Array(rows))
}

/// `_symbol_text`-based helper kept for the search path.
#[allow(dead_code)]
pub fn validated_symbol(value: Option<&Value>, name: &str) -> Result<String, ProviderError> {
    match value {
        Some(Value::String(text)) => symbol_text(text, name),
        _ => Err(ProviderError::value(format!("{name} must have type str."))),
    }
}

#[allow(dead_code)]
fn _keep_int_text(value: &str) -> Option<i64> {
    int_text(value)
}
