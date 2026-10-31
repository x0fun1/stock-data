//! Pure Yahoo/yfinance mappings (`yfinance_normalize.py`).
//!
//! Native frames are represented as indexed row records (not invented HTTP payloads).
//! Missing values never become zero; source identity/calendar evidence must be supplied by
//! the caller. Every number/date/timestamp rule mirrors the legacy normalizer so the
//! differential fixtures compare identical trees.

use std::collections::BTreeSet;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone, Utc};
use serde_json::{Map, Value};

/// Names/representations accepted by the normalizers.
pub type Rows = Vec<Value>;

fn number(value: Option<&Value>) -> Option<Value> {
    match value? {
        Value::Number(number) => {
            if number.is_i64() || number.is_u64() {
                Some(Value::Number(number.clone()))
            } else {
                number
                    .as_f64()
                    .filter(|float| float.is_finite())
                    .map(Value::from)
            }
        }
        _ => None,
    }
}

fn number_f64(value: Option<&Value>) -> Option<f64> {
    match number(value)? {
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

/// Parse the frame index value the way `_datetime` does.
fn parse_datetime(value: &Value) -> Option<DateTime<Utc>> {
    match value {
        Value::Null => None,
        Value::Number(number) => {
            number
                .as_f64()
                .filter(|float| float.is_finite())
                .and_then(|seconds| {
                    let whole = seconds.trunc() as i64;
                    let subsec = ((seconds.fract()) * 1e9).round() as u32;
                    Utc.timestamp_opt(whole, subsec.min(999_999_999)).single()
                })
        }
        Value::String(text) => parse_iso_datetime(text),
        _ => None,
    }
}

/// Python `datetime.fromisoformat(value.replace("Z", "+00:00"))` for the accepted shapes.
fn parse_iso_datetime(text: &str) -> Option<DateTime<Utc>> {
    if let Ok(parsed) = DateTime::parse_from_rfc3339(text) {
        return Some(parsed.with_timezone(&Utc));
    }
    let trimmed = text.trim();
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(trimmed, format) {
            return Some(Utc.from_utc_datetime(&parsed));
        }
    }
    NaiveDate::parse_from_str(trimmed, "%Y-%m-%d")
        .ok()
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|time| Utc.from_utc_datetime(&time))
}

fn iso_utc(stamp: DateTime<Utc>) -> String {
    let nanos = stamp.timestamp_subsec_nanos();
    if nanos == 0 {
        stamp.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        stamp.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

fn iso_local(stamp: DateTime<Utc>, timezone: Option<chrono_tz::Tz>) -> String {
    match timezone {
        Some(zone) => {
            let local = stamp.with_timezone(&zone);
            let nanos = local.timestamp_subsec_nanos();
            if nanos == 0 {
                local.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
            } else {
                local.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
            }
        }
        None => iso_utc(stamp),
    }
}

/// One row's index: the list branch of `_rows` uses `timestamp`, then `date`, then `index`.
fn row_index(row: &Value) -> Value {
    for key in ["timestamp", "date", "index"] {
        if let Some(value) = row.get(key) {
            return value.clone();
        }
    }
    Value::Null
}

/// `json_safe` for already-JSON values (numbers stay numbers, non-finite become null).
pub fn json_safe(value: &Value) -> Value {
    match value {
        Value::Number(number) => number
            .as_f64()
            .map(|float| {
                if float.is_finite() {
                    Value::Number(number.clone())
                } else {
                    Value::Null
                }
            })
            .unwrap_or(Value::Null),
        Value::Array(items) => Value::Array(items.iter().map(json_safe).collect()),
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, item) in map {
                out.insert(key.clone(), json_safe(item));
            }
            Value::Object(out)
        }
        other => other.clone(),
    }
}

fn row_field<'a>(row: &'a Value, keys: &[&str]) -> Option<&'a Value> {
    keys.iter().find_map(|key| row.get(*key))
}

fn utc_or_null(stamp: Option<DateTime<Utc>>) -> Value {
    stamp
        .map(|value| Value::String(iso_utc(value)))
        .unwrap_or(Value::Null)
}

