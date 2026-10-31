//! Request contract and timestamp parsing.
//!
//! Port of `stock-data/scripts/quant_research/contracts.py`. The rules are
//! deliberately strict and the emitted key order is part of the readable-JSON
//! contract, so the struct field order below matches the Python dict order:
//! `ticker, market, horizon, asof, mode, intent, asset_type, policy,
//! history_window, instrument`.

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::{SdError, SdResult};
use crate::policy;

/// Normalized research request.
///
/// `asof` is always serialized (as `null` when absent) because the legacy request
/// object always carries the key; the optional blocks are omitted when the input
/// did not supply them, exactly like the Python dict.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Request {
    pub ticker: String,
    pub market: String,
    pub horizon: String,
    pub asof: Option<String>,
    pub mode: String,
    pub intent: String,
    pub asset_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub policy: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub history_window: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub instrument: Option<Value>,
}

const REQUEST_FIELDS: &[&str] = &[
    "ticker",
    "market",
    "horizon",
    "asof",
    "mode",
    "intent",
    "asset_type",
    "instrument",
    "history_window",
    "history_start",
    "history_end",
    "policy",
];
const INSTRUMENT_FIELDS: &[&str] = &[
    "resolved_ticker",
    "provider_symbols",
    "currency",
    "unit",
    "source",
    "benchmark",
];
const WINDOW_FIELDS: &[&str] = &["start", "end", "selection"];

impl Request {
    /// JSON form with the frozen key order.
    pub fn to_json(&self) -> Value {
        serde_json::to_value(self).expect("Request serializes to a JSON object")
    }
}

/// Python `repr()` for the value types this contract accepts.
///
/// Only used in error messages that the legacy runtime formats with `{value!r}`.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'")),
        other => other.to_string(),
    }
}

/// Python `str()` for the value types this contract accepts.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn py_date_fromisoformat(text: &str) -> SdResult<NaiveDate> {
    policy::parse_iso_date(text)
        .map_err(|_| SdError::validation(format!("Invalid isoformat string: '{text}'")))
}

fn split_offset(text: &str) -> (String, Option<&str>) {
    // Trailing Z is already normalised by the caller; look for +/- after the time.
    if let Some(index) = text.rfind(['+', '-']) {
        if index > 10 {
            return (text[..index].to_string(), Some(&text[index..]));
        }
    }
    (text.to_string(), None)
}

fn parse_time(text: &str) -> SdResult<NaiveTime> {
    let (main, fractional) = match text.split_once('.') {
        Some((main, fraction)) => (main, Some(fraction)),
        None => (text, None),
    };
    let digits: String = fractional
        .unwrap_or("")
        .chars()
        .take_while(char::is_ascii_digit)
        .collect();
    if fractional.is_some() && digits.len() != fractional.unwrap_or("").len() {
        return Err(SdError::validation(format!(
            "Invalid isoformat string: '{text}'"
        )));
    }
    let (hour, minute, second) = if main.len() == 6 && !main.contains(':') {
        (
            main[0..2].parse::<u32>(),
            main[2..4].parse::<u32>(),
            main[4..6].parse::<u32>(),
        )
    } else {
        let parts: Vec<&str> = main.split(':').collect();
        if parts.len() < 2 || parts.len() > 3 {
            return Err(SdError::validation(format!(
                "Invalid isoformat string: '{text}'"
            )));
        }
        let hour = parts[0].parse::<u32>();
        let minute = parts[1].parse::<u32>();
        let second = if parts.len() == 3 {
            parts[2].parse::<u32>()
        } else {
            Ok(0)
        };
        (hour, minute, second)
    };
    let (Ok(hour), Ok(minute), Ok(second)) = (hour, minute, second) else {
        return Err(SdError::validation(format!(
            "Invalid isoformat string: '{text}'"
        )));
    };
    let nanos = if digits.is_empty() {
        0
    } else {
        let truncated: String = format!("{digits:0<9}").chars().take(9).collect();
        truncated.parse::<u32>().unwrap_or(0)
    };
    NaiveTime::from_hms_nano_opt(hour, minute, second, nanos)
        .ok_or_else(|| SdError::validation(format!("Invalid isoformat string: '{text}'")))
}

/// Parse the timestamp forms the legacy runtime accepts.
///
/// Mirrors `contracts.parse_timestamp`: a trailing `Z` becomes `+00:00`, the value
/// must carry a timezone, and the result is converted to UTC.
pub fn parse_timestamp(value: &Value) -> SdResult<DateTime<Utc>> {
    let raw = py_str(value);
    let text = raw.trim().to_string();
    let normalised = if let Some(stripped) = text.strip_suffix('Z') {
        format!("{stripped}+00:00")
    } else {
        text.clone()
    };
    let parsed = parse_iso_datetime(&normalised)?;
    match parsed {
        ParsedTimestamp::Aware(instant) => Ok(instant),
        ParsedTimestamp::Naive => Err(SdError::validation(format!(
            "timestamp must include a timezone: {}",
            py_repr(value)
        ))),
    }
}

