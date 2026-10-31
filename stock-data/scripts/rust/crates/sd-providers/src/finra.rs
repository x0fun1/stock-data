//! FINRA Reg SHO short-volume adapters (R14).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use crate::common::positive_int;
use crate::http::HttpTransport;
use crate::official::{official_get, ProviderEnv};
use crate::sec::{recent_weekdays, validated_date};
use crate::transport::{JobContext, ProviderError, ProviderErrorKind};

const MARKETS: [&str; 4] = ["CNMS", "FNSQ", "FNYX", "FNRA"];

/// `short_volume_all`: whole-market Reg SHO file for one day (weekday fallback).
pub fn short_volume_all(
    date: Option<&Value>,
    market: Option<&Value>,
    now: DateTime<Utc>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let requested_date = match date {
        None | Some(Value::Null) => None,
        Some(value) => Some(validated_date(Some(value), "date", "%Y%m%d")?),
    };
    let market = match market {
        None => "CNMS".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("Unknown FINRA market.")),
    };
    if !MARKETS.contains(&market.as_str()) {
        return Err(ProviderError::value("Unknown FINRA market."));
    }
    let candidates = match &requested_date {
        Some(date) => vec![date.clone()],
        None => recent_weekdays(now, 7),
    };
    for day in candidates {
        let url = format!("https://cdn.finra.org/equity/regsho/daily/{market}shvol{day}.txt");
        let raw = match official_get(
            &url,
            &[],
            &BTreeMap::new(),
            30.0,
            false,
            env,
            transport,
            context,
        ) {
            Ok(response) => response.text,
            Err(error) if error.kind == ProviderErrorKind::DataNotAvailable => continue,
            Err(error) => return Err(error),
        };
        let mut rows: Vec<(String, Value)> = Vec::new();
        for line in raw.lines().skip(1) {
            let parts: Vec<&str> = line.split('|').collect();
            if parts.len() < 5 || parts[1].is_empty() {
                continue;
            }
            let (Ok(sv), Ok(se), Ok(tv)) = (
                parts[2].parse::<f64>(),
                parts[3].parse::<f64>(),
                parts[4].parse::<f64>(),
            ) else {
                continue;
            };
            let mut record = Map::new();
            record.insert("short".into(), Value::from(sv));
            record.insert("short_exempt".into(), Value::from(se));
            record.insert("total".into(), Value::from(tv));
            record.insert(
                "ratio".into(),
                if tv != 0.0 {
                    Value::from(crate::common::round_to(sv / tv, 4))
                } else {
                    Value::Null
                },
            );
            rows.push((parts[1].to_string(), Value::Object(record)));
        }
        if !rows.is_empty() {
            let mut data = Map::new();
            for (symbol, record) in &rows {
                data.insert(symbol.clone(), record.clone());
            }
            return Ok(serde_json::json!({
                "date": day,
                "market": market,
                "count": rows.len(),
                "data": data,
            }));
        }
    }
    let scope = if requested_date.is_some() {
        "该日"
    } else {
        "近 7 个工作日"
    };
    Err(ProviderError::data_not_available(format!(
        "未找到 {market} {scope}的 Reg SHO 数据"
    )))
}

/// `short_volume_symbol`: recent per-symbol short-volume series.
pub fn short_volume_symbol(
    symbol: Option<&Value>,
    days: Option<&Value>,
    market: Option<&Value>,
    now: DateTime<Utc>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let symbol = crate::common::assert_us_ticker(symbol)?;
    let days = match days {
        None => 5,
        Some(value) => positive_int(Some(value), "days")?,
    } as usize;
    let market_text = match market {
        None => "CNMS".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("Unknown FINRA market.")),
    };
    let mut out: Vec<Value> = Vec::new();
    for day in recent_weekdays(now, days * 2) {
        if out.len() >= days {
            break;
        }
        let snapshot = match short_volume_all(
            Some(&Value::String(day.clone())),
            Some(&Value::String(market_text.clone())),
            now,
            env,
            transport,
            context,
        ) {
            Ok(snapshot) => snapshot,
            Err(error) if error.kind == ProviderErrorKind::DataNotAvailable => continue,
            Err(error) => return Err(error),
        };
        let Some(record) = snapshot
            .get("data")
            .and_then(|data| data.get(symbol.to_uppercase()))
            .cloned()
        else {
            continue;
        };
        let mut row = Map::new();
        row.insert("date".into(), Value::String(day));
        if let Some(fields) = record.as_object() {
            for (key, value) in fields {
                row.insert(key.clone(), value.clone());
            }
        }
        out.push(Value::Object(row));
    }
    Ok(Value::Array(out))
}