/// Parse a timestamp and report whether the source value carried a timezone.
fn parse_aware_datetime(value: &Value) -> Option<(DateTime<Utc>, bool)> {
    match value {
        Value::Null => None,
        Value::Number(number) => {
            let seconds = number.as_f64().filter(|float| float.is_finite())?;
            let whole = seconds.trunc() as i64;
            let subsec = ((seconds.fract()) * 1e9).round() as u32;
            Some((
                Utc.timestamp_opt(whole, subsec.min(999_999_999)).single()?,
                true,
            ))
        }
        Value::String(text) => {
            if let Ok(parsed) = DateTime::parse_from_rfc3339(text) {
                return Some((parsed.with_timezone(&Utc), true));
            }
            let trimmed = text.trim();
            for format in [
                "%Y-%m-%dT%H:%M:%S%.f",
                "%Y-%m-%d %H:%M:%S%.f",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M",
            ] {
                if let Ok(parsed) = NaiveDateTime::parse_from_str(trimmed, format) {
                    return Some((Utc.from_utc_datetime(&parsed), false));
                }
            }
            NaiveDate::parse_from_str(trimmed, "%Y-%m-%d")
                .ok()
                .and_then(|date| date.and_hms_opt(0, 0, 0))
                .map(|time| (Utc.from_utc_datetime(&time), false))
        }
        _ => None,
    }
}