/// Convenience wrapper for `&str` inputs.
pub fn parse_timestamp_str(text: &str) -> SdResult<DateTime<Utc>> {
    parse_timestamp(&Value::String(text.to_string()))
}

enum ParsedTimestamp {
    Aware(DateTime<Utc>),
    Naive,
}

fn parse_iso_datetime(text: &str) -> SdResult<ParsedTimestamp> {
    let (body, offset_text) = split_offset(text);
    let (date_text, time_text) = match body.split_once(['T', ' ']) {
        Some((date, time)) => (date.to_string(), Some(time.to_string())),
        None => (body.clone(), None),
    };
    let date = py_date_fromisoformat(&date_text)?;
    let time = match &time_text {
        Some(text) => parse_time(text)?,
        None => NaiveTime::from_hms_opt(0, 0, 0).expect("midnight is valid"),
    };
    let naive = NaiveDateTime::new(date, time);
    let Some(offset_text) = offset_text else {
        return Ok(ParsedTimestamp::Naive);
    };
    let offset = parse_offset(offset_text)?;
    let aware = offset
        .from_local_datetime(&naive)
        .single()
        .ok_or_else(|| SdError::validation(format!("Invalid isoformat string: '{text}'")))?;
    Ok(ParsedTimestamp::Aware(aware.with_timezone(&Utc)))
}

fn parse_offset(text: &str) -> SdResult<FixedOffset> {
    let sign = if text.starts_with('-') { -1 } else { 1 };
    let digits: String = text.chars().skip(1).filter(char::is_ascii_digit).collect();
    let (hours, minutes, seconds) = match digits.len() {
        2 => (digits[0..2].to_string(), "00".to_string(), "00".to_string()),
        4 => (
            digits[0..2].to_string(),
            digits[2..4].to_string(),
            "00".to_string(),
        ),
        6 => (
            digits[0..2].to_string(),
            digits[2..4].to_string(),
            digits[4..6].to_string(),
        ),
        _ => {
            return Err(SdError::validation(format!(
                "Invalid isoformat string: '{text}'"
            )))
        }
    };
    let (Ok(hours), Ok(minutes), Ok(seconds)) = (
        hours.parse::<i32>(),
        minutes.parse::<i32>(),
        seconds.parse::<i32>(),
    ) else {
        return Err(SdError::validation(format!(
            "Invalid isoformat string: '{text}'"
        )));
    };
    let total = sign * (hours * 3600 + minutes * 60 + seconds);
    FixedOffset::east_opt(total)
        .ok_or_else(|| SdError::validation(format!("Invalid isoformat string: '{text}'")))
}

fn is_valid_symbol(text: &str, allow_lowercase: bool) -> bool {
    let mut chars = text.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    let first_ok = first.is_ascii_uppercase()
        || first.is_ascii_digit()
        || first == '.'
        || first == '^'
        || (allow_lowercase && first.is_ascii_lowercase());
    if !first_ok || text.chars().count() > 32 {
        return false;
    }
    text.chars().skip(1).all(|item| {
        item.is_ascii_uppercase()
            || item.is_ascii_digit()
            || matches!(item, '.' | '_' | '^' | '-')
            || (allow_lowercase && item.is_ascii_lowercase())
    })
}

fn py_sorted_choices(choices: &[&str]) -> String {
    let mut sorted = choices.to_vec();
    sorted.sort_unstable();
    let quoted: Vec<String> = sorted.iter().map(|item| format!("'{item}'")).collect();
    format!("[{}]", quoted.join(", "))
}

fn reject_unknown_fields(
    value: &Map<String, Value>,
    allowed: &[&str],
    message_prefix: &str,
) -> SdResult<()> {
    let mut unknown: Vec<&str> = value
        .keys()
        .map(String::as_str)
        .filter(|key| !allowed.contains(key))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort_unstable();
    Err(SdError::validation(format!(
        "{message_prefix}{}",
        unknown.join(", ")
    )))
}

