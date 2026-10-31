//! Nasdaq earnings-calendar adapter (R14).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use crate::http::HttpTransport;
use crate::official::{official_get, ProviderEnv};
use crate::sec::{et_today, validated_date};
use crate::transport::{JobContext, ProviderError};

/// `earnings_calendar`: Nasdaq earnings rows for one date.
pub fn earnings_calendar(
    date: Option<&Value>,
    now: DateTime<Utc>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let date = match date {
        None | Some(Value::Null) => et_today(now).format("%Y-%m-%d").to_string(),
        Some(value) => validated_date(Some(value), "date", "%Y-%m-%d")?,
    };
    let mut headers = BTreeMap::new();
    headers.insert("Accept".to_string(), "application/json".to_string());
    let response = official_get(
        "https://api.nasdaq.com/api/calendar/earnings",
        &[("date", date.clone())],
        &headers,
        30.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    let rows = document
        .get("data")
        .and_then(|data| data.get("rows"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mapped: Vec<Value> = rows
        .iter()
        .map(|row| {
            let mut item = Map::new();
            item.insert(
                "symbol".into(),
                row.get("symbol").cloned().unwrap_or(Value::Null),
            );
            item.insert(
                "name".into(),
                row.get("name").cloned().unwrap_or(Value::Null),
            );
            item.insert(
                "time".into(),
                row.get("time").cloned().unwrap_or(Value::Null),
            );
            item.insert(
                "eps_forecast".into(),
                row.get("epsForecast").cloned().unwrap_or(Value::Null),
            );
            item.insert(
                "market_cap".into(),
                row.get("marketCap").cloned().unwrap_or(Value::Null),
            );
            Value::Object(item)
        })
        .collect();
    Ok(serde_json::json!({"date": date, "count": mapped.len(), "rows": mapped}))
}