/// Normalize one history frame into the documented bars envelope.
pub fn normalize_history(
    frame: &Value,
    metadata: &Value,
    price_basis: &str,
    symbol: Option<&str>,
    interval: &str,
) -> Result<Value, String> {
    if price_basis != "provider" && price_basis != "adjusted" {
        return Err("price_basis must be provider or adjusted".to_string());
    }
    let metadata_map = metadata.as_object().cloned().unwrap_or_default();
    let tz_name = metadata_map
        .get("exchangeTimezoneName")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let mut warnings: Vec<String> = Vec::new();
    let exchange_tz: Option<chrono_tz::Tz> = match tz_name
        .as_ref()
        .and_then(|name| name.parse::<chrono_tz::Tz>().ok())
    {
        Some(zone) => Some(zone),
        None => {
            if tz_name.is_some() {
                warnings.push("invalid_exchange_timezone".to_string());
            }
            warnings.push("exchange_timezone_unconfirmed".to_string());
            None
        }
    };
    let mut gaps = 0usize;
    let mut bars: Vec<Value> = Vec::new();
    let mut actions: Vec<Value> = Vec::new();
    let mut ratios: Vec<Value> = Vec::new();
    let rows: Vec<Value> = match frame {
        Value::Array(items) => items.clone(),
        Value::Null => Vec::new(),
        _ => return Err("Expected DataFrame or indexed row records".to_string()),
    };
    for row in &rows {
        let index = row_index(row);
        let Some((utc_stamp, aware)) = parse_aware_datetime(&index) else {
            warnings.push("invalid_bar_timestamp".to_string());
            gaps += 1;
            continue;
        };
        // A naive label is interpreted in the exchange zone (Python replaces tzinfo),
        // which makes it an aware instant; without a zone it stays unconfirmed.
        let mut stamp_utc = if aware { Some(utc_stamp) } else { None };
        let mut effective_aware = aware;
        if !aware {
            if let Some(zone) = exchange_tz {
                if let Some(local) = zone.from_local_datetime(&utc_stamp.naive_utc()).single() {
                    stamp_utc = Some(local.with_timezone(&Utc));
                    effective_aware = true;
                }
            }
        }
        let local_date = match exchange_tz {
            Some(zone) => stamp_utc
                .map(|stamp| stamp.with_timezone(&zone).date_naive())
                .unwrap_or_else(|| utc_stamp.date_naive()),
            None => utc_stamp.date_naive(),
        };
        let raw_fields = [
            ("open", "Open"),
            ("high", "High"),
            ("low", "Low"),
            ("close", "Close"),
        ];
        let mut raw = Map::new();
        for (key, column) in raw_fields {
            raw.insert(
                key.to_string(),
                number(row_field(row, &[column, key])).unwrap_or(Value::Null),
            );
        }
        let adj = number(row_field(row, &["Adj Close", "adj_close"]));
        let close = raw.get("close").and_then(Value::as_f64);
        let adj_number = adj.as_ref().and_then(Value::as_f64);
        let mut ratio = match (adj_number, close) {
            (Some(adj), Some(close)) if close > 0.0 && adj > 0.0 => Some(adj / close),
            _ => None,
        };
        if let Some(value) = ratio {
            if !value.is_finite() || value <= 0.0 {
                ratio = None;
            }
        }
        let mut bar = Map::new();
        bar.insert(
            "date".into(),
            Value::String(local_date.format("%Y-%m-%d").to_string()),
        );
        bar.insert("timestamp".into(), utc_or_null(stamp_utc));
        bar.insert("timestamp_utc".into(), utc_or_null(stamp_utc));
        bar.insert(
            "timezone".into(),
            tz_name
                .as_ref()
                .map(|name| Value::String(name.clone()))
                .unwrap_or(Value::Null),
        );
        bar.insert("provider_ohlc".into(), Value::Object(raw.clone()));
        bar.insert("adj_close".into(), adj.clone().unwrap_or(Value::Null));
        bar.insert(
            "volume".into(),
            number(row_field(row, &["Volume", "volume"])).unwrap_or(Value::Null),
        );
        bar.insert(
            "adjustment_ratio".into(),
            ratio.map(Value::from).unwrap_or(Value::Null),
        );
        bar.insert("price_basis".into(), Value::String(price_basis.to_string()));
        let missing: Vec<Value> = ["open", "high", "low", "close"]
            .iter()
            .filter(|key| raw.get(**key).map(Value::is_null).unwrap_or(true))
            .map(|key| Value::String((*key).to_string()))
            .collect();
        bar.insert("missing_fields".into(), Value::Array(missing));
        for key in ["open", "high", "low", "close"] {
            let value = if price_basis == "provider" {
                raw.get(key).cloned().unwrap_or(Value::Null)
            } else {
                let source = raw.get(key).and_then(Value::as_f64);
                match (source, ratio) {
                    (Some(value), Some(ratio_provider)) => {
                        json_safe(&Value::from(value * ratio_provider))
                    }
                    _ => Value::Null,
                }
            };
            bar.insert(key.to_string(), value);
        }
        for (key, column) in [
            ("dividends", "Dividends"),
            ("stock_splits", "Stock Splits"),
            ("capital_gains", "Capital Gains"),
        ] {
            bar.insert(
                key.to_string(),
                number(row_field(row, &[column, key])).unwrap_or(Value::Null),
            );
        }
        if let Some(flag) = row.get("Repaired?") {
            bar.insert("repaired_flag".into(), json_safe(flag));
        }
        if price_basis == "adjusted" && ratio.is_none() {
            bar.insert(
                "adjustment_gap".into(),
                Value::String("invalid_or_missing_adj_close_ratio".to_string()),
            );
            gaps += 1;
        }
        if raw.get("close").map(Value::is_null).unwrap_or(true) {
            gaps += 1;
        }
        if !effective_aware {
            warnings.push("bar_timezone_unconfirmed".to_string());
        }
        ratios.push(serde_json::json!({
            "date": bar.get("date").cloned().unwrap_or(Value::Null),
            "timestamp_utc": bar.get("timestamp_utc").cloned().unwrap_or(Value::Null),
            "ratio": ratio.map(Value::from).unwrap_or(Value::Null),
        }));
        for (key, kind) in [
            ("dividends", "dividend"),
            ("stock_splits", "split"),
            ("capital_gains", "capital_gain"),
        ] {
            let value = bar.get(key).cloned().unwrap_or(Value::Null);
            let numeric = value.as_f64();
            if numeric.is_some() && numeric != Some(0.0) {
                actions.push(serde_json::json!({
                    "date": bar.get("date").cloned().unwrap_or(Value::Null),
                    "type": kind,
                    "kind": key,
                    "value": value,
                }));
            }
        }
        bars.push(Value::Object(bar));
    }
    if price_basis == "adjusted" && bars.iter().any(|bar| bar.get("adjustment_gap").is_some()) {
        warnings.push("adjusted_view_has_ratio_gaps".to_string());
    }
    let dates: Vec<String> = bars
        .iter()
        .filter_map(|bar| {
            bar.get("date")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect();
    let source_times: Vec<String> = bars
        .iter()
        .filter_map(|bar| bar.get("timestamp_utc").and_then(Value::as_str))
        .filter(|text| !text.is_empty())
        .map(ToString::to_string)
        .collect();
    let source_symbol = metadata_map
        .get("source_symbol")
        .or_else(|| metadata_map.get("symbol"))
        .cloned()
        .unwrap_or(Value::Null);
    let transformation = if price_basis == "adjusted" {
        "Adj Close / Close applied to OHLC; volume unchanged"
    } else {
        "none"
    };
    let bar_count = bars.len();
    let unique_warnings: Vec<Value> = warnings
        .into_iter()
        .collect::<BTreeSet<String>>()
        .into_iter()
        .map(Value::String)
        .collect();
    let mut data = Map::new();
    data.insert("bars".into(), Value::Array(bars));
    data.insert(
        "history_window".into(),
        serde_json::json!({
            "start": dates.iter().min().cloned().map(Value::String).unwrap_or(Value::Null),
            "end": dates.iter().max().cloned().map(Value::String).unwrap_or(Value::Null),
            "selection_rule": "actual_returned_bars; closure not asserted",
        }),
    );
    data.insert("symbol".into(), source_symbol.clone());
    data.insert("source_symbol".into(), source_symbol.clone());
    data.insert(
        "currency".into(),
        metadata_map.get("currency").cloned().unwrap_or(Value::Null),
    );
    data.insert("frequency".into(), Value::String(interval.to_string()));
    let mut evidence = Map::new();
    evidence.insert("source".into(), Value::String("Yahoo Finance".into()));
    evidence.insert("price_basis".into(), Value::String(price_basis.to_string()));
    evidence.insert(
        "checked_through".into(),
        dates
            .iter()
            .max()
            .cloned()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    evidence.insert("actions".into(), Value::Array(actions));
    evidence.insert(
        "action_schema_version".into(),
        Value::String("yahoo_actions_v2".into()),
    );
    evidence.insert("action_coverage_confirmed".into(), Value::Bool(false));
    evidence.insert(
        "coverage_scope".into(),
        Value::String("returned_bars_only".into()),
    );
    evidence.insert("coverage_note".into(), Value::String(
        "observed actions only, scoped to the returned bars; an empty list does not prove that no action occurred, and a provider omission is undetectable here. The freeze gate reconciles any listed split against the close-to-close step of the series".into(),
    ));
    evidence.insert(
        "transformation".into(),
        Value::String(transformation.into()),
    );
    evidence.insert("ratios".into(), Value::Array(ratios));
    let status = if bar_count == 0 {
        "empty"
    } else if gaps > 0 {
        "partial"
    } else {
        "success"
    };
    Ok(json_safe(&serde_json::json!({
        "status": status,
        "actual_source": "Yahoo Finance",
        "requested_symbol": symbol.map(Value::from).unwrap_or(Value::Null),
        "source_symbol": source_symbol,
        "currency": metadata_map.get("currency").cloned().unwrap_or(Value::Null),
        "frequency": interval,
        "adjustment": price_basis,
        "adjustment_evidence": Value::Object(evidence),
        "source_timestamp": source_times.iter().max().cloned().map(Value::String).unwrap_or(Value::Null),
        "source_timestamp_semantics": "bar_label_utc",
        "last_bar_closed": Value::Null,
        "metadata": Value::Object(metadata_map),
        "data": Value::Object(data),
        "warnings": Value::Array(unique_warnings),
        "native_payload_kind": "yfinance_processed_result_not_raw_http",
    })))
}

/// Compare all values, including Adj Close/actions; a flag alone is insufficient.
pub fn repair_diff(
    original: &Value,
    repaired: &Value,
    original_metadata: &Value,
    repaired_metadata: &Value,
) -> Value {
    let to_map = |frame: &Value| -> Map<String, Value> {
        let mut map = Map::new();
        if let Value::Array(rows) = frame {
            for row in rows {
                let index = row_index(row);
                map.insert(sd_core::contracts::python_str(&index), json_safe(row));
            }
        }
        map
    };
    let before = to_map(original);
    let after = to_map(repaired);
    let mut keys: Vec<String> = before.keys().cloned().collect();
    keys.extend(after.keys().cloned());
    keys.sort();
    keys.dedup();
    let mut differences: Vec<Value> = Vec::new();
    for index in keys {
        let left = before
            .get(&index)
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new()));
        let right = after
            .get(&index)
            .cloned()
            .unwrap_or_else(|| Value::Object(Map::new()));
        let mut fields: Vec<String> = left
            .as_object()
            .map(|map| map.keys().cloned().collect())
            .unwrap_or_default();
        fields.extend(
            right
                .as_object()
                .map(|map| map.keys().cloned().collect::<Vec<String>>())
                .unwrap_or_default(),
        );
        fields.sort();
        fields.dedup();
        for field in fields {
            let left_value = left.get(&field).cloned();
            let right_value = right.get(&field).cloned();
            let same_presence = left_value.is_some() == right_value.is_some();
            if left_value != right_value || !same_presence {
                differences.push(serde_json::json!({
                    "index": index,
                    "field": field,
                    "before": left_value.unwrap_or(Value::Null),
                    "after": right_value.unwrap_or(Value::Null),
                }));
            }
        }
    }
    let currency_before = original_metadata
        .get("currency")
        .cloned()
        .unwrap_or(Value::Null);
    let currency_after = repaired_metadata
        .get("currency")
        .cloned()
        .unwrap_or(Value::Null);
    json_safe(&serde_json::json!({
        "changed": !differences.is_empty() || currency_before != currency_after,
        "differences": differences,
        "currency_before": currency_before,
        "currency_after": currency_after,
        "original": json_safe(original),
        "repaired": json_safe(repaired),
    }))
}

/// Normalize per-symbol frames for a batch call.
pub fn normalize_history_batch(
    frames: &Map<String, Value>,
    metadata: &Map<String, Value>,
    price_basis: &str,
    interval: &str,
    source_indices: Option<&Map<String, Value>>,
) -> Result<Value, String> {
    let mut results: Map<String, Value> = Map::new();
    for (symbol, frame) in frames {
        let known: Option<BTreeSet<String>> = source_indices
            .and_then(|indices| indices.get(symbol))
            .and_then(Value::as_array)
            .map(|items| items.iter().map(sd_core::contracts::python_str).collect());
        let mut rows: Vec<Value> = Vec::new();
        let mut excluded: Vec<Value> = Vec::new();
        if let Value::Array(items) = frame {
            for row in items {
                let index = row_index(row);
                let label = sd_core::contracts::python_str(&index);
                if known
                    .as_ref()
                    .map(|set| !set.contains(&label))
                    .unwrap_or(false)
                {
                    excluded.push(Value::String(label));
                    continue;
                }
                let mut cloned = row.as_object().cloned().unwrap_or_default();
                cloned.insert("timestamp".into(), index);
                rows.push(Value::Object(cloned));
            }
        }
        let symbol_metadata = metadata
            .get(symbol)
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));
        let mut result = normalize_history(
            &Value::Array(rows),
            &symbol_metadata,
            price_basis,
            Some(symbol),
            interval,
        )?;
        let valid = result
            .get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .map(|bars| {
                bars.iter()
                    .any(|bar| !bar.get("close").map(Value::is_null).unwrap_or(true))
            })
            .unwrap_or(false);
        if !valid {
            if let Some(object) = result.as_object_mut() {
                object.insert("status".into(), Value::String("empty".into()));
                if let Some(Value::Array(warnings)) = object.get_mut("warnings") {
                    warnings.push(Value::String(
                        "no_valid_prices; empty_reason_unconfirmed".into(),
                    ));
                }
            }
        }
        let bar_count = result
            .get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        if let Some(object) = result.as_object_mut() {
            object.insert(
                "alignment".into(),
                serde_json::json!({
                    "source_index_known": known.is_some(),
                    "excluded_slots": excluded,
                    "null_origin": if known.is_some() { "source" } else { "source_or_batch_alignment_unconfirmed" },
                }),
            );
            object.insert("bar_count".into(), Value::from(bar_count));
        }
        results.insert(symbol.clone(), result);
    }
    let all_success = !results.is_empty()
        && results
            .values()
            .all(|row| row.get("status").and_then(Value::as_str) == Some("success"));
    let has_data = results.values().any(|row| {
        row.get("data")
            .and_then(|data| data.get("bars"))
            .and_then(Value::as_array)
            .map(|bars| {
                bars.iter()
                    .any(|bar| !bar.get("close").map(Value::is_null).unwrap_or(true))
            })
            .unwrap_or(false)
    });
    let status = if all_success {
        "success"
    } else if has_data {
        "partial"
    } else {
        "empty"
    };
    Ok(json_safe(
        &serde_json::json!({"status": status, "symbols": results}),
    ))
}

