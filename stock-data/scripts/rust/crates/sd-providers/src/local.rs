//! R12 local gateway functions (`global_stock_data.py`): technical indicators, OCC/option
//! chain aggregation and local ranking/screening over already-collected payloads.
//!
//! These functions never touch the network. Their input validation, rounding, key order,
//! null semantics and error messages mirror the Python implementation.

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use chrono_tz::America::New_York;
use serde_json::{json, Map, Value};

use sd_core::float_sum::python_sum;

use crate::transport::{ProviderError, ProviderErrorKind};

fn key_error(name: &str) -> ProviderError {
    ProviderError::new(ProviderErrorKind::KeyError, format!("'{name}'"))
}

fn value_error(message: impl Into<String>) -> ProviderError {
    ProviderError::value(message)
}

/// Python `round(value, digits)` for a binary float.
pub fn py_round(value: f64, digits: u32) -> f64 {
    format!("{value:.precision$}", precision = digits as usize)
        .parse::<f64>()
        .unwrap_or(value)
}

/// Python `_num`: numeric source value or `None`; booleans/non-finite are missing.
pub fn num(value: &Value) -> Option<f64> {
    let parsed = match value {
        Value::Null | Value::Bool(_) => return None,
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    parsed.is_finite().then_some(parsed)
}

fn num_of(map: &Map<String, Value>, key: &str) -> Option<f64> {
    map.get(key).and_then(num)
}

fn positive_int(value: Option<&Value>, name: &str) -> Result<i64, ProviderError> {
    let Some(value) = value else {
        return Err(value_error(format!(
            "{name} must be a positive integer no larger than 10000."
        )));
    };
    let Some(number) = value.as_i64() else {
        return Err(value_error(format!(
            "{name} must be a positive integer no larger than 10000."
        )));
    };
    if !(1..=10_000).contains(&number) {
        return Err(value_error(format!(
            "{name} must be a positive integer no larger than 10000."
        )));
    }
    Ok(number)
}

fn positive_int_or(value: Option<&Value>, name: &str, default: i64) -> Result<i64, ProviderError> {
    match value {
        None => Ok(default),
        Some(value) => positive_int(Some(value), name),
    }
}

fn periods_arg(value: Option<&Value>, default: &[i64]) -> Result<Vec<i64>, ProviderError> {
    let Some(value) = value else {
        return Ok(default.to_vec());
    };
    let Some(items) = value.as_array() else {
        return Err(value_error(
            "periods must be a nonempty list of positive integers.",
        ));
    };
    if items.is_empty() {
        return Err(value_error(
            "periods must be a nonempty list of positive integers.",
        ));
    }
    let mut result = Vec::with_capacity(items.len());
    for item in items {
        result.push(positive_int(Some(item), "periods")?);
    }
    Ok(result)
}

fn require_str<'a>(params: &'a Map<String, Value>, name: &str) -> Result<&'a str, ProviderError> {
    match params.get(name) {
        Some(Value::String(text)) => Ok(text),
        _ => Err(value_error(format!("Parameter {name} must have type str."))),
    }
}

fn require_chain(chain: &Value) -> Result<&Vec<Value>, ProviderError> {
    chain
        .get("contracts")
        .and_then(Value::as_array)
        .ok_or_else(|| key_error("contracts"))
}

/// One validated candle with the raw date/close values kept for the output rows.
#[derive(Debug, Clone)]
struct Candle {
    date: Value,
    close_raw: Value,
    close: f64,
    high: Option<f64>,
    low: Option<f64>,
}

fn parse_series_instant(raw: &str) -> Option<f64> {
    if let Ok(parsed) = DateTime::parse_from_rfc3339(raw) {
        return Some(parsed.timestamp() as f64 + f64::from(parsed.timestamp_subsec_nanos()) / 1e9);
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(raw, format) {
            return Some(parsed.and_utc().timestamp() as f64);
        }
    }
    NaiveDate::parse_from_str(raw, "%Y-%m-%d")
        .ok()
        .map(|parsed| {
            parsed
                .and_hms_opt(0, 0, 0)
                .unwrap_or_default()
                .and_utc()
                .timestamp() as f64
        })
}

fn candle_sort_key(
    candle: &Map<String, Value>,
    index: usize,
) -> Result<(&'static str, f64), ProviderError> {
    if let Some(value) = candle.get("timestamp_epoch") {
        let Some(number) = num(value) else {
            return Err(value_error(format!(
                "Candle {index} has invalid timestamp_epoch."
            )));
        };
        return Ok(("timestamp_epoch", number));
    }
    let field = if candle.contains_key("timestamp_utc") {
        "timestamp_utc"
    } else {
        "date"
    };
    let Some(Value::String(text)) = candle.get(field) else {
        return Err(value_error(format!("Candle {index} has invalid {field}.")));
    };
    let Some(seconds) = parse_series_instant(text) else {
        return Err(value_error(format!("Candle {index} has invalid {field}.")));
    };
    if field == "timestamp_utc" {
        let utc = text.ends_with('Z')
            || text.ends_with("+00:00")
            || text.ends_with("+0000")
            || text.ends_with("+00");
        if !utc {
            return Err(value_error(format!(
                "Candle {index} timestamp_utc must contain a UTC timezone."
            )));
        }
    }
    Ok((field, seconds))
}

