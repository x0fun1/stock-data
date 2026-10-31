//! CBOE delayed-quote adapters (R14).

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::http::HttpTransport;
use crate::local::parse_osi;
use crate::official::{official_get, ProviderEnv};
use crate::transport::{JobContext, ProviderError};

const CBOE_BASE: &str = "https://cdn.cboe.com/api/global/delayed_quotes";

/// `cboe_quote`: one CBOE stock snapshot (`data` block).
pub fn cboe_quote(
    ticker: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let ticker = crate::common::assert_us_ticker(ticker)?;
    let response = official_get(
        &format!("{CBOE_BASE}/quotes/{ticker}.json"),
        &[],
        &BTreeMap::new(),
        30.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    document
        .get("data")
        .cloned()
        .ok_or_else(|| ProviderError::new(crate::transport::ProviderErrorKind::KeyError, "'data'"))
}

/// `options_chain_cboe`: delayed option chain with OCC metadata.
pub fn options_chain_cboe(
    ticker: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let ticker = crate::common::assert_us_ticker(ticker)?;
    let response = official_get(
        &format!("{CBOE_BASE}/options/{ticker}.json"),
        &[],
        &BTreeMap::new(),
        30.0,
        true,
        env,
        transport,
        context,
    )?;
    let raw = response.json.unwrap_or(Value::Null);
    let data = raw
        .get("data")
        .filter(|value| !value.is_null())
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let options = data
        .get("options")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut contracts: Vec<Value> = Vec::new();
    for option in &options {
        let symbol = option
            .get("option")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let meta = parse_osi(symbol);
        let Some(meta_object) = meta.as_object() else {
            continue;
        };
        if meta_object.is_empty() {
            continue;
        }
        let mut row = Map::new();
        row.insert("symbol".into(), Value::String(symbol.to_string()));
        for (key, value) in meta_object {
            row.insert(key.clone(), value.clone());
        }
        row.insert(
            "bid".into(),
            option.get("bid").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "ask".into(),
            option.get("ask").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "volume".into(),
            option
                .get("volume")
                .and_then(crate::common::num_value)
                .map(Value::from)
                .unwrap_or(Value::Null),
        );
        row.insert(
            "open_interest".into(),
            option
                .get("open_interest")
                .and_then(crate::common::num_value)
                .map(Value::from)
                .unwrap_or(Value::Null),
        );
        for key in [
            "iv",
            "delta",
            "gamma",
            "vega",
            "theta",
            "rho",
            "last_trade_price",
        ] {
            row.insert(key.into(), option.get(key).cloned().unwrap_or(Value::Null));
        }
        contracts.push(Value::Object(row));
    }
    if contracts.is_empty() {
        return Err(ProviderError::data_not_available(format!(
            "{ticker} 未返回任何期权合约 —— 该标的可能无期权，或不在 CBOE 覆盖范围（CBOE 仅覆盖美股）"
        )));
    }
    Ok(serde_json::json!({
        "ticker": ticker,
        "timestamp": raw.get("timestamp").cloned().unwrap_or(Value::Null),
        "spot": data.get("current_price").cloned().unwrap_or(Value::Null),
        "contracts": contracts,
    }))
}
