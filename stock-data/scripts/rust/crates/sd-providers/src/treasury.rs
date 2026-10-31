//! US Treasury yield-curve adapter (R14).

use std::collections::BTreeMap;

use chrono::{Datelike, Utc};
use serde_json::{Map, Value};

use crate::common::positive_int;
use crate::http::HttpTransport;
use crate::official::{official_get, ProviderEnv};
use crate::transport::{JobContext, ProviderError};

/// Parse one CSV document into header-ordered row objects (Python `csv.DictReader`).
pub fn csv_rows(text: &str) -> Vec<Value> {
    let mut records: Vec<Vec<String>> = Vec::new();
    let mut fields: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut quoted = false;
    let mut row: Vec<String> = Vec::new();
    let mut characters = text.chars().peekable();
    while let Some(character) = characters.next() {
        if quoted {
            if character == '"' {
                if characters.peek() == Some(&'"') {
                    field.push('"');
                    characters.next();
                } else {
                    quoted = false;
                }
            } else {
                field.push(character);
            }
            continue;
        }
        match character {
            '"' if field.is_empty() => quoted = true,
            ',' => {
                row.push(std::mem::take(&mut field));
            }
            '\r' => {}
            '\n' => {
                row.push(std::mem::take(&mut field));
                records.push(std::mem::take(&mut row));
            }
            other => field.push(other),
        }
    }
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        records.push(row);
    }
    let Some(header) = records.first().cloned() else {
        return Vec::new();
    };
    let mut rows = Vec::new();
    for record in records.iter().skip(1) {
        if record.len() == 1 && record[0].is_empty() {
            continue;
        }
        let mut object = Map::new();
        for (index, name) in header.iter().enumerate() {
            object.insert(
                name.clone(),
                Value::String(record.get(index).cloned().unwrap_or_default()),
            );
        }
        rows.push(Value::Object(object));
    }
    fields.clear();
    rows
}

/// `treasury_yield_curve`: daily Treasury rates for one year (default: current year).
pub fn treasury_yield_curve(
    year: Option<&Value>,
    now: chrono::DateTime<Utc>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let year = match year {
        None | Some(Value::Null) => now.year() as i64,
        Some(value) => positive_int(Some(value), "year")?,
    };
    // The legacy URL already embeds a query string; `requests` appends params with `&`.
    let url = format!(
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
    );
    let response = official_get(
        &url,
        &[],
        &BTreeMap::new(),
        30.0,
        false,
        env,
        transport,
        context,
    )?;
    Ok(Value::Array(csv_rows(&response.text)))
}