fn validate_klines(klines: &Value, fields: &[&str]) -> Result<Vec<Candle>, ProviderError> {
    let Some(items) = klines.as_array() else {
        return Err(value_error("klines must be a list of candles."));
    };
    let mut result = Vec::with_capacity(items.len());
    let mut key_field: Option<&'static str> = None;
    let mut previous: Option<f64> = None;
    for (index, item) in items.iter().enumerate() {
        let Some(candle) = item.as_object() else {
            return Err(value_error(format!("Candle {index} must contain a date.")));
        };
        let has_date = candle.get("date").map(py_truthy).unwrap_or(false);
        if !has_date {
            return Err(value_error(format!("Candle {index} must contain a date.")));
        }
        for field in fields {
            if num_of(candle, field).is_none() {
                return Err(value_error(format!(
                    "Candle {index} has missing or invalid {field}."
                )));
            }
        }
        if fields.contains(&"high") {
            let high = num_of(candle, "high").unwrap_or(0.0);
            let low = num_of(candle, "low").unwrap_or(0.0);
            if high < low {
                return Err(value_error(format!("Candle {index} has high below low.")));
            }
        }
        let (field, current) = candle_sort_key(candle, index)?;
        if let Some(existing) = key_field {
            if existing != field {
                return Err(value_error(
                    "Candles must use the same timestamp field throughout the series.",
                ));
            }
        }
        if let Some(prior) = previous {
            if current <= prior {
                return Err(value_error(
                    "Candles must be strictly ascending with no duplicates. Same-day intraday candles require explicit timestamp_epoch or timestamp_utc.",
                ));
            }
        }
        key_field = Some(field);
        previous = Some(current);
        result.push(Candle {
            date: candle.get("date").cloned().unwrap_or(Value::Null),
            close_raw: candle.get("close").cloned().unwrap_or(Value::Null),
            close: num_of(candle, "close").unwrap_or(0.0),
            high: num_of(candle, "high"),
            low: num_of(candle, "low"),
        });
    }
    Ok(result)
}

fn py_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// Python number semantics for the indicator paths: integers stay integers through
/// `sum()`/subtraction, and only true division or float factors promote to float.
#[derive(Debug, Clone, Copy)]
enum PyNum {
    Int(i64),
    Float(f64),
}

impl PyNum {
    fn to_float(self) -> f64 {
        match self {
            Self::Int(value) => value as f64,
            Self::Float(value) => value,
        }
    }

    fn is_int(self) -> bool {
        matches!(self, Self::Int(_))
    }

    fn sub(self, other: Self) -> Self {
        match (self, other) {
            (Self::Int(left), Self::Int(right)) => Self::Int(left.saturating_sub(right)),
            _ => Self::Float(self.to_float() - other.to_float()),
        }
    }

    fn mul(self, other: Self) -> Self {
        match (self, other) {
            (Self::Int(left), Self::Int(right)) => Self::Int(left.saturating_mul(right)),
            _ => Self::Float(self.to_float() * other.to_float()),
        }
    }

    fn div(self, divisor: f64) -> Self {
        Self::Float(self.to_float() / divisor)
    }

    fn rounded(self, digits: u32) -> Self {
        match self {
            Self::Int(value) => Self::Int(value),
            Self::Float(value) => Self::Float(py_round(value, digits)),
        }
    }

    fn to_value(self) -> Value {
        match self {
            Self::Int(value) => Value::from(value),
            Self::Float(value) => Value::from(value),
        }
    }
}

fn py_number(value: &Value) -> PyNum {
    match value {
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                PyNum::Int(integer)
            } else {
                PyNum::Float(number.as_f64().unwrap_or(0.0))
            }
        }
        Value::Bool(flag) => PyNum::Int(i64::from(*flag)),
        Value::String(text) => match text.trim().parse::<i64>() {
            Ok(value) => PyNum::Int(value),
            Err(_) => PyNum::Float(text.trim().parse::<f64>().unwrap_or(0.0)),
        },
        _ => PyNum::Int(0),
    }
}

fn py_sum(values: &[PyNum]) -> PyNum {
    if values.iter().all(|value| value.is_int()) {
        let total: i64 = values
            .iter()
            .map(|value| match value {
                PyNum::Int(inner) => *inner,
                PyNum::Float(inner) => *inner as i64,
            })
            .fold(0i64, |left, right| left.saturating_add(right));
        PyNum::Int(total)
    } else {
        PyNum::Float(python_sum(values.iter().map(|value| value.to_float())))
    }
}