/// Normalize and validate a research request.
pub fn normalize_request(value: &Value) -> SdResult<Request> {
    let Value::Object(object) = value else {
        return Err(SdError::validation("request must be an object"));
    };
    reject_unknown_fields(object, REQUEST_FIELDS, "unsupported request fields: ")?;

    let Some(Value::String(ticker_raw)) = object.get("ticker") else {
        return Err(SdError::validation("request.ticker must be a string"));
    };
    let ticker = ticker_raw.trim().to_uppercase();
    if !is_valid_symbol(&ticker, false) {
        return Err(SdError::validation(
            "request.ticker must use supported stock/index symbol characters",
        ));
    }

    let market = object
        .get("market")
        .map(py_str)
        .unwrap_or_else(|| "US".to_string())
        .trim()
        .to_uppercase();
    if !policy::MARKETS.contains(&market.as_str()) {
        return Err(SdError::validation("request.market must be US or HK"));
    }
    let horizon = object
        .get("horizon")
        .map(py_str)
        .unwrap_or_else(|| "5D".to_string())
        .trim()
        .to_uppercase();
    if policy::horizon_sessions(&horizon).is_none() {
        return Err(SdError::validation(
            "request.horizon must be one of 1D, 5D, 20D",
        ));
    }
    let mode = object
        .get("mode")
        .map(py_str)
        .unwrap_or_else(|| "standard".to_string())
        .trim()
        .to_lowercase();
    if !policy::MODES.contains(&mode.as_str()) {
        return Err(SdError::validation(
            "request.mode must be standard or strict",
        ));
    }
    let asof = match object.get("asof") {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) if text.is_empty() => None,
        Some(other) => Some(crate::clock::format_utc_timestamp(parse_timestamp(other)?)),
    };

    let intent_value = object
        .get("intent")
        .cloned()
        .unwrap_or_else(|| Value::String("forecast".into()));
    let Value::String(intent) = intent_value else {
        return Err(SdError::validation(format!(
            "request.intent must be one of {}",
            py_sorted_choices(policy::INTENTS)
        )));
    };
    if !policy::INTENTS.contains(&intent.as_str()) {
        return Err(SdError::validation(format!(
            "request.intent must be one of {}",
            py_sorted_choices(policy::INTENTS)
        )));
    }
    let asset_type_value = object
        .get("asset_type")
        .cloned()
        .unwrap_or_else(|| Value::String("unknown".into()));
    let Value::String(asset_type) = asset_type_value else {
        return Err(SdError::validation(format!(
            "request.asset_type must be one of {}",
            py_sorted_choices(policy::ASSET_TYPES)
        )));
    };
    if !policy::ASSET_TYPES.contains(&asset_type.as_str()) {
        return Err(SdError::validation(format!(
            "request.asset_type must be one of {}",
            py_sorted_choices(policy::ASSET_TYPES)
        )));
    }

    let horizon = policy::validate_forecast_horizon(&horizon, &intent)?;

    let mut request = Request {
        ticker: ticker.clone(),
        market,
        horizon,
        asof,
        mode,
        intent: intent.clone(),
        asset_type,
        policy: None,
        history_window: None,
        instrument: None,
    };

    if intent == "forecast" {
        if let Some(supplied) = object.get("policy") {
            if !supplied.is_object() {
                return Err(SdError::validation("request.policy must be an object"));
            }
            let active = policy::policy_metadata();
            let version = supplied.get("policy_version").and_then(Value::as_str);
            if version != active.get("policy_version").and_then(Value::as_str) {
                return Err(SdError::policy(
                    policy::ERROR_POLICY_MISMATCH,
                    "request.policy_version is not the active policy",
                ));
            }
        }
        request.policy = Some(policy::policy_metadata());
    }

    // history_window: only start/end/selection; history_start/history_end are the
    // deprecated spellings and must agree when both are present.
    let supplied_window = object
        .get("history_window")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let Value::Object(supplied_window) = supplied_window else {
        return Err(SdError::validation(
            "request.history_window must contain only start/end/selection",
        ));
    };
    reject_unknown_fields(
        &supplied_window,
        WINDOW_FIELDS,
        "request.history_window must contain only start/end/selection: ",
    )?;
    let mut window = supplied_window;
    for field in ["start", "end"] {
        let deprecated = object.get(&format!("history_{field}")).cloned();
        let item = match deprecated {
            Some(value) => Some(value),
            None => window.get(field).cloned(),
        };
        let Some(item) = item else { continue };
        if item.is_null() {
            continue;
        }
        let Some(text) = item.as_str() else {
            return Err(SdError::validation(format!(
                "Invalid isoformat string: {}",
                py_repr(&item)
            )));
        };
        let parsed = py_date_fromisoformat(text)?;
        if let Some(existing) = window.get(field) {
            if existing != &Value::String(text.to_string()) {
                return Err(SdError::validation("conflicting request history window"));
            }
        }
        window.insert(field.to_string(), Value::String(policy::iso_date(parsed)));
    }
    if let (Some(Value::String(start)), Some(Value::String(end))) =
        (window.get("start"), window.get("end"))
    {
        if start > end {
            return Err(SdError::validation("request history window is reversed"));
        }
    }
    if !window.is_empty() {
        request.history_window = Some(Value::Object(window));
    }

    if let Some(instrument) = object.get("instrument") {
        let Value::Object(instrument) = instrument else {
            return Err(SdError::validation(
                "request.instrument has unsupported fields",
            ));
        };
        reject_unknown_fields(
            instrument,
            INSTRUMENT_FIELDS,
            "request.instrument has unsupported fields: ",
        )?;
        let mut instrument = instrument.clone();
        let resolved = instrument
            .get("resolved_ticker")
            .and_then(Value::as_str)
            .unwrap_or(&ticker);
        if resolved != ticker {
            return Err(SdError::validation(
                "resolved instrument differs from request ticker",
            ));
        }
        if let Some(benchmark) = instrument.get("benchmark") {
            if !benchmark.is_null() {
                let Some(text) = benchmark.as_str() else {
                    return Err(SdError::validation(
                        "instrument.benchmark must use supported symbol characters",
                    ));
                };
                let trimmed = text.trim();
                if !is_valid_symbol(trimmed, true) {
                    return Err(SdError::validation(
                        "instrument.benchmark must use supported symbol characters",
                    ));
                }
                if trimmed.to_uppercase() == ticker {
                    return Err(SdError::validation(
                        "instrument.benchmark must differ from the request ticker",
                    ));
                }
                instrument.insert("benchmark".into(), Value::String(trimmed.to_uppercase()));
            }
        }
        if let Some(aliases) = instrument.get("provider_symbols") {
            let Some(aliases) = aliases.as_object() else {
                return Err(SdError::validation(
                    "instrument provider_symbols must be validated symbol strings",
                ));
            };
            if aliases.values().any(|item| match item.as_str() {
                Some(text) => !is_valid_symbol(text, true),
                None => true,
            }) {
                return Err(SdError::validation(
                    "instrument provider_symbols must be validated symbol strings",
                ));
            }
            let has_aliases = !aliases.is_empty();
            let has_source = instrument
                .get("source")
                .map(|item| !item.is_null() && item.as_str() != Some(""))
                .unwrap_or(false);
            if has_aliases && !has_source {
                return Err(SdError::validation(
                    "instrument aliases require source provenance",
                ));
            }
        }
        request.instrument = Some(Value::Object(instrument));
    }

    Ok(request)
}

