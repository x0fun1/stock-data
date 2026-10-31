//! SEC EDGAR adapters (R14): ticker→CIK, filings, XBRL facts, daily index, full-text
//! search and XBRL frames.

use std::collections::BTreeMap;

use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc};
use chrono_tz::America::New_York;
use serde_json::{Map, Value};

use crate::common::positive_int;
use crate::http::HttpTransport;
use crate::official::{normalize_cik, official_get, ProviderEnv};
use crate::transport::{JobContext, ProviderError};

/// Today's calendar date in the US Eastern zone (legacy `_et_today`).
pub fn et_today(now: DateTime<Utc>) -> NaiveDate {
    now.with_timezone(&New_York).date_naive()
}

/// The last `days_back` weekdays in the Eastern calendar, newest first.
pub fn recent_weekdays(now: DateTime<Utc>, days_back: usize) -> Vec<String> {
    let mut cursor = et_today(now);
    let mut out = Vec::with_capacity(days_back);
    while out.len() < days_back {
        if cursor.format("%u").to_string().parse::<u32>().unwrap_or(7) < 6 {
            out.push(cursor.format("%Y%m%d").to_string());
        }
        cursor -= Duration::days(1);
    }
    out
}

/// `_date(value, name, format)`: strict parse plus canonical round-trip.
pub fn validated_date(
    value: Option<&Value>,
    name: &str,
    format: &str,
) -> Result<String, ProviderError> {
    let text = match value {
        Some(Value::String(text)) if !text.trim().is_empty() => text.trim().to_string(),
        _ => {
            return Err(ProviderError::value(format!(
                "{name} must be a nonempty string."
            )))
        }
    };
    let parsed = NaiveDate::parse_from_str(&text, format).map_err(|_| {
        ProviderError::value(format!("{name} has an invalid date format or value."))
    })?;
    if parsed.format(format).to_string() != text {
        return Err(ProviderError::value(format!(
            "{name} has an invalid date format."
        )));
    }
    Ok(text)
}

fn text_of(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

fn array_at<'a>(value: &'a Value, keys: &[&str]) -> &'a [Value] {
    let mut current = value;
    for key in keys {
        current = current.get(*key).unwrap_or(&Value::Null);
    }
    current.as_array().map(Vec::as_slice).unwrap_or(&[])
}

