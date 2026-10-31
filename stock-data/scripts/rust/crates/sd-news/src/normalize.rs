//! Normalize timestamped news records and enforce the shared as-of boundary
//! (`news/normalize.py`).
//!
//! Every record is audited at the point-in-time boundary before the valid evidence set
//! is bounded: a record published after the snapshot as-of, an unparsable timestamp, a
//! headline with newlines (which could inject rows into the line-oriented reports) or an
//! undeclared aggregate scale is excluded with a counted reason, never silently repaired.

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use sd_core::contracts::parse_timestamp;
use sd_core::security::{bounded_text, safe_url};

/// Bounded event-score fields.
pub const EVENT_SCORE_FIELDS: [&str; 5] = [
    "event_strength",
    "relevance",
    "magnitude",
    "novelty",
    "source_quality",
];
/// Record types that represent opinion rather than events.
pub const OPINION_TYPES: [&str; 5] = [
    "opinion",
    "analyst_opinion",
    "social",
    "social_post",
    "commentary",
];

/// Python `datetime.isoformat()` with the `+00:00` -> `Z` suffix rewrite.
pub fn isoformat_z(value: DateTime<Utc>) -> String {
    if value.timestamp_subsec_micros() == 0 {
        value.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        // Python always prints six fractional digits when the microsecond is nonzero.
        value.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

fn optional_text(value: Option<&Value>) -> Option<String> {
    let value = value?;
    if value.is_null() {
        return None;
    }
    let normalized = bounded_text(value, 1800);
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

fn python_truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Null) | None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn float_value(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    if value.is_null() || matches!(value, Value::Bool(_)) {
        return None;
    }
    value.as_f64()
}

/// Split the provider payload into article records, counting non-object entries.
pub fn extract_article_records(data: &Value) -> (Vec<Value>, i64, Vec<String>) {
    let records: Vec<Value> = if let Some(records) = data.as_array() {
        records.clone()
    } else if let Some(records) = data.get("articles").and_then(Value::as_array) {
        records.clone()
    } else {
        return (
            Vec::new(),
            0,
            vec!["news.data must be an array or an object with an articles array".into()],
        );
    };
    let kept: Vec<Value> = records
        .iter()
        .filter(|record| record.is_object())
        .cloned()
        .collect();
    let dropped = records.len() as i64 - kept.len() as i64;
    (kept, dropped, Vec::new())
}

/// Preserve a provider aggregate only when its point-in-time is verifiable.
pub fn normalize_source_sentiment(
    data: &Value,
    available_at: Option<&Value>,
    asof_timestamp: &str,
) -> (Option<Value>, Vec<String>) {
    let Some(value) = data.get("source_sentiment") else {
        return (None, Vec::new());
    };
    if !value.is_object() {
        return (None, Vec::new());
    }
    let default_available = available_at.cloned().unwrap_or(Value::Null);
    let available_raw = value
        .get("available_at")
        .filter(|candidate| python_truthy(Some(candidate)))
        .cloned()
        .unwrap_or(default_available);
    let Ok(available) = parse_timestamp(&available_raw) else {
        return (
            None,
            vec![
                "provider sentiment aggregate excluded because no timezone-aware availability timestamp was supplied"
                    .into(),
            ],
        );
    };
    let Ok(asof) = parse_timestamp(&Value::String(asof_timestamp.to_string())) else {
        return (
            None,
            vec![
                "provider sentiment aggregate excluded because no timezone-aware availability timestamp was supplied"
                    .into(),
            ],
        );
    };
    if available > asof {
        return (
            None,
            vec![
                "provider sentiment aggregate excluded because it was fetched or timestamped after the snapshot as-of"
                    .into(),
            ],
        );
    }
    let mut result = Map::new();
    result.insert(
        "source".into(),
        optional_text(value.get("source"))
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    result.insert(
        "period".into(),
        optional_text(value.get("period"))
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    result.insert(
        "sentiment_source".into(),
        optional_text(value.get("sentiment_source"))
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    result.insert("available_at".into(), Value::String(isoformat_z(available)));
    let mut warnings: Vec<String> = Vec::new();
    let percent_scale = value
        .get("percent_scale")
        .cloned()
        .unwrap_or_else(|| Value::String("fraction".into()));
    let scale_name = percent_scale.as_str().unwrap_or("");
    let upper = match scale_name {
        "fraction" => Some(1.0),
        "percent" => Some(100.0),
        _ => None,
    };
    result.insert("percent_scale".into(), percent_scale);
    for field in ["sentiment_score", "bullish_percent", "bearish_percent"] {
        let raw = value.get(field);
        let score = float_value(raw);
        let finite = score.map(|value| value.is_finite()).unwrap_or(false);
        let valid = if field != "sentiment_score" {
            finite
                && upper
                    .map(|upper| (0.0..=upper).contains(&score.unwrap_or(0.0)))
                    .unwrap_or(false)
        } else if value.get("sentiment_score_scale").and_then(Value::as_str)
            == Some("minus_one_to_one")
        {
            finite && (-1.0..=1.0).contains(&score.unwrap_or(0.0))
        } else {
            // No declared scale for the aggregate score: it stays unusable.
            false
        };
        result.insert(
            field.into(),
            if valid {
                Value::from(score.unwrap_or(0.0))
            } else {
                Value::Null
            },
        );
        if raw.is_some() && !valid {
            warnings.push(format!(
                "provider {field} excluded: invalid or unspecified scale/range"
            ));
        }
    }
    let bullish = result.get("bullish_percent").and_then(Value::as_f64);
    let bearish = result.get("bearish_percent").and_then(Value::as_f64);
    if let (Some(upper), Some(bullish), Some(bearish)) = (upper, bullish, bearish) {
        if bullish + bearish > upper + 1e-6 {
            result.insert("bullish_percent".into(), Value::Null);
            result.insert("bearish_percent".into(), Value::Null);
            warnings.push("provider bullish/bearish shares exceed the declared scale".into());
        }
    }
    result.insert(
        "ticker".into(),
        optional_text(value.get("ticker"))
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    (Some(Value::Object(result)), warnings)
}

/// Normalized articles plus the counted exclusions and warnings.
pub struct NormalizedArticles {
    /// Accepted articles in input order.
    pub articles: Vec<Value>,
    /// Exclusion counters (fixed key order).
    pub excluded: Map<String, Value>,
    /// Human-readable warnings.
    pub warnings: Vec<String>,
}

/// Normalize and time-bound the articles against the snapshot as-of.
pub fn normalize_articles(data: &Value, asof_timestamp: &str) -> NormalizedArticles {
    let (records, dropped, mut warnings) = extract_article_records(data);
    let records_len = records.len();
    let cutoff = parse_timestamp(&Value::String(asof_timestamp.to_string()))
        .unwrap_or_else(|_| DateTime::<Utc>::from_timestamp(0, 0).expect("epoch"));
    let mut excluded = Map::new();
    for key in [
        "invalid_record",
        "missing_title",
        "missing_or_invalid_published_at",
        "after_asof",
        "updated_after_asof",
        "over_analysis_cap",
    ] {
        excluded.insert(key.into(), Value::from(0));
    }
    excluded.insert("invalid_record".into(), Value::from(dropped));
    let mut truncated_titles = 0i64;
    let mut truncated_summaries = 0i64;
    // (record, title, published, updated, optional event times)
    type EligibleRecord = (
        Value,
        String,
        DateTime<Utc>,
        Option<DateTime<Utc>>,
        Map<String, Value>,
    );
    let mut eligible: Vec<EligibleRecord> = Vec::new();
    for record in records {
        let raw_title = record.get("title");
        let title = optional_text(raw_title)
            .unwrap_or_default()
            .replace(['\n', '\r'], " ");
        if title.is_empty() {
            bump(&mut excluded, "missing_title");
            continue;
        }
        let Ok(published) = parse_timestamp(record.get("published_at").unwrap_or(&Value::Null))
        else {
            bump(&mut excluded, "missing_or_invalid_published_at");
            continue;
        };
        if published > cutoff {
            bump(&mut excluded, "after_asof");
            continue;
        }
        let mut updated: Option<DateTime<Utc>> = None;
        let updated_at = record.get("updated_at").cloned().unwrap_or(Value::Null);
        let updated_present = match &updated_at {
            Value::Null => false,
            Value::String(text) => !text.is_empty(),
            _ => true,
        };
        if updated_present {
            let Ok(parsed) = parse_timestamp(&updated_at) else {
                bump(&mut excluded, "missing_or_invalid_published_at");
                continue;
            };
            if parsed > cutoff {
                bump(&mut excluded, "updated_after_asof");
                continue;
            }
            updated = Some(parsed);
        }
        let mut times = Map::new();
        let mut invalid_time = false;
        for field in ["occurred_at", "announcement_at", "scheduled_at"] {
            let raw = record.get(field);
            let raw_value = raw.cloned().unwrap_or(Value::Null);
            let present = match &raw_value {
                Value::Null => false,
                Value::String(text) => !text.is_empty(),
                _ => true,
            };
            if !present {
                times.insert(field.into(), Value::Null);
                continue;
            }
            match parse_timestamp(&raw_value) {
                Ok(stamp) => {
                    if field != "scheduled_at" && stamp > cutoff {
                        invalid_time = true;
                        break;
                    }
                    times.insert(field.into(), Value::String(isoformat_z(stamp)));
                }
                Err(_) => {
                    if field == "scheduled_at" {
                        times.insert(field.into(), Value::Null);
                        warnings.push(
                            "scheduled_at could not be parsed and was dropped; the record is retained".into(),
                        );
                        continue;
                    }
                    invalid_time = true;
                }
            }
        }
        if invalid_time {
            bump(&mut excluded, "missing_or_invalid_published_at");
            continue;
        }
        eligible.push((record, title, published, updated, times));
    }
    // Audit every record's PIT boundary before bounding the valid evidence set.
    if eligible.len() > 500 {
        warnings.push("article analysis bounded to 500 most recently published records".into());
        excluded.insert(
            "over_analysis_cap".into(),
            Value::from(eligible.len() as i64 - 500),
        );
        eligible.sort_by_key(|record| std::cmp::Reverse(record.2));
        eligible.truncate(500);
    }
    let mut normalized: Vec<Value> = Vec::new();
    for (record, title, published, updated, times) in eligible {
        let raw_title = record
            .get("title")
            .map(sd_core::contracts::py_str)
            .unwrap_or_default();
        if raw_title.trim().chars().count() > 400 {
            // The legacy length check counts characters, not bytes.
            truncated_titles += 1;
        }
        let mut scores = Map::new();
        for field in EVENT_SCORE_FIELDS {
            let score = float_value(record.get(field));
            let valid = score
                .map(|value| value.is_finite() && (0.0..=1.0).contains(&value))
                .unwrap_or(false);
            scores.insert(
                field.into(),
                if valid {
                    Value::from(score.unwrap_or(0.0))
                } else {
                    Value::Null
                },
            );
        }
        let sentiment = float_value(record.get("sentiment_score"));
        let sentiment_valid = sentiment
            .map(|value| value.is_finite() && (-1.0..=1.0).contains(&value))
            .unwrap_or(false);
        scores.insert(
            "sentiment_score".into(),
            if sentiment_valid {
                Value::from(sentiment.unwrap_or(0.0))
            } else {
                Value::Null
            },
        );
        let mut record_type = optional_text(record.get("record_type"))
            .unwrap_or_else(|| "unknown".into())
            .to_lowercase();
        if !OPINION_TYPES.contains(&record_type.as_str())
            && !["event", "report", "news", "unknown"].contains(&record_type.as_str())
        {
            warnings.push(format!(
                "ignored unsupported record_type '{}'; treated as unknown",
                record_type
            ));
            record_type = "unknown".into();
        }
        let tickers_raw = record
            .get("tickers")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut tickers: Vec<String> = tickers_raw
            .iter()
            .take(20)
            .filter_map(|item| optional_text(Some(item)))
            .map(|text| text.to_uppercase())
            .collect();
        tickers.sort();
        tickers.dedup();
        tickers.truncate(10);
        let raw_summary = record.get("summary");
        if let Some(summary) = raw_summary {
            let text = sd_core::contracts::py_str(summary);
            if text.trim().chars().count() > 1800 {
                truncated_summaries += 1;
            }
        }
        let mut article = Map::new();
        article.insert(
            "article_id".into(),
            optional_text(record.get("article_id"))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        article.insert(
            "url".into(),
            safe_url(record.get("url").unwrap_or(&Value::Null))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        article.insert(
            "title".into(),
            Value::String(title.chars().take(400).collect()),
        );
        article.insert(
            "summary".into(),
            optional_text(raw_summary)
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        article.insert("published_at".into(), Value::String(isoformat_z(published)));
        article.insert(
            "updated_at".into(),
            updated
                .map(|value| Value::String(isoformat_z(value)))
                .unwrap_or(Value::Null),
        );
        for (key, value) in &times {
            article.insert(key.clone(), value.clone());
        }
        article.insert(
            "source".into(),
            Value::String(optional_text(record.get("source")).unwrap_or_else(|| "unknown".into())),
        );
        article.insert(
            "source_type".into(),
            Value::String(
                optional_text(record.get("source_type"))
                    .unwrap_or_else(|| "news".into())
                    .to_lowercase(),
            ),
        );
        article.insert("record_type".into(), Value::String(record_type));
        article.insert(
            "tickers".into(),
            Value::Array(tickers.into_iter().map(Value::String).collect()),
        );
        let hint = optional_text(
            record
                .get("event_type")
                .or_else(|| record.get("event_type_hint")),
        )
        .map(|value| value.to_uppercase())
        .unwrap_or_default();
        article.insert(
            "event_type_hint".into(),
            if hint.is_empty() {
                Value::Null
            } else {
                Value::String(hint)
            },
        );
        article.insert(
            "canonical_event_id".into(),
            optional_text(record.get("canonical_event_id"))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        article.insert(
            "original_reporting_id".into(),
            optional_text(record.get("original_reporting_id"))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        let event_group = record.get("event_group").and_then(Value::as_str);
        article.insert(
            "event_group".into(),
            match event_group {
                Some("company" | "industry" | "macro") => {
                    Value::String(event_group.unwrap_or("").into())
                }
                _ => Value::Null,
            },
        );
        let exposure: Vec<String> = record
            .get("exposure_tickers")
            .and_then(Value::as_array)
            .map(|items| {
                let mut values: Vec<String> = items
                    .iter()
                    .take(20)
                    .filter_map(|item| optional_text(Some(item)))
                    .map(|text| text.to_uppercase())
                    .collect();
                values.sort();
                values.dedup();
                values.truncate(10);
                values
            })
            .unwrap_or_default();
        article.insert(
            "exposure_tickers".into(),
            Value::Array(exposure.into_iter().map(Value::String).collect()),
        );
        article.insert(
            "exposure_basis".into(),
            optional_text(record.get("exposure_basis"))
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        for (key, value) in &scores {
            article.insert(key.clone(), value.clone());
        }
        normalized.push(Value::Object(article));
    }
    let mut summary_parts: Vec<String> = Vec::new();
    for (key, value) in &excluded {
        if let Some(count) = value.as_i64() {
            if count != 0 {
                summary_parts.push(format!("{key}={count}"));
            }
        }
    }
    if !summary_parts.is_empty() {
        warnings.push(format!(
            "excluded news records at the point-in-time boundary: {}",
            summary_parts.join(", ")
        ));
    }
    if truncated_titles != 0 {
        warnings.push(format!(
            "{truncated_titles} news headline(s) were truncated to 400 characters"
        ));
    }
    if truncated_summaries != 0 {
        warnings.push(format!(
            "{truncated_summaries} news summary/summaries were truncated to 1,800 characters"
        ));
    }
    if normalized.is_empty() && records_len != 0 {
        warnings.push("no news records remained after timestamp and schema validation".into());
    }
    NormalizedArticles {
        articles: normalized,
        excluded,
        warnings,
    }
}

fn bump(excluded: &mut Map<String, Value>, key: &str) {
    let current = excluded.get(key).and_then(Value::as_i64).unwrap_or(0);
    excluded.insert(key.to_string(), Value::from(current + 1));
}

/// Whether an article is an opinion record rather than an event report.
pub fn is_opinion_record(article: &Value) -> bool {
    let source_type = article
        .get("source_type")
        .map(sd_core::contracts::py_str)
        .unwrap_or_default()
        .to_lowercase();
    let record_type = article
        .get("record_type")
        .and_then(Value::as_str)
        .unwrap_or("");
    OPINION_TYPES.contains(&record_type)
        || [
            "social",
            "social_media",
            "stocktwits",
            "reddit",
            "opinion",
            "analyst_opinion",
        ]
        .contains(&source_type.as_str())
}

/// Role of a record (`opinion`, `event`, `unknown`).
pub fn record_role(article: &Value) -> &'static str {
    if is_opinion_record(article) {
        return "opinion";
    }
    match article.get("record_type").and_then(Value::as_str) {
        Some("event" | "report" | "news") => "event",
        _ => "unknown",
    }
}
