//! Eastmoney adapters (R13): quotes, financial statements/indicators, fund flow,
//! stock search and market list.

use serde_json::{Map, Value};

use crate::common::{
    int_value, nonempty_text, num_value, positive_int, put_int, put_number, put_text, round_to,
    symbol_text,
};
use crate::http::{HttpRequest, HttpTransport};
use crate::transport::{JobContext, ProviderError};

const UA: &str = crate::transport::USER_AGENT;
const DATACENTER_URL: &str = "https://datacenter-web.eastmoney.com/api/data/v1/get";

fn secid_prefix(value: Option<&Value>) -> Result<i64, ProviderError> {
    let prefix = match value {
        None => 105,
        Some(Value::Number(number)) => number.as_i64().unwrap_or(-1),
        Some(_) => -1,
    };
    if !matches!(prefix, 105 | 106 | 107 | 116) {
        return Err(ProviderError::value(
            "secid_prefix must be 105, 106, 107 or 116.",
        ));
    }
    Ok(prefix)
}

/// `eastmoney_datacenter`: shared datacenter query (returns the `data` rows).
pub fn eastmoney_datacenter(
    report_name: &str,
    filter_str: &str,
    page_size: i64,
    sort_columns: &str,
    sort_types: &str,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    nonempty_text(Some(&Value::String(report_name.to_string())), "report_name")?;
    positive_int(Some(&Value::from(page_size)), "page_size")?;
    let request = HttpRequest::new(
        DATACENTER_URL,
        &[
            ("reportName", report_name.to_string()),
            ("columns", "ALL".to_string()),
            ("filter", filter_str.to_string()),
            ("pageNumber", "1".to_string()),
            ("pageSize", page_size.to_string()),
            ("sortColumns", sort_columns.to_string()),
            ("sortTypes", sort_types.to_string()),
            ("source", "WEB".to_string()),
            ("client", "WEB".to_string()),
        ],
        15.0,
    )
    .with_header("User-Agent", UA);
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    if document.get("success").and_then(Value::as_bool) == Some(false) {
        return Err(ProviderError::provider(
            "Eastmoney datacenter returned a provider error.",
        ));
    }
    let rows = document
        .get("result")
        .and_then(|result| result.get("data"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    Ok(Value::Array(rows))
}

/// `stock_quote_eastmoney`: normalized push2 quote for US/HK (`secid_prefix`).
pub fn stock_quote_eastmoney(
    ticker_or_code: Option<&Value>,
    secid_prefix_value: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let symbol = nonempty_text(ticker_or_code, "ticker_or_code")?;
    let prefix = secid_prefix(secid_prefix_value)?;
    let request = HttpRequest::new(
        "https://push2.eastmoney.com/api/qt/stock/get",
        &[
            ("secid", format!("{prefix}.{symbol}")),
            (
                "fields",
                "f43,f44,f45,f46,f47,f48,f55,f57,f58,f59,f60,f170".to_string(),
            ),
        ],
        10.0,
    );
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    let Some(data) = document.get("data").filter(|value| !value.is_null()) else {
        return Ok(Value::Object(Map::new()));
    };
    let decimal = data.get("f59").and_then(int_value);
    let Some(decimal) = decimal.filter(|value| (0..=12).contains(value)) else {
        return Err(ProviderError::provider(
            "Eastmoney quote lacks a valid price precision.",
        ));
    };
    let divisor = 10f64.powi(decimal as i32);
    let price = |key: &str| -> Option<f64> {
        data.get(key)
            .and_then(num_value)
            .map(|raw| round_to(raw / divisor, decimal as u32))
    };
    let change_raw = data.get("f170").ok_or_else(|| {
        ProviderError::new(crate::transport::ProviderErrorKind::KeyError, "'f170'")
    })?;
    let change_pct = num_value(change_raw).map(|value| value / 100.0);
    let mut map = Map::new();
    map.insert(
        "code".into(),
        data.get("f57").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "name".into(),
        data.get("f58").cloned().unwrap_or(Value::Null),
    );
    put_number(&mut map, "price", price("f43"));
    put_number(&mut map, "high", price("f44"));
    put_number(&mut map, "low", price("f45"));
    put_number(&mut map, "open", price("f46"));
    map.insert(
        "volume".into(),
        data.get("f47")
            .and_then(int_value)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    put_number(&mut map, "amount", data.get("f48").and_then(num_value));
    put_number(
        &mut map,
        "turnover_rate",
        data.get("f55").and_then(num_value),
    );
    put_number(&mut map, "prev_close", price("f60"));
    put_number(&mut map, "change_pct", change_pct);
    Ok(Value::Object(map))
}

const REPORT_MAP: [(&str, &str, &str); 3] = [
    ("balance", "RPT_USF10_FN_BALANCE", "RPT_HKF10_FN_BALANCE"),
    ("income", "RPT_USF10_FN_INCOME", "RPT_HKF10_FN_INCOME"),
    ("cashflow", "RPT_USSK_FN_CASHFLOW", "RPT_HKSK_FN_CASHFLOW"),
];

/// `financial_statements_eastmoney`: US/HK income/balance/cashflow rows.
pub fn financial_statements_eastmoney(
    secucode: Option<&Value>,
    statement: Option<&Value>,
    page_size: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let secucode = symbol_text(&nonempty_text(secucode, "secucode")?, "secucode")?;
    let statement = match statement {
        None => "balance".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => {
            return Err(ProviderError::value(
                "statement must be balance, income or cashflow.",
            ))
        }
    };
    let page_size = match page_size {
        None => 200,
        Some(value) => positive_int(Some(value), "page_size")?,
    };
    let market_is_hk = secucode.ends_with(".HK");
    let report_name = REPORT_MAP
        .iter()
        .find(|(name, _, _)| *name == statement)
        .map(|(_, us, hk)| if market_is_hk { *hk } else { *us })
        .ok_or_else(|| ProviderError::value("statement must be balance, income or cashflow."))?;
    eastmoney_datacenter(
        report_name,
        &format!("(SECUCODE=\"{secucode}\")"),
        page_size,
        "REPORT_DATE",
        "-1",
        transport,
        context,
    )
}

/// `key_indicators_eastmoney`: US/HK GMAININDICATOR rows.
pub fn key_indicators_eastmoney(
    secucode: Option<&Value>,
    page_size: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let secucode = symbol_text(&nonempty_text(secucode, "secucode")?, "secucode")?;
    let page_size = match page_size {
        None => 4,
        Some(value) => positive_int(Some(value), "page_size")?,
    };
    let report_name = if secucode.ends_with(".HK") {
        "RPT_HKF10_FN_GMAININDICATOR"
    } else {
        "RPT_USF10_FN_GMAININDICATOR"
    };
    eastmoney_datacenter(
        report_name,
        &format!("(SECUCODE=\"{secucode}\")"),
        page_size,
        "REPORT_DATE",
        "-1",
        transport,
        context,
    )
}

/// `fund_flow_daily`: push2his daily fund-flow klines.
pub fn fund_flow_daily(
    ticker_or_code: Option<&Value>,
    secid_prefix_value: Option<&Value>,
    limit: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let limit = positive_int(limit.or(Some(&Value::from(100))), "limit")?;
    let symbol = nonempty_text(ticker_or_code, "ticker_or_code")?;
    let prefix = secid_prefix(secid_prefix_value)?;
    let request = HttpRequest::new(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        &[
            ("secid", format!("{prefix}.{symbol}")),
            ("klt", "101".to_string()),
            ("fields1", "f1,f2,f3,f7".to_string()),
            ("fields2", "f51,f52,f53,f54,f55,f56,f57".to_string()),
            ("lmt", limit.to_string()),
        ],
        15.0,
    );
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    let Some(klines) = document
        .get("data")
        .and_then(|data| data.get("klines"))
        .and_then(Value::as_array)
    else {
        return Ok(Value::Array(Vec::new()));
    };
    let mut rows = Vec::with_capacity(klines.len());
    for line in klines {
        let Some(text) = line.as_str() else {
            continue;
        };
        let parts: Vec<&str> = text.split(',').collect();
        let number = |index: usize| -> Result<f64, ProviderError> {
            parts
                .get(index)
                .ok_or_else(|| ProviderError::provider("Eastmoney fund flow row is truncated."))?
                .parse::<f64>()
                .map_err(|_| {
                    ProviderError::value(format!(
                        "could not convert string to float: '{}'",
                        parts.get(index).copied().unwrap_or("")
                    ))
                })
        };
        let mut row = Map::new();
        put_text(&mut row, "date", parts.first().copied().unwrap_or(""));
        put_number(&mut row, "main_net", Some(number(1)?));
        put_number(&mut row, "small_net", Some(number(2)?));
        put_number(&mut row, "mid_net", Some(number(3)?));
        put_number(&mut row, "big_net", Some(number(4)?));
        put_number(&mut row, "super_big_net", Some(number(5)?));
        let main_pct = if parts.len() > 6 && !parts[6].is_empty() {
            parts[6]
                .parse::<f64>()
                .map_err(|_| ProviderError::value("invalid main percentage"))?
        } else {
            0.0
        };
        row.insert(
            "main_pct".into(),
            if parts.len() > 6 && !parts[6].is_empty() {
                Value::from(main_pct)
            } else {
                Value::from(0)
            },
        );
        rows.push(Value::Object(row));
    }
    Ok(Value::Array(rows))
}

/// `stock_search`: Eastmoney suggestion search (US/HK only).
pub fn stock_search(
    keyword: Option<&Value>,
    count: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let keyword = nonempty_text(keyword, "keyword")?;
    let count = match count {
        None => 10,
        Some(value) => positive_int(Some(value), "count")?,
    };
    let request = HttpRequest::new(
        "https://searchapi.eastmoney.com/api/suggest/get",
        &[
            ("input", keyword),
            ("type", "14".to_string()),
            ("token", "D43BF722C8E33BDC906FB84D85E326E8".to_string()),
            ("count", count.to_string()),
        ],
        10.0,
    );
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    let data = document
        .get("QuotationCodeTable")
        .and_then(|table| table.get("Data"));
    let suggestions = match data {
        None => Vec::new(),
        Some(Value::Null) => {
            return Err(ProviderError::new(
                crate::transport::ProviderErrorKind::TypeError,
                "'NoneType' object is not iterable",
            ))
        }
        Some(Value::Array(items)) => items.clone(),
        Some(_) => {
            return Err(ProviderError::new(
                crate::transport::ProviderErrorKind::TypeError,
                "'NoneType' object is not iterable",
            ))
        }
    };
    let market_map = [
        ("105", "NASDAQ"),
        ("106", "NYSE"),
        ("107", "US_OTHER"),
        ("116", "HK"),
    ];
    let mut rows = Vec::new();
    for suggestion in suggestions {
        let market = suggestion.get("MktNum").cloned().unwrap_or(Value::Null);
        let market_text = match &market {
            Value::String(text) => text.clone(),
            Value::Number(number) => number.to_string(),
            _ => String::new(),
        };
        if !matches!(market_text.as_str(), "105" | "106" | "107" | "116") {
            continue;
        }
        let mut row = Map::new();
        row.insert(
            "code".into(),
            suggestion.get("Code").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "name".into(),
            suggestion.get("Name").cloned().unwrap_or(Value::Null),
        );
        let market_number = suggestion
            .get("MktNum")
            .and_then(int_value)
            .unwrap_or_else(|| market_text.parse().unwrap_or(0));
        row.insert("mkt_num".into(), Value::from(market_number));
        row.insert(
            "market_name".into(),
            Value::String(
                market_map
                    .iter()
                    .find(|(key, _)| *key == market_text)
                    .map(|(_, name)| (*name).to_string())
                    .unwrap_or_else(|| market_text.clone()),
            ),
        );
        row.insert(
            "security_type".into(),
            suggestion
                .get("SecurityTypeName")
                .cloned()
                .unwrap_or(Value::Null),
        );
        rows.push(Value::Object(row));
    }
    Ok(Value::Array(rows))
}

/// `market_stock_list`: push2 clist page with raw (unscaled) prices and precision.
pub fn market_stock_list(
    market: Option<&Value>,
    sort_field: Option<&Value>,
    sort_desc: Option<&Value>,
    page: Option<&Value>,
    page_size: Option<&Value>,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let page = match page {
        None => 1,
        Some(value) => positive_int(Some(value), "page")?,
    };
    let page_size = match page_size {
        None => 20,
        Some(value) => positive_int(Some(value), "page_size")?,
    };
    let market = match market {
        None => "us_nasdaq".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("market must have type str.")),
    };
    let sort_field = match sort_field {
        None => "f3".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("sort_field must have type str.")),
    };
    let sort_desc = match sort_desc {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return Err(ProviderError::value("sort_desc must have type bool.")),
    };
    let market_map = [
        ("us_nasdaq", "m:105"),
        ("us_nyse", "m:106"),
        ("us_etf", "m:107"),
        ("hk", "m:116"),
    ];
    let fs = market_map
        .iter()
        .find(|(name, _)| *name == market)
        .map(|(_, value)| (*value).to_string())
        .unwrap_or_else(|| market.clone());
    let request = HttpRequest::new(
        "https://push2.eastmoney.com/api/qt/clist/get",
        &[
            ("fs", fs),
            (
                "fields",
                "f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18,f152".to_string(),
            ),
            ("pn", page.to_string()),
            ("pz", page_size.to_string()),
            ("fid", sort_field),
            ("po", if sort_desc { "1" } else { "0" }.to_string()),
        ],
        15.0,
    );
    let response = transport.get(&request, context)?;
    response.raise_for_status()?;
    let document = response.json()?;
    let data = document.get("data").cloned().unwrap_or(Value::Null);
    let total = data.get("total").cloned().unwrap_or(Value::from(0));
    let diff_value = data
        .get("diff")
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));
    let diff: Vec<Value> = match diff_value {
        Value::Array(items) => items,
        Value::Object(map) => map.values().cloned().collect(),
        _ => Vec::new(),
    };
    let mut stocks = Vec::with_capacity(diff.len());
    for item in diff {
        let raw_price = item.get("f2").and_then(num_value);
        let raw_change = item.get("f3").and_then(num_value);
        let mut row = Map::new();
        row.insert(
            "code".into(),
            item.get("f12").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "name".into(),
            item.get("f14").cloned().unwrap_or(Value::Null),
        );
        put_int(&mut row, "precision", item.get("f152").and_then(int_value));
        put_number(&mut row, "raw_price", raw_price);
        put_number(
            &mut row,
            "raw_change_amount",
            item.get("f4").and_then(num_value),
        );
        put_number(&mut row, "raw_high", item.get("f15").and_then(num_value));
        put_number(&mut row, "raw_low", item.get("f16").and_then(num_value));
        put_number(&mut row, "raw_open", item.get("f17").and_then(num_value));
        put_number(
            &mut row,
            "raw_prev_close",
            item.get("f18").and_then(num_value),
        );
        for key in [
            "price",
            "change_amount",
            "high",
            "low",
            "open",
            "prev_close",
        ] {
            row.insert(key.into(), Value::Null);
        }
        put_text(
            &mut row,
            "normalization_note",
            "Raw clist price scaling has not been verified; use stock_quote_eastmoney for normalized quotes. precision is retained without assuming a divisor.",
        );
        put_number(&mut row, "raw_change_pct", raw_change);
        put_number(
            &mut row,
            "change_pct",
            raw_change.map(|value| value / 100.0),
        );
        put_int(&mut row, "volume", item.get("f5").and_then(int_value));
        put_number(&mut row, "amount", item.get("f6").and_then(num_value));
        let raw_amplitude = item.get("f7").and_then(num_value);
        put_number(&mut row, "raw_amplitude", raw_amplitude);
        put_number(
            &mut row,
            "amplitude",
            raw_amplitude.map(|value| value / 100.0),
        );
        stocks.push(Value::Object(row));
    }
    Ok(serde_json::json!({"total": total, "stocks": stocks}))
}