/// `ticker_to_cik`: SEC company tickers mapping (one request per call).
pub fn ticker_to_cik(
    ticker: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let ticker = crate::common::assert_us_ticker(ticker)?;
    let response = official_get(
        "https://www.sec.gov/files/company_tickers.json",
        &[],
        &BTreeMap::new(),
        15.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    let Some(mapping) = document.as_object() else {
        return Ok(Value::Object(Map::new()));
    };
    for entry in mapping.values() {
        if entry.get("ticker").and_then(Value::as_str) == Some(ticker.as_str()) {
            let cik = entry
                .get("cik_str")
                .map(Value::to_string)
                .unwrap_or_default();
            let mut result = Map::new();
            result.insert("ticker".into(), Value::String(ticker.clone()));
            result.insert("cik".into(), Value::String(format!("{cik:0>10}")));
            result.insert(
                "company".into(),
                entry.get("title").cloned().unwrap_or(Value::Null),
            );
            return Ok(Value::Object(result));
        }
    }
    Ok(Value::Object(Map::new()))
}

/// `sec_filings`: recent filings for one CIK.
pub fn sec_filings(
    cik: Option<&Value>,
    form_type: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let cik = normalize_cik(cik)?;
    let response = official_get(
        &format!("https://data.sec.gov/submissions/CIK{cik}.json"),
        &[],
        &BTreeMap::new(),
        15.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    let recent = document
        .get("filings")
        .and_then(|filings| filings.get("recent"))
        .cloned()
        .unwrap_or(Value::Null);
    let forms = array_at(&recent, &["form"]);
    let dates = array_at(&recent, &["filingDate"]);
    let accessions = array_at(&recent, &["accessionNumber"]);
    let primary_docs = array_at(&recent, &["primaryDocument"]);
    let descriptions = array_at(&recent, &["primaryDocDescription"]);
    let form_type = form_type.and_then(Value::as_str);
    let mut filings: Vec<Value> = Vec::new();
    for (index, form) in forms.iter().enumerate() {
        let form_text = text_of(Some(form));
        if let Some(filter) = form_type {
            if form_text != filter {
                continue;
            }
        }
        let accession = accessions
            .get(index)
            .map(|value| text_of(Some(value)))
            .unwrap_or_default();
        let primary = primary_docs
            .get(index)
            .map(|value| text_of(Some(value)))
            .unwrap_or_default();
        let cik_number: u64 = cik.parse().unwrap_or(0);
        let mut item = Map::new();
        item.insert("form".into(), Value::String(form_text));
        item.insert(
            "date".into(),
            dates.get(index).cloned().unwrap_or(Value::Null),
        );
        item.insert("accession_number".into(), Value::String(accession.clone()));
        item.insert("primary_document".into(), Value::String(primary.clone()));
        item.insert(
            "description".into(),
            descriptions
                .get(index)
                .map(|value| text_of(Some(value)))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        let url = if index < primary_docs.len() && !primary.is_empty() {
            format!(
                "https://www.sec.gov/Archives/edgar/data/{cik_number}/{}/{primary}",
                accession.replace('-', "")
            )
        } else {
            String::new()
        };
        item.insert("url".into(), Value::String(url));
        filings.push(Value::Object(item));
        if filings.len() == 50 {
            break;
        }
    }
    let ticker = document
        .get("tickers")
        .and_then(Value::as_array)
        .and_then(|tickers| tickers.first())
        .cloned()
        .unwrap_or_else(|| Value::String(String::new()));
    let mut result = Map::new();
    result.insert(
        "company_name".into(),
        document.get("name").cloned().unwrap_or(Value::Null),
    );
    result.insert("cik".into(), Value::String(cik));
    result.insert("ticker".into(), ticker);
    result.insert("filings".into(), Value::Array(filings));
    Ok(Value::Object(result))
}

/// `sec_xbrl_facts`: GAAP facts (metric inventory or selected metrics).
pub fn sec_xbrl_facts(
    cik: Option<&Value>,
    metrics: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let cik = normalize_cik(cik)?;
    if let Some(value) = metrics {
        let valid = match value.as_array() {
            Some(items) => items
                .iter()
                .all(|item| item.as_str().map(|text| !text.is_empty()).unwrap_or(false)),
            None => false,
        };
        if !valid {
            return Err(ProviderError::value(
                "metrics must be a list of nonempty XBRL names.",
            ));
        }
    }
    let response = official_get(
        &format!("https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"),
        &[],
        &BTreeMap::new(),
        15.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    let gaap = document
        .get("facts")
        .and_then(|facts| facts.get("us-gaap"))
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let company = document.get("entityName").cloned().unwrap_or(Value::Null);
    let Some(metric_map) = gaap.as_object() else {
        return Ok(serde_json::json!({"company": company, "metrics": {}}));
    };
    let requested: Vec<String> = match metrics {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| item.as_str().map(ToString::to_string))
            .collect(),
        _ => Vec::new(),
    };
    if requested.is_empty() {
        let available: Vec<Value> = metric_map
            .iter()
            .map(|(name, metric)| {
                let units = metric
                    .get("units")
                    .and_then(Value::as_object)
                    .map(|units| units.keys().cloned().collect::<Vec<_>>())
                    .unwrap_or_default();
                serde_json::json!({
                    "name": name,
                    "label": metric.get("label").cloned().unwrap_or(Value::String(name.clone())),
                    "units": units,
                })
            })
            .collect();
        return Ok(serde_json::json!({
            "company": company,
            "total_metrics": available.len(),
            "available_metrics": available,
        }));
    }
    let fields = [
        "start", "end", "val", "form", "frame", "accn", "fy", "fp", "filed",
    ];
    let mut result: Map<String, Value> = Map::new();
    for name in requested {
        let mut rows: Vec<Value> = Vec::new();
        if let Some(units) = metric_map.get(&name).and_then(|metric| metric.get("units")) {
            if let Some(unit_map) = units.as_object() {
                for (unit, entries) in unit_map {
                    let Some(entries) = entries.as_array() else {
                        continue;
                    };
                    for entry in entries {
                        let form = entry.get("form").and_then(Value::as_str).unwrap_or("");
                        if form != "10-K" && form != "10-Q" {
                            continue;
                        }
                        let mut row = Map::new();
                        for field in fields {
                            row.insert(
                                field.to_string(),
                                entry.get(field).cloned().unwrap_or(Value::Null),
                            );
                        }
                        row.insert("unit".into(), Value::String(unit.clone()));
                        rows.push(Value::Object(row));
                    }
                }
            }
        }
        rows.sort_by(|left, right| {
            let key = |row: &Value| {
                (
                    text_of(row.get("filed")),
                    text_of(row.get("end")),
                    text_of(row.get("start")),
                    text_of(row.get("accn")),
                )
            };
            key(right).cmp(&key(left))
        });
        result.insert(name, Value::Array(rows));
    }
    Ok(serde_json::json!({"company": company, "metrics": result}))
}

const FORM_LABEL: [(&str, &str); 9] = [
    ("4", "内部人交易"),
    ("8-K", "重大事件"),
    ("13F-HR", "机构持仓"),
    ("144", "限售股拟出售"),
    ("10-K", "年报"),
    ("10-Q", "季报"),
    ("SC 13D", "举牌(主动)"),
    ("SC 13G", "举牌(被动)"),
    ("S-1", "IPO注册"),
];

/// `daily_filings`: EDGAR daily index with weekday fallback.
pub fn daily_filings(
    date: Option<&Value>,
    forms: Option<&Value>,
    now: DateTime<Utc>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let requested_date = match date {
        None | Some(Value::Null) => None,
        Some(value) => Some(validated_date(Some(value), "date", "%Y%m%d")?),
    };
    let form_filter: Option<Vec<String>> = match forms {
        None | Some(Value::Null) => None,
        Some(Value::Array(items)) => {
            let mut result = Vec::with_capacity(items.len());
            for item in items {
                let Some(text) = item.as_str() else {
                    return Err(ProviderError::value("forms must be a list of form names."));
                };
                result.push(text.to_string());
            }
            Some(result)
        }
        Some(_) => return Err(ProviderError::value("forms must be a list of form names.")),
    };
    let candidates = match &requested_date {
        Some(date) => vec![date.clone()],
        None => recent_weekdays(now, 7),
    };
    for day in candidates {
        let parsed = NaiveDate::parse_from_str(&day, "%Y%m%d")
            .map_err(|_| ProviderError::value("date has an invalid date format or value."))?;
        let url = format!(
            "https://www.sec.gov/Archives/edgar/daily-index/{}/QTR{}/form.{day}.idx",
            parsed.year(),
            (parsed.month() - 1) / 3 + 1
        );
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
            Err(error) if error.kind == crate::transport::ProviderErrorKind::DataNotAvailable => {
                continue
            }
            Err(error) => return Err(error),
        };
        let lines: Vec<&str> = raw.lines().collect();
        let start = lines
            .iter()
            .position(|line| line.starts_with("---"))
            .map(|index| index + 1)
            .unwrap_or(11);
        let mut filings: Vec<Value> = Vec::new();
        let mut by_form: Vec<(String, i64)> = Vec::new();
        for line in lines.iter().skip(start) {
            let parts: Vec<&str> = split_columns(line.trim());
            if parts.len() < 5 {
                continue;
            }
            let (form, company, cik, filed, path) =
                (parts[0], parts[1], parts[2], parts[3], parts[4]);
            if form.is_empty() {
                continue;
            }
            match by_form.iter_mut().find(|(name, _)| name == form) {
                Some((_, count)) => *count += 1,
                None => by_form.push((form.to_string(), 1)),
            }
            if let Some(filter) = &form_filter {
                if !filter.iter().any(|item| item == form) {
                    continue;
                }
            }
            let label = FORM_LABEL
                .iter()
                .find(|(name, _)| *name == form)
                .map(|(_, label)| (*label).to_string())
                .unwrap_or_default();
            let url = if path.is_empty() {
                Value::Null
            } else {
                Value::String(format!("https://www.sec.gov/Archives/{path}"))
            };
            filings.push(serde_json::json!({
                "form": form,
                "form_label": label,
                "company": company,
                "cik": cik,
                "date": filed,
                "url": url,
            }));
        }
        if !by_form.is_empty() {
            by_form.sort_by_key(|(_, count)| std::cmp::Reverse(*count));
            let mut by_form_map = Map::new();
            let mut total = 0i64;
            for (form, count) in by_form {
                total += count;
                by_form_map.insert(form, Value::from(count));
            }
            return Ok(serde_json::json!({
                "date": day,
                "total": total,
                "by_form": Value::Object(by_form_map),
                "filings": filings,
            }));
        }
    }
    Err(ProviderError::data_not_available(
        "未找到近 7 个工作日的 EDGAR 每日索引",
    ))
}

/// Split one daily index line on runs of two or more spaces (legacy adaptive parser).
fn split_columns(line: &str) -> Vec<&str> {
    let bytes = line.as_bytes();
    let mut parts: Vec<&str> = Vec::new();
    let mut start = 0usize;
    let mut index = 0usize;
    while index < bytes.len() {
        if bytes[index] == b' ' {
            let run_start = index;
            while index < bytes.len() && bytes[index] == b' ' {
                index += 1;
            }
            if index - run_start >= 2 {
                parts.push(&line[start..run_start]);
                start = index;
            }
        } else {
            index += 1;
        }
    }
    parts.push(&line[start..]);
    parts.into_iter().map(str::trim).collect()
}

/// `fulltext_search`: EDGAR full-text search index.
#[allow(clippy::too_many_arguments)]
pub fn fulltext_search(
    query: Option<&Value>,
    forms: Option<&Value>,
    date_from: Option<&Value>,
    date_to: Option<&Value>,
    limit: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let query = match query {
        Some(Value::String(text)) if !text.trim().is_empty() => text.trim().to_string(),
        _ => return Err(ProviderError::value("query must be a nonempty string.")),
    };
    let limit = match limit {
        None => 20,
        Some(value) => positive_int(Some(value), "limit")?,
    };
    let date_from = match date_from {
        None | Some(Value::Null) => None,
        Some(value) => Some(validated_date(Some(value), "date_from", "%Y-%m-%d")?),
    };
    let date_to = match date_to {
        None | Some(Value::Null) => None,
        Some(value) => Some(validated_date(Some(value), "date_to", "%Y-%m-%d")?),
    };
    if let (Some(from), Some(to)) = (&date_from, &date_to) {
        if from > to {
            return Err(ProviderError::value("date_from must not be after date_to."));
        }
    }
    let forms_text = match forms {
        Some(Value::String(text)) if !text.is_empty() => Some(text.clone()),
        _ => None,
    };
    let mut params: Vec<(&str, String)> = vec![
        ("q", query),
        ("from", "0".to_string()),
        ("size", limit.to_string()),
    ];
    if let Some(forms) = &forms_text {
        params.push(("forms", forms.clone()));
    }
    if let Some(from) = &date_from {
        params.push(("dateRange", "custom".to_string()));
        params.push(("startdt", from.clone()));
    }
    if let Some(to) = &date_to {
        params.push(("dateRange", "custom".to_string()));
        params.push(("enddt", to.clone()));
    }
    let response = official_get(
        "https://efts.sec.gov/LATEST/search-index",
        &params,
        &BTreeMap::new(),
        30.0,
        true,
        env,
        transport,
        context,
    )?;
    let document = response.json.unwrap_or(Value::Null);
    let hits = document
        .get("hits")
        .and_then(|hits| hits.get("hits"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let total = document
        .get("hits")
        .and_then(|hits| hits.get("total"))
        .and_then(|total| total.get("value"))
        .cloned()
        .unwrap_or(Value::from(0));
    let results: Vec<Value> = hits
        .iter()
        .map(|hit| {
            let source = hit.get("_source").cloned().unwrap_or(Value::Null);
            let company = source
                .get("display_names")
                .and_then(Value::as_array)
                .and_then(|names| names.first())
                .cloned()
                .unwrap_or(Value::Null);
            serde_json::json!({
                "form": source.get("root_form").cloned().unwrap_or(Value::Null),
                "company": company,
                "filed": source.get("file_date").cloned().unwrap_or(Value::Null),
                "id": hit.get("_id").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(serde_json::json!({"total": total, "results": results}))
}

const XBRL_TAGS: [(&str, &str); 13] = [
    ("营业收入", "Revenues"),
    (
        "营业收入(合同)",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    ("净利润", "NetIncomeLoss"),
    ("研发费用", "ResearchAndDevelopmentExpense"),
    ("毛利", "GrossProfit"),
    ("经营利润", "OperatingIncomeLoss"),
    ("总资产", "Assets"),
    ("股东权益", "StockholdersEquity"),
    ("现金及等价物", "CashAndCashEquivalentsAtCarryingValue"),
    ("经营现金流", "NetCashProvidedByUsedInOperatingActivities"),
    ("资本开支", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("长期负债", "LongTermDebtNoncurrent"),
    ("稀释EPS", "EarningsPerShareDiluted"),
];

const INSTANT_TAGS: [&str; 4] = [
    "Assets",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebtNoncurrent",
];

fn frame_period(year: i64, quarter: Option<i64>, instant: bool) -> String {
    match (instant, quarter) {
        (true, Some(quarter)) => format!("CY{year}Q{quarter}I"),
        (true, None) => format!("CY{year}Q4I"),
        (false, Some(quarter)) => format!("CY{year}Q{quarter}"),
        (false, None) => format!("CY{year}"),
    }
}

/// `market_frame`: one XBRL frame slice with instant/duration probing.
#[allow(clippy::too_many_arguments)]
pub fn market_frame(
    tag: Option<&Value>,
    year: Option<&Value>,
    quarter: Option<&Value>,
    unit: Option<&Value>,
    instant: Option<&Value>,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<Value, ProviderError> {
    let tag = match tag {
        Some(Value::String(text)) if !text.trim().is_empty() => text.trim().to_string(),
        _ => return Err(ProviderError::value("tag must be a nonempty string.")),
    };
    let year = positive_int(year, "year")?;
    let quarter = match quarter {
        None | Some(Value::Null) => None,
        Some(Value::Number(number)) => {
            let value = number.as_i64().unwrap_or(-1);
            if !(1..=4).contains(&value) {
                return Err(ProviderError::value("quarter must be 1, 2, 3, 4 or null."));
            }
            Some(value)
        }
        Some(_) => return Err(ProviderError::value("quarter must be 1, 2, 3, 4 or null.")),
    };
    let instant = match instant {
        None | Some(Value::Null) => None,
        Some(Value::Bool(flag)) => Some(*flag),
        Some(_) => return Err(ProviderError::value("instant must be true, false or null.")),
    };
    let unit = match unit {
        None => "USD".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(_) => return Err(ProviderError::value("unit must have type str.")),
    };
    let tag = XBRL_TAGS
        .iter()
        .find(|(name, _)| *name == tag)
        .map(|(_, xbrl)| (*xbrl).to_string())
        .unwrap_or(tag);
    let guess = instant.unwrap_or_else(|| INSTANT_TAGS.contains(&tag.as_str()));
    let attempts = if instant.is_some() {
        vec![guess]
    } else {
        vec![guess, !guess]
    };
    let mut last_error: Option<ProviderError> = None;
    for is_instant in attempts {
        let period = frame_period(year, quarter, is_instant);
        let url =
            format!("https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json");
        let response = match official_get(
            &url,
            &[],
            &BTreeMap::new(),
            45.0,
            true,
            env,
            transport,
            context,
        ) {
            Ok(response) => response,
            Err(error) if error.kind == crate::transport::ProviderErrorKind::DataNotAvailable => {
                last_error = Some(error);
                continue;
            }
            Err(error) => return Err(error),
        };
        let document = response.json.unwrap_or(Value::Null);
        let rows: Vec<Value> = document
            .get("data")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|entry| {
                        serde_json::json!({
                            "cik": entry.get("cik").cloned().unwrap_or(Value::Null),
                            "entity": entry.get("entityName").cloned().unwrap_or(Value::Null),
                            "value": entry.get("val").cloned().unwrap_or(Value::Null),
                            "end": entry.get("end").cloned().unwrap_or(Value::Null),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        return Ok(serde_json::json!({
            "tag": tag,
            "period": period,
            "unit": unit,
            "instant": is_instant,
            "count": rows.len(),
            "data": rows,
        }));
    }
    Err(last_error.unwrap_or_else(|| {
        ProviderError::data_not_available("HTTP 404: requested resource is not available.")
    }))
}
