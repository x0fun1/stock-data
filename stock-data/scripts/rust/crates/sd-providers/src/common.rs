//! Shared adapter helpers for the China sources (R13).
//!
//! The legacy functions validate symbols, parse GBK/JSONP payloads and coerce numeric
//! fields; these helpers reproduce those rules so each source module stays declarative.

use serde_json::{Map, Value};

use crate::http::HttpResponse;
use crate::local::py_round;
use crate::transport::{ProviderError, ProviderErrorKind};

/// Python `_num` over a split field: finite float or `None`.
pub fn num_text(text: &str) -> Option<f64> {
    let trimmed = text.trim();
    let parsed: f64 = trimmed.parse().ok()?;
    parsed.is_finite().then_some(parsed)
}

/// Python `_int_or_none`: truncate a parsed number toward zero.
pub fn int_text(text: &str) -> Option<i64> {
    num_text(text).map(|value| value as i64)
}

/// Python `_num` over a JSON value.
pub fn num_value(value: &Value) -> Option<f64> {
    crate::local::num(value)
}

/// Python `_int_or_none` over a JSON value.
pub fn int_value(value: &Value) -> Option<i64> {
    num_value(value).map(|number| number as i64)
}

/// `_nonempty_text`: strip and reject empty/non-string values with the legacy message.
pub fn nonempty_text(value: Option<&Value>, name: &str) -> Result<String, ProviderError> {
    match value {
        Some(Value::String(text)) if !text.trim().is_empty() => Ok(text.trim().to_string()),
        _ => Err(ProviderError::value(format!(
            "{name} must be a nonempty string."
        ))),
    }
}

/// `_positive_int` validation for JSON parameters.
pub fn positive_int(value: Option<&Value>, name: &str) -> Result<i64, ProviderError> {
    match value {
        Some(Value::Number(number)) => {
            if let Some(integer) = number.as_i64() {
                if (1..=10_000).contains(&integer) {
                    return Ok(integer);
                }
            }
            Err(ProviderError::value(format!(
                "{name} must be a positive integer no larger than 10000."
            )))
        }
        _ => Err(ProviderError::value(format!(
            "{name} must be a positive integer no larger than 10000."
        ))),
    }
}

/// `_symbol_text`: symbol/path/filter character allowlist.
pub fn symbol_text(value: &str, name: &str) -> Result<String, ProviderError> {
    let bytes = value.as_bytes();
    let valid = !bytes.is_empty()
        && bytes.len() <= 64
        && (bytes[0].is_ascii_alphanumeric() || matches!(bytes[0], b'.' | b'^'))
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'^' | b'-'));
    if !valid {
        return Err(ProviderError::value(format!(
            "{name} contains unsupported symbol/path/filter characters."
        )));
    }
    Ok(value.to_string())
}

/// `assert_us_ticker`: uppercase US ticker, HK/digit-only rejected.
pub fn assert_us_ticker(value: Option<&Value>) -> Result<String, ProviderError> {
    let text = nonempty_text(value, "ticker")?.to_uppercase();
    if text.ends_with(".HK") || text.chars().all(|character| character.is_ascii_digit()) {
        return Err(ProviderError::value(
            "This function supports US tickers only.",
        ));
    }
    let bytes = text.as_bytes();
    let valid = bytes
        .first()
        .map(|byte| byte.is_ascii_uppercase())
        .unwrap_or(false)
        && bytes.iter().all(|byte| {
            byte.is_ascii_uppercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-')
        });
    if !valid {
        return Err(ProviderError::value("ticker has an invalid format."));
    }
    Ok(text)
}

/// Hong Kong code normalisation (`4` or `5` digits zero-padded to five).
pub fn hk_code(value: Option<&Value>) -> Result<String, ProviderError> {
    let text = nonempty_text(value, "code")?;
    if !text.is_ascii() || !text.chars().all(|character| character.is_ascii_digit()) {
        return Err(ProviderError::value(
            "code must contain 4 or 5 Hong Kong code digits.",
        ));
    }
    if text.len() != 4 && text.len() != 5 {
        return Err(ProviderError::value(
            "code must contain 4 or 5 Hong Kong code digits.",
        ));
    }
    Ok(format!("{text:0>5}"))
}

/// Extract the quoted payload of a Sina/Tencent `var x="...";` line.
pub fn quoted_payload(text: &str) -> Option<String> {
    let start = text.find('"')?;
    let end = text[start + 1..].rfind('"')?;
    Some(text[start + 1..start + 1 + end].to_string())
}

/// Extract the JSONP array `([...])` payload from a Sina JSONP response.
pub fn jsonp_array(text: &str) -> Option<Vec<Value>> {
    let open = text.find('(')?;
    let close = text.rfind(')')?;
    if close <= open {
        return None;
    }
    let payload = &text[open + 1..close];
    serde_json::from_str::<Vec<Value>>(payload).ok()
}

/// Build the response object for `_raise_for_status`-style callers.
pub fn ensure_status(response: &HttpResponse) -> Result<(), ProviderError> {
    response.raise_for_status()
}

/// Round like Python `round(value, digits)`.
pub fn round_to(value: f64, digits: u32) -> f64 {
    py_round(value, digits)
}

/// Numeric value as JSON (`None` becomes null).
pub fn number_or_null(value: Option<f64>) -> Value {
    value.map(Value::from).unwrap_or(Value::Null)
}

/// Insert a key with a numeric-or-null value.
pub fn put_number(map: &mut Map<String, Value>, key: &str, value: Option<f64>) {
    map.insert(key.to_string(), number_or_null(value));
}

/// Insert a key with an integer-or-null value.
pub fn put_int(map: &mut Map<String, Value>, key: &str, value: Option<i64>) {
    map.insert(
        key.to_string(),
        value.map(Value::from).unwrap_or(Value::Null),
    );
}

/// Insert a raw string field.
pub fn put_text(map: &mut Map<String, Value>, key: &str, value: &str) {
    map.insert(key.to_string(), Value::String(value.to_string()));
}

/// Provider error whose type name mirrors the legacy exception class.
pub fn error(kind: ProviderErrorKind, message: impl Into<String>) -> ProviderError {
    ProviderError::new(kind, message)
}