/// Python `repr()` of a JSON value (used by report/markdown text and identity digests).
pub fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                integer.to_string()
            } else if let Some(float) = number.as_f64() {
                python_float_repr(float)
            } else {
                number.to_string()
            }
        }
        Value::String(text) => python_str_repr(text),
        Value::Array(items) => {
            let rendered: Vec<String> = items.iter().map(python_repr).collect();
            format!("[{}]", rendered.join(", "))
        }
        Value::Object(map) => {
            if map.is_empty() {
                return "{}".to_string();
            }
            let rendered: Vec<String> = map
                .iter()
                .map(|(key, item)| format!("{}: {}", python_str_repr(key), python_repr(item)))
                .collect();
            format!("{{{}}}", rendered.join(", "))
        }
    }
}

/// Python `str()` of a JSON value: strings unchanged, everything else like `repr`.
pub fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python_repr(other),
    }
}

/// Python `repr(float)`: shortest round-trip, fixed notation for -4 <= exp < 16.
pub fn python_float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let debug = format!("{value:?}");
    if let Some(position) = debug.find(['e', 'E']) {
        let mantissa = &debug[..position];
        let exponent: i32 = debug[position + 1..].parse().unwrap_or(0);
        if (-4..16).contains(&exponent) {
            return expand_fixed(mantissa, exponent);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exponent.abs());
    }
    if debug.contains('.') {
        debug
    } else {
        format!("{debug}.0")
    }
}

fn expand_fixed(mantissa: &str, exponent: i32) -> String {
    let negative = mantissa.starts_with('-');
    let mantissa = mantissa.trim_start_matches('-');
    let digits: String = mantissa
        .chars()
        .filter(|character| *character != '.')
        .collect();
    let position = exponent + 1;
    let body = if position <= 0 {
        format!("0.{}{}", "0".repeat((-position) as usize), digits)
    } else if position as usize >= digits.len() {
        format!(
            "{}{}.0",
            digits,
            "0".repeat(position as usize - digits.len())
        )
    } else {
        let (whole, fraction) = digits.split_at(position as usize);
        format!("{whole}.{fraction}")
    };
    if negative {
        format!("-{body}")
    } else {
        body
    }
}

pub fn python_str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}