fn ema_numbers(values: &[PyNum], period: i64) -> Vec<PyNum> {
    let mut result: Vec<PyNum> = Vec::with_capacity(values.len());
    let Some(first) = values.first().copied() else {
        return result;
    };
    result.push(first);
    let k = 2.0 / (period as f64 + 1.0);
    for value in &values[1..] {
        let previous = result
            .last()
            .map(|item| item.to_float())
            .unwrap_or(first.to_float());
        result.push(PyNum::Float(value.to_float() * k + previous * (1.0 - k)));
    }
    result
}

/// `_ema` over plain floats (used by the RSI/KDJ/Bollinger-free paths).
fn _ema_float(values: &[f64], period: i64) -> Vec<f64> {
    let mut result = Vec::with_capacity(values.len());
    let Some(first) = values.first() else {
        return result;
    };
    result.push(*first);
    let k = 2.0 / (period as f64 + 1.0);
    for value in &values[1..] {
        let previous = *result.last().unwrap_or(first);
        result.push(value * k + previous * (1.0 - k));
    }
    result
}

fn row(date: &Value, close: &Value) -> Map<String, Value> {
    let mut item = Map::new();
    item.insert("date".into(), date.clone());
    item.insert("close".into(), close.clone());
    item
}

/// `calc_ma`: moving averages plus EMA12/EMA26.
pub fn calc_ma(klines: &Value, periods: Option<&Value>) -> Result<Value, ProviderError> {
    let candles = validate_klines(klines, &["close"])?;
    let periods = periods_arg(periods, &[5, 10, 20, 60])?;
    if candles.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let closes: Vec<PyNum> = candles
        .iter()
        .map(|candle| py_number(&candle.close_raw))
        .collect();
    let ema12 = ema_numbers(&closes, 12);
    let ema26 = ema_numbers(&closes, 26);
    let mut rows = Vec::with_capacity(candles.len());
    for (index, candle) in candles.iter().enumerate() {
        let mut item = row(&candle.date, &candle.close_raw);
        for period in &periods {
            let period = *period as usize;
            let key = format!("ma{period}");
            if index + 1 >= period {
                let window = &closes[index + 1 - period..=index];
                let average = py_sum(window).div(period as f64).rounded(4);
                item.insert(key, average.to_value());
            } else {
                item.insert(key, Value::Null);
            }
        }
        item.insert("ema12".into(), ema12[index].rounded(4).to_value());
        item.insert("ema26".into(), ema26[index].rounded(4).to_value());
        rows.push(Value::Object(item));
    }
    Ok(Value::Array(rows))
}

/// `calc_macd`: DIF/DEA/histogram.
pub fn calc_macd(
    klines: &Value,
    fast: Option<&Value>,
    slow: Option<&Value>,
    signal: Option<&Value>,
) -> Result<Value, ProviderError> {
    let candles = validate_klines(klines, &["close"])?;
    let fast = positive_int_or(fast, "fast", 12)?;
    let slow = positive_int_or(slow, "slow", 26)?;
    let signal = positive_int_or(signal, "signal", 9)?;
    if candles.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let closes: Vec<PyNum> = candles
        .iter()
        .map(|candle| py_number(&candle.close_raw))
        .collect();
    let ema_fast = ema_numbers(&closes, fast);
    let ema_slow = ema_numbers(&closes, slow);
    let dif: Vec<PyNum> = ema_fast
        .iter()
        .zip(ema_slow.iter())
        .map(|(fast_ema, slow_ema)| fast_ema.sub(*slow_ema).rounded(4))
        .collect();
    let dea = ema_numbers(&dif, signal);
    let mut rows = Vec::with_capacity(candles.len());
    for (index, candle) in candles.iter().enumerate() {
        let mut item = row(&candle.date, &candle.close_raw);
        item.insert("dif".into(), dif[index].rounded(4).to_value());
        item.insert("dea".into(), dea[index].rounded(4).to_value());
        item.insert(
            "macd_hist".into(),
            dif[index]
                .sub(dea[index])
                .mul(PyNum::Int(2))
                .rounded(4)
                .to_value(),
        );
        rows.push(Value::Object(item));
    }
    Ok(Value::Array(rows))
}