/// Normalize financial statements (already-indexed rows, `{"columns","rows"}` frames).
pub fn normalize_financials(
    statements: &Map<String, Value>,
    frequency: &str,
    currency: Option<&Value>,
) -> Result<Value, String> {
    if !matches!(
        frequency,
        "annual" | "yearly" | "quarterly" | "ttm" | "trailing"
    ) {
        return Err("Unsupported financial frequency".to_string());
    }
    let mut output: Vec<Value> = Vec::new();
    let mut statuses: Map<String, Value> = Map::new();
    for (statement, frame) in statements {
        if matches!(frequency, "ttm" | "trailing")
            && matches!(
                statement.as_str(),
                "balance_sheet" | "balance" | "balancesheet"
            )
        {
            statuses.insert(statement.clone(), Value::String("unsupported".into()));
            continue;
        }
        let mut count = 0usize;
        if let Some(Value::Object(frame_obj)) = Some(frame) {
            if let Some(Value::Array(rows)) = frame_obj.get("rows") {
                let columns: Vec<String> = frame_obj
                    .get("columns")
                    .and_then(Value::as_array)
                    .map(|items| items.iter().map(sd_core::contracts::python_str).collect())
                    .unwrap_or_default();
                for row in rows {
                    let account = row
                        .get("index")
                        .map(sd_core::contracts::python_str)
                        .unwrap_or_default();
                    let values = row
                        .get("values")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    for (position, column) in columns.iter().enumerate() {
                        let value = values.get(position).cloned().unwrap_or(Value::Null);
                        let end = parse_datetime(&Value::String(column.clone()));
                        let normalized = number(Some(&value)).unwrap_or(Value::Null);
                        output.push(json_safe(&serde_json::json!({
                            "statement": statement,
                            "period_end": end.map(|stamp| stamp.date_naive().format("%Y-%m-%d").to_string()).map(Value::String).unwrap_or(Value::Null),
                            "frequency": frequency,
                            "account": account,
                            "value": normalized,
                            "currency": currency.cloned().unwrap_or(Value::Null),
                            "unit": Value::Null,
                            "published_at": Value::Null,
                            "available_at": Value::Null,
                        })));
                        if !normalized.is_null() {
                            count += 1;
                        }
                    }
                }
            }
        }
        statuses.insert(
            statement.clone(),
            Value::String(if count > 0 { "success" } else { "empty" }.into()),
        );
    }
    let success = statuses
        .values()
        .filter(|status| status.as_str() == Some("success"))
        .count();
    let status = if success > 0 && success == statuses.len() {
        "success"
    } else if success > 0 {
        "partial"
    } else {
        "empty"
    };
    Ok(json_safe(&serde_json::json!({
        "status": status,
        "statements": statuses,
        "records": output,
        "currency": currency.cloned().unwrap_or(Value::Null),
        "warnings": ["period_end_is_not_disclosure_date", "units_not_confirmed"],
    })))
}

