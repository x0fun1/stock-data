//! Tencent market adapters (R13): US quote and HK quote (`qt.gtimg.cn`).

use serde_json::{Map, Value};

use crate::common::{
    assert_us_ticker, hk_code, int_text, put_int, put_number, put_text, quoted_payload,
};
use crate::http::{HttpRequest, HttpTransport};
use crate::transport::{JobContext, ProviderError};

/// `us_stock_quote_tencent`: 71-field Tencent US quote.
pub fn us_stock_quote_tencent(
    ticker: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let ticker = assert_us_ticker(ticker)?;
    let url = format!("https://qt.gtimg.cn/q=us{ticker}");
    let request = HttpRequest::new(url, &[], 10.0);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let text = response.gbk_text();
    let Some(payload) = quoted_payload(&text) else {
        return Ok(Value::Object(Map::new()));
    };
    let fields: Vec<&str> = payload.split('~').collect();
    if fields.len() < 52 {
        return Ok(Value::Object(Map::new()));
    }
    let mut map = Map::new();
    put_text(&mut map, "name", fields[1]);
    put_text(&mut map, "name_en", fields[46]);
    put_number(&mut map, "price", super::common::num_text(fields[3]));
    put_number(&mut map, "prev_close", super::common::num_text(fields[4]));
    put_number(&mut map, "open", super::common::num_text(fields[5]));
    put_int(&mut map, "volume", int_text(fields[6]));
    put_number(&mut map, "high", super::common::num_text(fields[33]));
    put_number(&mut map, "low", super::common::num_text(fields[34]));
    put_number(&mut map, "high_52w", super::common::num_text(fields[48]));
    put_number(&mut map, "low_52w", super::common::num_text(fields[49]));
    put_number(&mut map, "change_pct", super::common::num_text(fields[32]));
    put_number(
        &mut map,
        "float_market_cap",
        super::common::num_text(fields[44]),
    );
    put_number(&mut map, "market_cap", super::common::num_text(fields[45]));
    put_number(&mut map, "eps", super::common::num_text(fields[47]));
    put_number(&mut map, "pe", super::common::num_text(fields[39]));
    put_number(&mut map, "pb", super::common::num_text(fields[51]));
    put_text(&mut map, "currency", fields[35]);
    put_text(&mut map, "timestamp", fields[30]);
    Ok(Value::Object(map))
}

/// `hk_stock_quote_tencent`: 78-field Tencent HK quote.
pub fn hk_stock_quote_tencent(
    code: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let code = hk_code(code)?;
    let url = format!("https://qt.gtimg.cn/q=r_hk{code}");
    let request = HttpRequest::new(url, &[], 10.0);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let text = response.gbk_text();
    let Some(payload) = quoted_payload(&text) else {
        return Ok(Value::Object(Map::new()));
    };
    let fields: Vec<&str> = payload.split('~').collect();
    if fields.len() < 76 {
        return Ok(Value::Object(Map::new()));
    }
    let mut map = Map::new();
    put_text(&mut map, "name", fields[1]);
    put_text(&mut map, "code", fields[2]);
    put_text(&mut map, "name_en", fields[46]);
    put_number(&mut map, "price", super::common::num_text(fields[3]));
    put_number(&mut map, "prev_close", super::common::num_text(fields[4]));
    put_number(&mut map, "open", super::common::num_text(fields[5]));
    put_number(&mut map, "high", super::common::num_text(fields[33]));
    put_number(&mut map, "low", super::common::num_text(fields[34]));
    put_int(&mut map, "volume", int_text(fields[6]));
    put_number(&mut map, "amount", super::common::num_text(fields[37]));
    put_number(&mut map, "change_pct", super::common::num_text(fields[32]));
    put_number(&mut map, "pe", super::common::num_text(fields[39]));
    put_number(&mut map, "pb", super::common::num_text(fields[58]));
    put_number(&mut map, "high_52w", super::common::num_text(fields[48]));
    put_number(&mut map, "low_52w", super::common::num_text(fields[49]));
    put_number(
        &mut map,
        "float_market_cap",
        super::common::num_text(fields[44]),
    );
    put_number(&mut map, "market_cap", super::common::num_text(fields[45]));
    put_text(&mut map, "currency", fields[75]);
    put_text(&mut map, "timestamp", fields[30]);
    Ok(Value::Object(map))
}