/// `calc_rsi`: simple-average RSI (not Wilder).
pub fn calc_rsi(klines: &Value, periods: Option<&Value>) -> Result<Value, ProviderError> {
    let candles = validate_klines(klines, &["close"])?;
    let periods = periods_arg(periods, &[6, 12, 24])?;
    let closes: Vec<f64> = candles.iter().map(|candle| candle.close).collect();
    let mut changes = vec![0.0];
    for index in 1..closes.len() {
        changes.push(closes[index] - closes[index - 1]);
    }
    let gains: Vec<f64> = changes.iter().map(|change| change.max(0.0)).collect();
    let losses: Vec<f64> = changes.iter().map(|change| (-change).max(0.0)).collect();
    let mut rows = Vec::with_capacity(candles.len());
    for (index, candle) in candles.iter().enumerate() {
        let mut item = row(&candle.date, &candle.close_raw);
        for period in &periods {
            let period = *period as usize;
            if index < period {
                item.insert(format!("rsi{period}"), Value::Null);
                continue;
            }
            let avg_gain =
                python_sum(gains[index + 1 - period..=index].iter().copied()) / period as f64;
            let avg_loss =
                python_sum(losses[index + 1 - period..=index].iter().copied()) / period as f64;
            let value = if avg_loss == 0.0 {
                100.0
            } else {
                let rs = avg_gain / avg_loss;
                py_round(100.0 - 100.0 / (1.0 + rs), 2)
            };
            item.insert(format!("rsi{period}"), Value::from(value));
        }
        rows.push(Value::Object(item));
    }
    Ok(Value::Array(rows))
}

/// `calc_kdj`: RSV with K/D smoothing.
pub fn calc_kdj(
    klines: &Value,
    n: Option<&Value>,
    m1: Option<&Value>,
    m2: Option<&Value>,
) -> Result<Value, ProviderError> {
    let candles = validate_klines(klines, &["close", "high", "low"])?;
    let n = positive_int_or(n, "n", 9)?;
    let m1 = positive_int_or(m1, "m1", 3)?;
    let m2 = positive_int_or(m2, "m2", 3)?;
    let mut k_value = 50.0f64;
    let mut d_value = 50.0f64;
    let mut rows = Vec::with_capacity(candles.len());
    for (index, candle) in candles.iter().enumerate() {
        if index + 1 < n as usize {
            let mut item = row(&candle.date, &candle.close_raw);
            item.insert("k".into(), Value::Null);
            item.insert("d".into(), Value::Null);
            item.insert("j".into(), Value::Null);
            rows.push(Value::Object(item));
            continue;
        }
        let start = index + 1 - n as usize;
        let window = &candles[start..=index];
        let high_n = window
            .iter()
            .filter_map(|row| row.high)
            .fold(f64::NEG_INFINITY, f64::max);
        let low_n = window
            .iter()
            .filter_map(|row| row.low)
            .fold(f64::INFINITY, f64::min);
        let rsv = if high_n != low_n {
            (candle.close - low_n) / (high_n - low_n) * 100.0
        } else {
            50.0
        };
        k_value = (1.0 / m1 as f64) * rsv + (1.0 - 1.0 / m1 as f64) * k_value;
        d_value = (1.0 / m2 as f64) * k_value + (1.0 - 1.0 / m2 as f64) * d_value;
        let j_value = 3.0 * k_value - 2.0 * d_value;
        let mut item = row(&candle.date, &candle.close_raw);
        item.insert("k".into(), Value::from(py_round(k_value, 2)));
        item.insert("d".into(), Value::from(py_round(d_value, 2)));
        item.insert("j".into(), Value::from(py_round(j_value, 2)));
        rows.push(Value::Object(item));
    }
    Ok(Value::Array(rows))
}

/// `calc_boll`: Bollinger bands with optional bandwidth.
pub fn calc_boll(
    klines: &Value,
    period: Option<&Value>,
    num_std: Option<&Value>,
) -> Result<Value, ProviderError> {
    let candles = validate_klines(klines, &["close"])?;
    let period = positive_int_or(period, "period", 20)? as usize;
    let num_std = match num_std {
        None => 2.0,
        Some(value) => {
            let Some(number) = num(value) else {
                return Err(value_error("num_std must be a finite nonnegative number."));
            };
            if number < 0.0 {
                return Err(value_error("num_std must be a finite nonnegative number."));
            }
            number
        }
    };
    let closes: Vec<f64> = candles.iter().map(|candle| candle.close).collect();
    let mut rows = Vec::with_capacity(candles.len());
    for (index, candle) in candles.iter().enumerate() {
        if index + 1 < period {
            let mut item = row(&candle.date, &candle.close_raw);
            for key in ["upper", "middle", "lower", "bandwidth"] {
                item.insert(key.into(), Value::Null);
            }
            rows.push(Value::Object(item));
            continue;
        }
        let window = &closes[index + 1 - period..=index];
        let ma = python_sum(window.iter().copied()) / period as f64;
        let variance = python_sum(window.iter().map(|close| (close - ma).powi(2))) / period as f64;
        let std = variance.sqrt();
        let upper = ma + num_std * std;
        let lower = ma - num_std * std;
        let mut item = row(&candle.date, &candle.close_raw);
        item.insert("upper".into(), Value::from(py_round(upper, 4)));
        item.insert("middle".into(), Value::from(py_round(ma, 4)));
        item.insert("lower".into(), Value::from(py_round(lower, 4)));
        item.insert(
            "bandwidth".into(),
            if ma != 0.0 {
                Value::from(py_round((upper - lower) / ma * 100.0, 2))
            } else {
                Value::Null
            },
        );
        rows.push(Value::Object(item));
    }
    Ok(Value::Array(rows))
}