/// Normalize an options chain (calls/puts rows). No Greeks or trade direction.
pub fn normalize_options(
    chain: &Value,
    expiration: Option<&Value>,
    underlying: Option<&Value>,
) -> Value {
    let mut output = Map::new();
    for side in ["calls", "puts"] {
        let frame = chain.get(side).cloned().unwrap_or(Value::Null);
        let mut rows: Vec<Value> = Vec::new();
        if let Value::Array(items) = frame {
            for row in items {
                let mut normalized = row.as_object().cloned().unwrap_or_default();
                let last = row.get("lastTradeDate").cloned().unwrap_or(Value::Null);
                normalized.insert(
                    "lastTradeDate".into(),
                    parse_datetime(&last)
                        .map(|stamp| Value::String(iso_utc(stamp)))
                        .unwrap_or(Value::Null),
                );
                rows.push(json_safe(&Value::Object(normalized)));
            }
        }
        output.insert(side.into(), Value::Array(rows));
    }
    let mut result = Map::new();
    result.insert(
        "expiration".into(),
        expiration.cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "underlying".into(),
        underlying.map(json_safe).unwrap_or(Value::Null),
    );
    for (key, value) in output {
        result.insert(key, value);
    }
    result.insert(
        "warnings".into(),
        Value::Array(vec![Value::String(
            "no_greeks_or_trade_direction_inferred".into(),
        )]),
    );
    json_safe(&Value::Object(result))
}

#[allow(dead_code)]
fn _keep_helpers(stamp: DateTime<FixedOffset>) -> String {
    iso_local(stamp.with_timezone(&Utc), None)
}

#[allow(dead_code)]
fn _keep_number(value: Option<&Value>) -> Option<f64> {
    number_f64(value)
}
