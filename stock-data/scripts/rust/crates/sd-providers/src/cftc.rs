//! CFTC commitments-of-traders adapter (R14).

use std::collections::BTreeMap;

use serde_json::Value;

use crate::common::positive_int;
use crate::http::HttpTransport;
use crate::official::{official_get, ProviderEnv};
use crate::transport::{JobContext, ProviderError};

/// `cftc_cot`: CFTC commitments-of-traders rows.
pub fn cftc_cot(
    limit: Option<&Value>,
    market_contains: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let limit = match limit {
        None => 20,
        Some(value) => positive_int(Some(value), "limit")?,
    };
    let mut params: Vec<(&str, String)> = vec![
        ("$limit", limit.to_string()),
        ("$order", "report_date_as_yyyy_mm_dd DESC".to_string()),
    ];
    if let Some(Value::String(text)) = market_contains {
        if !text.is_empty() {
            let escaped = text.replace('\'', "''");
            params.push((
                "$where",
                format!("upper(contract_market_name) like upper('%{escaped}%')"),
            ));
        }
    }
    let response = official_get(
        "https://publicreporting.cftc.gov/resource/6dca-aqww.json",
        &params,
        &BTreeMap::new(),
        30.0,
        true,
        env,
        transport,
        context,
    )?;
    Ok(response.json.unwrap_or(Value::Null))
}