/// `parse_osi`: OCC option symbol to expiry/type/strike (empty object when invalid).
pub fn parse_osi(symbol: &str) -> Value {
    // Root `[A-Z][A-Z0-9]*` is greedy, so the fixed-width suffix forces the legacy
    // regex to backtrack; try every root length from longest to shortest.
    let bytes = symbol.as_bytes();
    for root_end in (1..bytes.len()).rev() {
        if !symbol.is_char_boundary(root_end) {
            continue;
        }
        if !bytes[0].is_ascii_uppercase() {
            return Value::Object(Map::new());
        }
        if !bytes[..root_end]
            .iter()
            .enumerate()
            .all(|(index, byte)| byte.is_ascii_uppercase() || (index > 0 && byte.is_ascii_digit()))
        {
            continue;
        }
        let rest = &symbol[root_end..];
        if rest.len() != 15 {
            continue;
        }
        let (date_part, type_strike) = rest.split_at(6);
        let (call_put, strike_part) = type_strike.split_at(1);
        if !date_part
            .chars()
            .all(|character| character.is_ascii_digit())
            || !strike_part
                .chars()
                .all(|character| character.is_ascii_digit())
            || (call_put != "C" && call_put != "P")
        {
            continue;
        }
        let year = &date_part[0..2];
        let month = &date_part[2..4];
        let day = &date_part[4..6];
        let strike: f64 = strike_part.parse::<f64>().unwrap_or(0.0) / 1000.0;
        return json!({
            "expiry": format!("20{year}-{month}-{day}"),
            "type": if call_put == "C" { "call" } else { "put" },
            "strike": strike,
        });
    }
    Value::Object(Map::new())
}

fn et_today() -> String {
    Utc::now()
        .with_timezone(&New_York)
        .format("%Y-%m-%d")
        .to_string()
}

fn parse_date(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()
}

/// `_date`: parse and require the canonical `%Y-%m-%d` round-trip (Python rejects
/// unpadded fields even though `strptime` accepts them).
fn validated_date(value: &str, name: &str) -> Result<NaiveDate, ProviderError> {
    let trimmed = value.trim();
    let parts: Vec<&str> = trimmed.split('-').collect();
    let structurally_valid = parts.len() == 3
        && parts[0].len() == 4
        && parts[0].chars().all(|character| character.is_ascii_digit())
        && (1..=2).contains(&parts[1].len())
        && parts[1].chars().all(|character| character.is_ascii_digit())
        && (1..=2).contains(&parts[2].len())
        && parts[2].chars().all(|character| character.is_ascii_digit());
    if !structurally_valid {
        return Err(value_error(format!(
            "{name} has an invalid date format or value."
        )));
    }
    let Ok(parsed) = NaiveDate::parse_from_str(trimmed, "%Y-%m-%d") else {
        return Err(value_error(format!(
            "{name} has an invalid date format or value."
        )));
    };
    if parsed.format("%Y-%m-%d").to_string() != trimmed {
        return Err(value_error(format!("{name} has an invalid date format.")));
    }
    Ok(parsed)
}

/// `filter_expiry`: filter chain contracts by expiry/0DTE/days-to-expiry.
pub fn filter_expiry(
    chain: &Value,
    expiry: Option<&str>,
    dte_max: Option<i64>,
) -> Result<Value, ProviderError> {
    if let Some(expiry) = expiry {
        if expiry != "0DTE" {
            validated_date(expiry, "expiry")?;
        }
    }
    if let Some(dte_max) = dte_max {
        if dte_max < 0 {
            return Err(value_error("dte_max must be a nonnegative integer."));
        }
    }
    let contracts = require_chain(chain)?;
    let selected: Vec<Value> = match expiry {
        Some("0DTE") => {
            let today = et_today();
            contracts
                .iter()
                .filter(|contract| contract["expiry"].as_str() == Some(today.as_str()))
                .cloned()
                .collect()
        }
        Some(expiry) => contracts
            .iter()
            .filter(|contract| contract["expiry"].as_str() == Some(expiry))
            .cloned()
            .collect(),
        None => {
            if let Some(dte_max) = dte_max {
                let today =
                    parse_date(&et_today()).ok_or_else(|| value_error("invalid provider date"))?;
                contracts
                    .iter()
                    .filter(|contract| {
                        let Some(raw) = contract["expiry"].as_str() else {
                            return false;
                        };
                        let Some(expiry) = parse_date(raw) else {
                            return false;
                        };
                        let days = (expiry - today).num_days();
                        (0..=dte_max).contains(&days)
                    })
                    .cloned()
                    .collect()
            } else {
                contracts.clone()
            }
        }
    };
    Ok(Value::Array(selected))
}

