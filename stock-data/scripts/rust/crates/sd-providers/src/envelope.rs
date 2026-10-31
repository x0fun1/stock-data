//! Gateway envelope and receipt (`global_stock_data.main`).
//!
//! One JSON envelope per invocation: status/source/function/fetched_at_utc/data for a
//! successful call, and status/source/function/fetched_at_utc/error_type/error_message for
//! a failure. `--output` writes the full envelope to a new file and prints only a receipt.

use std::path::Path;

use serde_json::{Map, Value};

use crate::registry;

/// Status label of one result (`empty` when the payload carries no records).
pub fn status_for(data: &Value) -> &'static str {
    match data {
        Value::Null => "empty",
        Value::Array(items) if items.is_empty() => "empty",
        Value::Object(map) if map.is_empty() => "empty",
        _ => "empty_or_success_placeholder",
    }
}

/// Envelope status for a payload: `empty` for null/empty collections, else `success`.
pub fn data_status(data: &Value) -> &'static str {
    match data {
        Value::Null => "empty",
        Value::Array(items) if items.is_empty() => "empty",
        Value::Object(map) if map.is_empty() => "empty",
        _ => "success",
    }
}

/// Source label registered for one function (`FUNCTION_SOURCES`).
pub fn function_source(function: &str) -> Option<&'static str> {
    registry::find(function).map(|entry| entry.source)
}

/// Successful envelope in the legacy key order.
pub fn success_envelope(
    source: Option<&str>,
    function: &str,
    data: Value,
    fetched_at: &str,
) -> Value {
    let status = data_status(&data);
    let mut envelope = Map::new();
    envelope.insert("status".into(), Value::String(status.into()));
    envelope.insert(
        "source".into(),
        source
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    envelope.insert("function".into(), Value::String(function.into()));
    envelope.insert("fetched_at_utc".into(), Value::String(fetched_at.into()));
    envelope.insert("data".into(), data);
    sd_core::security::sanitize_data(&Value::Object(envelope))
}

/// Error envelope in the legacy key order (the message is already redacted).
pub fn error_envelope(
    source: Option<&str>,
    function: Option<&str>,
    error_type: &str,
    error_message: &str,
    fetched_at: &str,
) -> Value {
    let mut envelope = Map::new();
    envelope.insert("status".into(), Value::String("error".into()));
    envelope.insert(
        "source".into(),
        source
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    envelope.insert(
        "function".into(),
        function
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    envelope.insert("fetched_at_utc".into(), Value::String(fetched_at.into()));
    envelope.insert("error_type".into(), Value::String(error_type.into()));
    envelope.insert("error_message".into(), Value::String(error_message.into()));
    Value::Object(envelope)
}

/// `_yahoo_record_count`: normalized record count for the CLI receipt.
pub fn record_count(data: &Value) -> Option<u64> {
    let Value::Object(map) = data else {
        return match data {
            Value::Array(items) => Some(items.len() as u64),
            _ => None,
        };
    };
    let payload = if map.contains_key("status") && map.contains_key("data") {
        map.get("data").unwrap_or(&Value::Null)
    } else {
        data
    };
    match payload {
        Value::Null => Some(0),
        Value::Array(items) => Some(items.len() as u64),
        Value::Object(payload) => {
            if payload.is_empty() {
                return Some(0);
            }
            for key in ["bars", "articles", "records", "quotes"] {
                if let Some(Value::Array(items)) = payload.get(key) {
                    return Some(items.len() as u64);
                }
            }
            let calls = payload.get("calls").and_then(Value::as_array);
            let puts = payload.get("puts").and_then(Value::as_array);
            if calls.is_some() || puts.is_some() {
                return Some(
                    calls.map(|rows| rows.len() as u64).unwrap_or(0)
                        + puts.map(|rows| rows.len() as u64).unwrap_or(0),
                );
            }
            if let Some(Value::Object(symbols)) = payload.get("symbols") {
                return Some(symbols.len() as u64);
            }
            if let Some(Value::Array(pages)) = payload.get("pages") {
                let mut total = 0u64;
                for page in pages {
                    if let Some(quotes) = page
                        .get("result")
                        .and_then(|result| result.get("quotes"))
                        .and_then(Value::as_array)
                    {
                        total += quotes.len() as u64;
                    }
                }
                return Some(total);
            }
            if let Some(Value::Object(statuses)) = map.get("module_status") {
                return Some(
                    statuses
                        .values()
                        .filter(|status| status.as_str() == Some("success"))
                        .count() as u64,
                );
            }
            Some(1)
        }
        _ => Some(0),
    }
}

/// Receipt printed on stdout when `--output` captured the full envelope.
pub fn output_receipt(
    envelope: &Value,
    envelope_file: &Path,
    data: &Value,
    yahoo_function: bool,
) -> Value {
    let mut receipt = Map::new();
    receipt.insert(
        "status".into(),
        envelope
            .get("status")
            .cloned()
            .unwrap_or(Value::String("success".into())),
    );
    receipt.insert(
        "source".into(),
        envelope.get("source").cloned().unwrap_or(Value::Null),
    );
    receipt.insert(
        "function".into(),
        envelope.get("function").cloned().unwrap_or(Value::Null),
    );
    receipt.insert(
        "fetched_at_utc".into(),
        envelope
            .get("fetched_at_utc")
            .cloned()
            .unwrap_or(Value::Null),
    );
    receipt.insert(
        "envelope_file".into(),
        Value::String(
            std::fs::canonicalize(envelope_file)
                .map(|path| path.to_string_lossy().to_string())
                .unwrap_or_else(|_| envelope_file.to_string_lossy().to_string()),
        ),
    );
    let count = if yahoo_function {
        record_count(data)
    } else {
        match data {
            Value::Object(map) => Some(map.len() as u64),
            Value::Array(items) => Some(items.len() as u64),
            _ => None,
        }
    };
    receipt.insert(
        "record_count".into(),
        count.map(Value::from).unwrap_or(Value::Null),
    );
    Value::Object(receipt)
}