/// `unusual_activity`: high volume relative to reported open interest.
pub fn unusual_activity(
    contracts: &Value,
    min_volume: Option<&Value>,
    vol_oi_min: Option<&Value>,
) -> Result<Value, ProviderError> {
    let min_volume = positive_int_or(min_volume, "min_volume", 500)?;
    let vol_oi_min = match vol_oi_min {
        None => 1.0,
        Some(value) => {
            let Some(number) = num(value) else {
                return Err(value_error(
                    "vol_oi_min must be a finite nonnegative number.",
                ));
            };
            if number < 0.0 {
                return Err(value_error(
                    "vol_oi_min must be a finite nonnegative number.",
                ));
            }
            number
        }
    };
    let rows = contracts
        .as_array()
        .ok_or_else(|| value_error("contracts must be a list of contracts."))?;
    let mut result: Vec<(f64, Value)> = Vec::new();
    for contract in rows {
        let Some(object) = contract.as_object() else {
            continue;
        };
        let Some(volume) = num_of(object, "volume") else {
            continue;
        };
        let Some(interest) = num_of(object, "open_interest") else {
            continue;
        };
        if interest <= 0.0 || volume < min_volume as f64 {
            continue;
        }
        let ratio = volume / interest;
        if ratio >= vol_oi_min {
            let mut item = object.clone();
            item.insert("vol_oi_ratio".into(), Value::from(ratio));
            result.push((volume, Value::Object(item)));
        }
    }
    result.sort_by(|left, right| {
        right
            .0
            .partial_cmp(&left.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    Ok(Value::Array(
        result.into_iter().map(|(_, row)| row).collect(),
    ))
}

fn total(rows: &[&Map<String, Value>], field: &str) -> Option<f64> {
    let present: Vec<f64> = rows.iter().filter_map(|row| num_of(row, field)).collect();
    if present.is_empty() {
        None
    } else {
        Some(python_sum(present.iter().copied()))
    }
}

/// `chain_summary`: aggregate contract activity with a volume-weighted delta proxy.
pub fn chain_summary(contracts: &Value) -> Result<Value, ProviderError> {
    let rows = contracts
        .as_array()
        .ok_or_else(|| value_error("contracts must be a list of contracts."))?;
    let objects: Vec<&Map<String, Value>> = rows.iter().filter_map(Value::as_object).collect();
    let calls: Vec<&Map<String, Value>> = objects
        .iter()
        .copied()
        .filter(|row| row.get("type").and_then(Value::as_str) == Some("call"))
        .collect();
    let puts: Vec<&Map<String, Value>> = objects
        .iter()
        .copied()
        .filter(|row| row.get("type").and_then(Value::as_str) == Some("put"))
        .collect();
    let call_volume = total(&calls, "volume");
    let put_volume = total(&puts, "volume");
    let call_oi = total(&calls, "open_interest");
    let put_oi = total(&puts, "open_interest");
    let traded: Vec<&Map<String, Value>> = objects
        .iter()
        .copied()
        .filter(|row| num_of(row, "volume").unwrap_or(0.0) > 0.0)
        .collect();
    let iv_rows: Vec<&Map<String, Value>> = traded
        .iter()
        .copied()
        .filter(|row| num_of(row, "iv").is_some())
        .collect();
    let iv_volume: f64 = python_sum(
        iv_rows
            .iter()
            .filter_map(|row| num_of(row, "volume"))
            .collect::<Vec<f64>>()
            .iter()
            .copied(),
    );
    let volume_weighted_iv = if iv_volume != 0.0 {
        let weighted: Vec<f64> = iv_rows
            .iter()
            .map(|row| num_of(row, "iv").unwrap_or(0.0) * num_of(row, "volume").unwrap_or(0.0))
            .collect();
        Some(python_sum(weighted.iter().copied()) / iv_volume)
    } else {
        None
    };
    let delta_rows: Vec<&Map<String, Value>> = objects
        .iter()
        .copied()
        .filter(|row| num_of(row, "delta").is_some() && num_of(row, "volume").is_some())
        .collect();
    let delta_proxy = if delta_rows.is_empty() {
        None
    } else {
        let values: Vec<f64> = delta_rows
            .iter()
            .map(|row| {
                num_of(row, "delta").unwrap_or(0.0) * num_of(row, "volume").unwrap_or(0.0) * 100.0
            })
            .collect();
        Some(python_sum(values.iter().copied()))
    };
    let put_call_volume_ratio = match (call_volume, put_volume) {
        (Some(call_volume), Some(put_volume)) if call_volume != 0.0 => {
            Some(put_volume / call_volume)
        }
        _ => None,
    };
    let put_call_oi_ratio = match (call_oi, put_oi) {
        (Some(call_oi), Some(put_oi)) if call_oi != 0.0 => Some(put_oi / call_oi),
        _ => None,
    };
    let missing = |items: &[&Map<String, Value>], field: &str| -> Value {
        Value::from(
            items
                .iter()
                .filter(|row| num_of(row, field).is_none())
                .count(),
        )
    };
    Ok(json!({
        "call_volume": call_volume.map(Value::from).unwrap_or(Value::Null),
        "put_volume": put_volume.map(Value::from).unwrap_or(Value::Null),
        "put_call_volume_ratio": put_call_volume_ratio.map(Value::from).unwrap_or(Value::Null),
        "call_oi": call_oi.map(Value::from).unwrap_or(Value::Null),
        "put_oi": put_oi.map(Value::from).unwrap_or(Value::Null),
        "put_call_oi_ratio": put_call_oi_ratio.map(Value::from).unwrap_or(Value::Null),
        "volume_weighted_iv": volume_weighted_iv.map(Value::from).unwrap_or(Value::Null),
        "volume_weighted_delta_proxy_shares": delta_proxy.map(Value::from).unwrap_or(Value::Null),
        "delta_proxy_contract_multiplier": 100,
        "delta_proxy_note": "Volume proxy; trade direction and position ownership are unknown.",
        "contracts_total": objects.len(),
        "contracts_traded": traded.len(),
        "contracts_missing_volume": missing(&objects, "volume"),
        "contracts_missing_open_interest": missing(&objects, "open_interest"),
        "contracts_missing_iv": missing(&traded, "iv"),
        "contracts_missing_delta": missing(&objects, "delta"),
    }))
}

/// `short_volume_ranking`: top symbols by short-volume ratio above a total floor.
pub fn short_volume_ranking(
    snapshot: &Value,
    min_total: Option<f64>,
    top: Option<i64>,
) -> Result<Value, ProviderError> {
    let min_total = min_total.unwrap_or(1_000_000.0);
    let top = top.unwrap_or(20);
    let data = snapshot
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| key_error("data"))?;
    let mut rows: Vec<(f64, Value)> = Vec::new();
    for (symbol, record) in data {
        let Some(object) = record.as_object() else {
            continue;
        };
        let Some(total) = object.get("total") else {
            return Err(key_error("total"));
        };
        let Some(ratio_value) = object.get("ratio") else {
            return Err(key_error("ratio"));
        };
        let total = total.as_f64().unwrap_or(0.0);
        if total < min_total {
            continue;
        }
        let Some(ratio) = num(ratio_value) else {
            continue;
        };
        let mut item = Map::new();
        item.insert("symbol".into(), Value::String(symbol.clone()));
        for (key, value) in object {
            item.insert(key.clone(), value.clone());
        }
        let _ = total;
        rows.push((ratio, Value::Object(item)));
    }
    rows.sort_by(|left, right| {
        right
            .0
            .partial_cmp(&left.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let rows: Vec<Value> = rows.into_iter().map(|(_, row)| row).collect();
    Ok(Value::Array(python_slice(rows, top)))
}

fn python_slice(mut items: Vec<Value>, top: i64) -> Vec<Value> {
    if top >= 0 {
        let limit = top as usize;
        if items.len() > limit {
            items.truncate(limit);
        }
        items
    } else {
        let drop = top.unsigned_abs() as usize;
        let keep = items.len().saturating_sub(drop);
        items.truncate(keep);
        items
    }
}

/// `frame_ranking`: sort one SEC frame's rows by value.
pub fn frame_ranking(
    frame: &Value,
    top: Option<i64>,
    ascending: Option<bool>,
) -> Result<Value, ProviderError> {
    let data = frame
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| key_error("data"))?;
    let ascending = ascending.unwrap_or(false);
    let top = top.unwrap_or(20);
    let mut rows: Vec<(f64, Value)> = Vec::with_capacity(data.len());
    let mut missing_value = false;
    let mut numeric_type = "float";
    for row in data {
        let Some(raw) = row.get("value") else {
            return Err(key_error("value"));
        };
        match num(raw) {
            Some(value) => {
                if rows.is_empty() {
                    numeric_type = if raw.as_i64().is_some() {
                        "int"
                    } else {
                        "float"
                    };
                }
                rows.push((value, row.clone()));
            }
            None => missing_value = true,
        }
    }
    if missing_value {
        let message = if ascending {
            format!("'<' not supported between instances of 'NoneType' and '{numeric_type}'")
        } else {
            format!("'<' not supported between instances of '{numeric_type}' and 'NoneType'")
        };
        return Err(ProviderError::new(ProviderErrorKind::TypeError, message));
    }
    rows.sort_by(|left, right| {
        let ordering = left
            .0
            .partial_cmp(&right.0)
            .unwrap_or(std::cmp::Ordering::Equal);
        if ascending {
            ordering
        } else {
            ordering.reverse()
        }
    });
    let rows: Vec<Value> = rows.into_iter().map(|(_, row)| row).collect();
    Ok(Value::Array(python_slice(rows, top)))
}

/// `frame_screen`: filter one SEC frame's rows by value range.
pub fn frame_screen(
    frame: &Value,
    min_value: Option<f64>,
    max_value: Option<f64>,
) -> Result<Value, ProviderError> {
    let data = frame
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| key_error("data"))?;
    let mut result: Vec<Value> = Vec::new();
    for row in data {
        let Some(raw) = row.get("value") else {
            return Err(key_error("value"));
        };
        let Some(value) = num(raw) else {
            let (operator, bound) = if let Some(maximum) = max_value {
                ("<=", maximum)
            } else {
                (">=", min_value.unwrap_or(0.0))
            };
            let bound_type = if raw.as_i64().is_some() || bound.fract() == 0.0 {
                "int"
            } else {
                "float"
            };
            return Err(ProviderError::new(
                ProviderErrorKind::TypeError,
                format!(
                    "'{operator}' not supported between instances of 'NoneType' and '{bound_type}'"
                ),
            ));
        };
        if let Some(minimum) = min_value {
            if value < minimum {
                continue;
            }
        }
        if let Some(maximum) = max_value {
            if value > maximum {
                continue;
            }
        }
        result.push(row.clone());
    }
    Ok(Value::Array(result))
}

fn opt_f64(params: &Map<String, Value>, name: &str) -> Result<Option<f64>, ProviderError> {
    match params.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => num(value)
            .map(Some)
            .ok_or_else(|| value_error(format!("Parameter {name} must have type float."))),
    }
}

/// Dispatch one R12 local function by name.
pub fn call(function: &str, params: &Map<String, Value>) -> Result<Value, ProviderError> {
    match function {
        "calc_ma" => calc_ma(
            params.get("klines").unwrap_or(&Value::Null),
            params.get("periods"),
        ),
        "calc_macd" => calc_macd(
            params.get("klines").unwrap_or(&Value::Null),
            params.get("fast"),
            params.get("slow"),
            params.get("signal"),
        ),
        "calc_rsi" => calc_rsi(
            params.get("klines").unwrap_or(&Value::Null),
            params.get("periods"),
        ),
        "calc_kdj" => calc_kdj(
            params.get("klines").unwrap_or(&Value::Null),
            params.get("n"),
            params.get("m1"),
            params.get("m2"),
        ),
        "calc_boll" => calc_boll(
            params.get("klines").unwrap_or(&Value::Null),
            params.get("period"),
            params.get("num_std"),
        ),
        "parse_osi" => Ok(parse_osi(require_str(params, "symbol")?)),
        "filter_expiry" => filter_expiry(
            params.get("chain").unwrap_or(&Value::Null),
            params.get("expiry").and_then(Value::as_str),
            params.get("dte_max").and_then(Value::as_i64),
        ),
        "unusual_activity" => unusual_activity(
            params.get("contracts").unwrap_or(&Value::Null),
            params.get("min_volume"),
            params.get("vol_oi_min"),
        ),
        "chain_summary" => chain_summary(params.get("contracts").unwrap_or(&Value::Null)),
        "short_volume_ranking" => short_volume_ranking(
            params.get("snapshot").unwrap_or(&Value::Null),
            opt_f64(params, "min_total")?,
            params.get("top").and_then(Value::as_i64),
        ),
        "frame_ranking" => frame_ranking(
            params.get("frame").unwrap_or(&Value::Null),
            params.get("top").and_then(Value::as_i64),
            params.get("ascending").and_then(Value::as_bool),
        ),
        "frame_screen" => frame_screen(
            params.get("frame").unwrap_or(&Value::Null),
            opt_f64(params, "min_value")?,
            opt_f64(params, "max_value")?,
        ),
        other => Err(value_error(format!(
            "Function is not in the allowed list; use --list. ({other})"
        ))),
    }
}

/// Whether one gateway function is an implemented R12 local function.
pub fn is_local_function(function: &str) -> bool {
    matches!(
        function,
        "calc_ma"
            | "calc_macd"
            | "calc_rsi"
            | "calc_kdj"
            | "calc_boll"
            | "parse_osi"
            | "filter_expiry"
            | "unusual_activity"
            | "chain_summary"
            | "short_volume_ranking"
            | "frame_ranking"
            | "frame_screen"
    )
}
