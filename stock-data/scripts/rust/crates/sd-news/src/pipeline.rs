//! News pipeline orchestration (`news/pipeline.py`).
//!
//! This is the I/O-free mirror of the legacy pipeline: `sd-news` never reads files or
//! calls providers, so [`prepare_news_input`] takes the already-loaded manifest, market
//! and news-domain values (the orchestrator owns file access), and the freeze helpers are
//! exposed as value-level digest/receipt functions.
//!
//! Hard boundaries preserved from the legacy module:
//!
//! * the restricted [`NewsInput`] has no Quant result, probability or eligibility path;
//! * the three states are distinct — `partial` is still *assessed*, only
//!   `not_assessed` means no evidence was produced, and only evidence-affecting warnings
//!   move an assessed run to `partial` (presentation warnings do not);
//! * the host AI lean artifact is validated against this run's own records and attached
//!   as display evidence only: it never enters direction, confidence, event scoring,
//!   sentiment or any probability;
//! * event and opinion cards are built from frozen OHLCV only, and every non-directional
//!   disclosure says `direction_neutral` / `probability_effect=none`.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Map, Value};

use sd_core::contracts::parse_timestamp;
use sd_core::float_sum::math_fsum;
use sd_core::json_text::canonical_text;
use sd_core::security::{bounded_text, provider_failed};

use crate::ai_assessment::{
    attach_lean_to_records, build_ai_lean_block, lean_candidates, validate_ai_assessment,
    AI_LEAN_LAYER,
};
use crate::classify::{
    direction_for, event_text, event_type_for, matched_topics, opinion_score, COVERAGE_NOTE,
    LEXICON_VERSION, SUPPORTED_LANGUAGES,
};
use crate::contracts::NewsInput;
use crate::decay::{time_decay, NEWS_MAX_AGE_CALENDAR_DAYS};
use crate::deduplicate::deduplicate_articles;
use crate::impact::{assess as assess_impact, SOURCE_TIER_RULE_VERSION};
use crate::normalize::{normalize_articles, normalize_source_sentiment};
use crate::reaction::validate_reaction;
use crate::timing::{aggregate_priced_in, event_time_bucket, priced_in_assessment};

/// Latest result schema version.
pub const NEWS_SCHEMA_VERSION: &str = "1.3";
/// Maximum lean candidates the host artifact may carry.
const LEAN_CANDIDATE_LIMIT: usize = 500;

// --------------------------------------------------------------------------- warnings

/// Warnings that shrank or degraded the evidence set.
pub const EVIDENCE_AFFECTING: &str = "evidence_affecting";
/// Warnings that only describe how a retained record was rendered.
pub const PRESENTATION: &str = "presentation";

const PRESENTATION_MARKERS: [&str; 4] = [
    "truncated to 400 characters",
    "truncated to 1,800 characters",
    "could not be parsed and was dropped; the record is retained",
    "treated as unknown",
];

/// Severity of one warning. Unclassified warnings fail conservative (evidence-affecting).
pub fn warning_severity(message: &str) -> &'static str {
    if PRESENTATION_MARKERS
        .iter()
        .any(|marker| message.contains(marker))
    {
        PRESENTATION
    } else {
        EVIDENCE_AFFECTING
    }
}

/// `[{"message", "severity"}]` rows for the warning list.
pub fn warning_details(warnings: &[String]) -> Value {
    Value::Array(
        warnings
            .iter()
            .map(|message| json!({"message": message, "severity": warning_severity(message)}))
            .collect(),
    )
}

/// Counts by severity, in the legacy key order.
pub fn warning_severity_counts(warnings: &[String]) -> Map<String, Value> {
    let mut counts = Map::new();
    counts.insert(EVIDENCE_AFFECTING.into(), Value::from(0));
    counts.insert(PRESENTATION.into(), Value::from(0));
    for message in warnings {
        let severity = warning_severity(message);
        let current = counts.get(severity).and_then(Value::as_i64).unwrap_or(0);
        counts.insert(severity.into(), Value::from(current + 1));
    }
    counts
}

// --------------------------------------------------------------------------- small helpers

fn py_str(value: Option<&Value>) -> String {
    sd_core::contracts::py_str(value.unwrap_or(&Value::Null))
}

/// Python truthiness for the JSON values the pipeline inspects.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Null) | None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn python_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    }
}

/// Mean with CPython `sum()` semantics (compensated float summation).
fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    Some(math_fsum(values.iter().copied()) / values.len() as f64)
}

/// Python `round(value, 6)` shape for the 6-decimal fields written by this module.
fn rounded(value: Option<f64>) -> Option<f64> {
    value.filter(|value| value.is_finite()).map(|value| {
        let factor = 1_000_000.0f64;
        (value * factor).round() / factor
    })
}

fn python_round(value: f64, digits: u32) -> f64 {
    let factor = 10f64.powi(digits as i32);
    (value * factor).round() / factor
}

fn rounded_value(value: Option<f64>) -> Value {
    rounded(value).map(Value::from).unwrap_or(Value::Null)
}

fn mean_field(articles: &[Value], name: &str) -> Option<f64> {
    let values: Vec<f64> = articles
        .iter()
        .filter_map(|row| python_float(row.get(name)))
        .collect();
    if values.is_empty() {
        None
    } else {
        rounded(mean(&values))
    }
}

fn label(score: Option<f64>) -> String {
    let Some(score) = score else {
        return "unknown".into();
    };
    if score >= 0.35 {
        "moderately_bullish".into()
    } else if score > 0.15 {
        "slightly_bullish".into()
    } else if score <= -0.35 {
        "moderately_bearish".into()
    } else if score < -0.15 {
        "slightly_bearish".into()
    } else {
        "neutral".into()
    }
}

fn direction_aggregate(values: &[String]) -> String {
    if values.iter().any(|value| value == "mixed") {
        return "mixed".into();
    }
    let positive = values.iter().any(|value| value == "positive");
    let negative = values.iter().any(|value| value == "negative");
    if positive && negative {
        return "mixed".into();
    }
    if positive {
        return "positive".into();
    }
    if negative {
        return "negative".into();
    }
    if !values.is_empty() && values.iter().all(|value| value == "neutral") {
        "neutral".into()
    } else {
        "unknown".into()
    }
}

fn source_agreement(opinions: &[Value]) -> (String, String) {
    let mut by_source: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for opinion in opinions {
        let Some(score) = python_float(opinion.get("sentiment_score")) else {
            continue;
        };
        if let Some(sources) = opinion.get("sources").and_then(Value::as_array) {
            for source in sources {
                by_source
                    .entry(py_str(Some(source)))
                    .or_default()
                    .push(score);
            }
        }
    }
    let mut labels: BTreeSet<String> = BTreeSet::new();
    for scores in by_source.values() {
        if scores.is_empty() {
            continue;
        }
        let center = mean(scores).unwrap_or(0.0);
        labels.insert(
            if center > 0.15 {
                "positive"
            } else if center < -0.15 {
                "negative"
            } else {
                "neutral"
            }
            .into(),
        );
    }
    let directional: BTreeSet<String> = labels
        .iter()
        .filter(|label| *label != "neutral" && *label != "unknown")
        .cloned()
        .collect();
    let divergence = if directional.len() > 1 {
        "high"
    } else if by_source.len() >= 3 {
        "low"
    } else if by_source.len() >= 2 {
        "medium"
    } else {
        "unknown"
    };
    let agreement = if labels.is_empty() {
        "unknown"
    } else if directional.len() > 1 {
        "low"
    } else if by_source.len() >= 2 {
        if divergence == "low" {
            "high"
        } else {
            "medium"
        }
    } else {
        "single_source"
    };
    (agreement.to_string(), divergence.to_string())
}

fn event_source_agreement(events: &[Value]) -> Value {
    let values: Vec<String> = events
        .iter()
        .map(
            |event| match event.get("source_agreement").and_then(Value::as_str) {
                Some(value) if !value.is_empty() => value.to_string(),
                _ => "unknown".into(),
            },
        )
        .collect();
    let state = if values.iter().any(|value| value == "low") {
        "low"
    } else if values.iter().any(|value| value == "high") {
        "high"
    } else if values.iter().any(|value| value == "single_source") {
        "single_source"
    } else {
        "unknown"
    };
    let mut map = Map::new();
    map.insert("state".into(), Value::String(state.into()));
    map.insert(
        "basis".into(),
        Value::String("weakest_link_across_eligible_events".into()),
    );
    map.insert("event_count".into(), Value::from(values.len()));
    map.insert(
        "per_event".into(),
        Value::Array(values.iter().take(20).cloned().map(Value::String).collect()),
    );
    Value::Object(map)
}

/// Event types whose release window carries a documented variance elevation.
pub const VOLATILITY_CATALYST_TYPES: [&str; 6] = [
    "EARNINGS",
    "GUIDANCE",
    "MACRO",
    "MERGER_ACQUISITION",
    "REGULATORY",
    "CAPITAL_RAISE",
];

fn volatility_regime(events: &[Value], horizon: i64) -> Value {
    let recent: Vec<&Value> = events
        .iter()
        .filter(|event| {
            event
                .get("age_sessions")
                .and_then(Value::as_i64)
                .map(|age| (0..=horizon).contains(&age))
                .unwrap_or(false)
        })
        .collect();
    let catalysts: Vec<&Value> = recent
        .into_iter()
        .filter(|event| {
            VOLATILITY_CATALYST_TYPES.contains(&py_str(event.get("event_type")).as_str())
        })
        .collect();
    let event_ids: Vec<Value> = catalysts
        .iter()
        .take(10)
        .map(|event| event.get("event_id").cloned().unwrap_or(Value::Null))
        .collect();
    let event_types: BTreeSet<String> = catalysts
        .iter()
        .map(|event| py_str(event.get("event_type")))
        .collect();
    let mut map = Map::new();
    map.insert(
        "regime".into(),
        Value::String(
            if catalysts.is_empty() {
                "normal"
            } else {
                "elevated"
            }
            .into(),
        ),
    );
    map.insert(
        "basis".into(),
        Value::String("declared_event_type_rule_within_the_forecast_window".into()),
    );
    map.insert("lookback_sessions".into(), Value::from(horizon));
    map.insert("event_ids".into(), Value::Array(event_ids));
    map.insert(
        "event_types".into(),
        Value::Array(event_types.into_iter().map(Value::String).collect()),
    );
    map.insert("direction_neutral".into(), Value::Bool(true));
    map.insert("probability_effect".into(), Value::String("none".into()));
    map.insert(
        "calibration_status".into(),
        Value::String("heuristic_uncalibrated".into()),
    );
    map.insert(
        "evidence_note".into(),
        Value::String(
            "measured on 318 real earnings releases, event windows showed ~1.36x the non-event 5-day dispersion; direction was not predictable"
                .into(),
        ),
    );
    Value::Object(map)
}

/// Match a ticker only with ASCII alphanumeric boundaries (Python lookarounds).
fn text_contains_ticker(text: &str, ticker: &str) -> bool {
    if ticker.is_empty() {
        return false;
    }
    let bytes = text.as_bytes();
    let needle = ticker.as_bytes();
    if needle.len() > bytes.len() {
        return false;
    }
    let mut start = 0usize;
    while let Some(found) = text[start..].find(ticker) {
        let position = start + found;
        let before_ok = position == 0 || !bytes[position - 1].is_ascii_alphanumeric();
        let after = position + needle.len();
        let after_ok = after >= bytes.len() || !bytes[after].is_ascii_alphanumeric();
        if before_ok && after_ok {
            return true;
        }
        start = position + 1;
        if start > text.len() {
            break;
        }
    }
    false
}

fn relevance(rows: &[Value], ticker: &str) -> &'static str {
    let mut tickers: BTreeSet<String> = BTreeSet::new();
    for row in rows {
        if let Some(items) = row.get("tickers").and_then(Value::as_array) {
            for item in items {
                tickers.insert(py_str(Some(item)));
            }
        }
    }
    if tickers.contains(ticker) {
        return "tagged_target";
    }
    for row in rows {
        let exposure = row
            .get("exposure_tickers")
            .and_then(Value::as_array)
            .map(|items| items.iter().any(|item| py_str(Some(item)) == ticker))
            .unwrap_or(false);
        if exposure && truthy(row.get("exposure_basis")) {
            return "documented_exposure";
        }
    }
    if !tickers.is_empty() {
        return "other_instrument";
    }
    let matched = rows.iter().any(|row| {
        let text = format!(
            "{} {}",
            py_str(row.get("title")),
            py_str(row.get("summary"))
        );
        text_contains_ticker(&text, ticker)
    });
    if matched {
        "text_match"
    } else {
        "not_explicitly_matched"
    }
}

fn eligibility(
    cluster: &Value,
    news_input: &NewsInput,
    decay: &Map<String, Value>,
    policy: &Value,
) -> (bool, Vec<String>) {
    let rows = cluster
        .get("articles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut reasons: Vec<String> = Vec::new();
    if !["tagged_target", "text_match", "documented_exposure"]
        .contains(&relevance(&rows, &news_input.ticker))
    {
        reasons.push("target relevance is unverified or belongs to another instrument".into());
    }
    let traceable = rows.iter().any(|row| {
        let source = py_str(row.get("source")).to_lowercase();
        !["unknown", "", "none"].contains(&source.as_str())
    });
    if !traceable {
        reasons.push("news source is untraceable".into());
    }
    if rows.iter().any(|row| {
        row.get("source_quality").and_then(Value::as_f64) == Some(0.0)
            || row.get("relevance").and_then(Value::as_f64) == Some(0.0)
    }) {
        reasons.push("source supplied zero quality/relevance".into());
    }
    let representative = cluster
        .get("representative")
        .cloned()
        .unwrap_or(Value::Null);
    let available = representative
        .get("occurred_at")
        .filter(|value| truthy(Some(value)))
        .or_else(|| {
            representative
                .get("announcement_at")
                .filter(|value| truthy(Some(value)))
        })
        .cloned()
        .unwrap_or_else(|| {
            cluster
                .get("first_published_at")
                .cloned()
                .unwrap_or(Value::Null)
        });
    let asof = parse_timestamp(&Value::String(news_input.asof_timestamp.clone())).ok();
    let available_stamp = parse_timestamp(&available).ok();
    if let (Some(asof), Some(available_stamp)) = (asof, available_stamp) {
        let age = (asof - available_stamp).num_milliseconds() as f64 / 1000.0 / 86400.0;
        let limit = policy
            .get("max_age_calendar_days")
            .and_then(Value::as_i64)
            .unwrap_or(NEWS_MAX_AGE_CALENDAR_DAYS);
        if age > limit as f64 {
            reasons.push("event/opinion is outside the current evidence window".into());
        }
    }
    let decay_value = decay
        .get("time_decay")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let minimum = policy
        .get("minimum_time_decay")
        .and_then(Value::as_f64)
        .unwrap_or(0.0625);
    if decay_value < minimum {
        reasons.push("event/opinion decay is below the evidence threshold".into());
    }
    if ["failed", "invalid", "unavailable", "empty"].contains(&news_input.news_status.as_str()) {
        reasons.push("news domain is unavailable or failed".into());
    }
    (reasons.is_empty(), reasons)
}

// --------------------------------------------------------------------------- input preparation

fn manifest_field<'a>(manifest: &'a Value, field: &str) -> Result<&'a Value, String> {
    manifest
        .get(field)
        .ok_or_else(|| format!("snapshot manifest is missing {field}"))
}

/// Read only normalized news, snapshot identity and frozen market bars.
///
/// The legacy version opens `snapshot_dir/news.json` and `load_snapshot`; this I/O-free
/// mirror receives the already-loaded values instead, which keeps the News layer free of
/// filesystem and provider access.
pub fn prepare_news_input(
    manifest: &Value,
    market: &Value,
    news: &Value,
) -> Result<NewsInput, String> {
    let ticker = py_str(Some(manifest_field(manifest, "ticker")?));
    let market_code = py_str(Some(manifest_field(manifest, "market")?));
    let horizon = py_str(Some(manifest_field(manifest, "horizon")?));
    let asof_timestamp = py_str(Some(manifest_field(manifest, "asof_timestamp")?));
    let snapshot_id = py_str(Some(manifest_field(manifest, "snapshot_id")?));
    let news_object = news.as_object().cloned().unwrap_or_default();
    let failed_payload = provider_failed(news);
    let news_status_raw = news_object
        .get("status")
        .map(|value| py_str(Some(value)))
        .unwrap_or_else(|| "unavailable".into());
    let data_available = !failed_payload
        && !["unavailable", "failed", "invalid", "empty"].contains(&news_status_raw.as_str());
    let data = if data_available {
        news_object.get("data").cloned().unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let normalized = normalize_articles(&data, &asof_timestamp);
    let mut warnings = normalized.warnings.clone();
    let available_at = news_object
        .get("source_timestamp")
        .filter(|value| truthy(Some(value)))
        .or_else(|| {
            news_object
                .get("fetched_at_utc")
                .filter(|value| truthy(Some(value)))
        })
        .or_else(|| {
            news_object
                .get("fetched_at")
                .filter(|value| truthy(Some(value)))
        })
        .cloned();
    let (source_sentiment, sentiment_warnings) =
        normalize_source_sentiment(&data, available_at.as_ref(), &asof_timestamp);
    warnings.extend(sentiment_warnings);
    if ["unavailable", "failed", "invalid"].contains(&news_status_raw.as_str()) {
        warnings.push(format!("news domain status is {news_status_raw}"));
    }
    let excluded_count = |key: &str| {
        normalized
            .excluded
            .get(key)
            .and_then(Value::as_i64)
            .unwrap_or(0)
    };
    if excluded_count("missing_or_invalid_published_at") != 0
        || excluded_count("after_asof") != 0
        || excluded_count("updated_after_asof") != 0
    {
        warnings.push(
            "news records without a reliable point-in-time publication/update timestamp were excluded"
                .into(),
        );
    }
    if excluded_count("invalid_record") != 0 {
        warnings.push(format!(
            "{} news record(s) were not objects and were dropped before normalization",
            excluded_count("invalid_record")
        ));
    }
    if excluded_count("over_analysis_cap") != 0 {
        warnings.push(format!(
            "{} PIT-eligible news record(s) were dropped by the 500-record analysis bound",
            excluded_count("over_analysis_cap")
        ));
    }
    let news_status = if failed_payload {
        "failed".to_string()
    } else {
        match news_object.get("status") {
            Some(value) => py_str(Some(value)),
            None => "unavailable".into(),
        }
    };
    let bars = market
        .get("data")
        .and_then(|data| data.get("bars"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut session_closes = Map::new();
    if let Some(sessions) = market
        .get("session_calendar")
        .and_then(|calendar| calendar.get("sessions"))
        .and_then(Value::as_array)
    {
        for row in sessions {
            session_closes.insert(
                py_str(row.get("date")),
                row.get("close_at").cloned().unwrap_or(Value::Null),
            );
        }
    }
    let mut coverage = Map::new();
    coverage.insert(
        "actual_source".into(),
        news_object
            .get("actual_source")
            .cloned()
            .unwrap_or(Value::Null),
    );
    coverage.insert(
        "source_timestamp".into(),
        news_object
            .get("source_timestamp")
            .cloned()
            .unwrap_or(Value::Null),
    );
    coverage.insert(
        "retrieved_window".into(),
        if data.is_object() {
            data.get("history_window").cloned().unwrap_or(Value::Null)
        } else {
            Value::Null
        },
    );
    coverage.insert(
        "excluded_at_normalization".into(),
        Value::Object(normalized.excluded.clone()),
    );
    coverage.insert(
        "calendar_verification".into(),
        manifest
            .get("data_validation")
            .and_then(|validation| validation.get("calendar_verification"))
            .cloned()
            .unwrap_or_else(|| Value::String("unverified".into())),
    );
    coverage.insert("session_closes".into(), Value::Object(session_closes));
    Ok(NewsInput {
        ticker,
        market: market_code,
        horizon,
        asof_timestamp,
        snapshot_id,
        news_status,
        source_sentiment,
        articles: normalized.articles,
        bars,
        warnings,
        coverage,
    })
}

/// Canonical SHA-256 of a JSON value (`jsonio.canonical_bytes`).
pub fn result_sha256(result: &Value) -> String {
    sha256_hex(canonical_text(result).as_bytes())
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Stable digest of the exact News input a candidate export was built from.
pub fn news_input_digest(news_input: &NewsInput) -> String {
    let mut payload = Map::new();
    payload.insert("ticker".into(), Value::String(news_input.ticker.clone()));
    payload.insert("market".into(), Value::String(news_input.market.clone()));
    payload.insert("horizon".into(), Value::String(news_input.horizon.clone()));
    payload.insert(
        "asof_timestamp".into(),
        Value::String(news_input.asof_timestamp.clone()),
    );
    payload.insert(
        "snapshot_id".into(),
        Value::String(news_input.snapshot_id.clone()),
    );
    let mut articles: Vec<Value> = Vec::new();
    for row in &news_input.articles {
        if !row.is_object() {
            continue;
        }
        let mut article = Map::new();
        let identifier = row
            .get("id")
            .filter(|value| truthy(Some(value)))
            .or_else(|| row.get("url").filter(|value| truthy(Some(value))))
            .or_else(|| row.get("title").filter(|value| truthy(Some(value))))
            .cloned()
            .unwrap_or(Value::Null);
        article.insert("id".into(), identifier);
        article.insert(
            "published_at".into(),
            row.get("published_at").cloned().unwrap_or(Value::Null),
        );
        article.insert(
            "title".into(),
            row.get("title").cloned().unwrap_or(Value::Null),
        );
        article.insert("url".into(), row.get("url").cloned().unwrap_or(Value::Null));
        articles.push(Value::Object(article));
    }
    payload.insert("articles".into(), Value::Array(articles));
    let bars: Vec<Value> = news_input
        .bars
        .iter()
        .filter(|row| row.is_object())
        .map(|row| {
            json!({
                "date": row.get("date").cloned().unwrap_or(Value::Null),
                "close": row.get("close").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    payload.insert("bars".into(), Value::Array(bars));
    sha256_hex(canonical_text(&Value::Object(payload)).as_bytes())
}

// --------------------------------------------------------------------------- cards

struct CardContext<'a> {
    news_input: &'a NewsInput,
    policy: &'a Value,
    bars: &'a [Value],
    closes: &'a [f64],
    dates: &'a [String],
}

fn build_event_card(cluster: &Value, context: &CardContext<'_>) -> Result<Value, String> {
    let news_input = context.news_input;
    let representative = cluster
        .get("representative")
        .cloned()
        .unwrap_or(Value::Null);
    let text = event_text(cluster);
    let event_type = event_type_for(
        &text,
        representative
            .get("event_type_hint")
            .and_then(Value::as_str),
    );
    let event_direction = direction_for(&text);
    let decay_time = representative
        .get("occurred_at")
        .filter(|value| truthy(Some(value)))
        .or_else(|| {
            representative
                .get("announcement_at")
                .filter(|value| truthy(Some(value)))
        })
        .cloned()
        .unwrap_or_else(|| {
            cluster
                .get("first_published_at")
                .cloned()
                .unwrap_or(Value::Null)
        });
    let decay = time_decay(
        &py_str(Some(&decay_time)),
        context.bars,
        &event_type,
        &news_input.horizon,
        context.policy,
        &news_input.market,
    )?;
    let (eligible, exclusion_reasons) = eligibility(cluster, news_input, &decay, context.policy);
    let session_closes = news_input
        .coverage
        .get("session_closes")
        .and_then(Value::as_object);
    let reaction = validate_reaction(
        &py_str(cluster.get("first_published_at")),
        event_direction,
        &news_input.market,
        context.bars,
        session_closes,
    );
    let session_index = reaction
        .get("market_session_date")
        .and_then(Value::as_str)
        .and_then(|day| {
            let day = day.get(..10).unwrap_or(day);
            context.dates.iter().position(|candidate| candidate == day)
        })
        .map(|index| index as i64);
    let publishers: Vec<String> = cluster
        .get("sources")
        .and_then(Value::as_array)
        .map(|items| items.iter().map(|item| py_str(Some(item))).collect())
        .unwrap_or_default();
    let mut impact = assess_impact(
        Some(&event_type),
        &reaction,
        &publishers,
        context.closes,
        session_index,
    );
    let bucket = event_time_bucket(
        Some(&decay_time),
        reaction.get("market_session_date"),
        session_closes,
    );
    if let Some(map) = impact.as_object_mut() {
        map.insert("event_time_bucket".into(), bucket);
    }
    let priced_in = priced_in_assessment(
        &reaction,
        context.closes,
        session_index,
        event_direction,
        news_input.horizon_sessions() as usize,
        1.0,
    );
    let articles = cluster
        .get("articles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let score_fields = [
        "event_strength",
        "relevance",
        "magnitude",
        "novelty",
        "source_quality",
    ];
    let mut scores = Map::new();
    for field in score_fields {
        scores.insert(field.into(), rounded_value(mean_field(&articles, field)));
    }
    let score_inputs = ["event_strength", "relevance", "novelty", "source_quality"]
        .iter()
        .map(|field| scores.get(*field).and_then(Value::as_f64))
        .collect::<Vec<_>>();
    let effective_score = if score_inputs.iter().all(Option::is_some) {
        let product: f64 = score_inputs
            .iter()
            .map(|value| value.unwrap_or(0.0))
            .product();
        Some(
            product
                * decay
                    .get("time_decay")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
        )
    } else {
        None
    };
    let mut topics: BTreeSet<String> = BTreeSet::new();
    for row in &articles {
        for term in matched_topics(&format!(
            "{} {}",
            py_str(row.get("title")),
            py_str(row.get("summary"))
        )) {
            topics.insert(term);
        }
    }
    let source_directions: Vec<&'static str> = articles
        .iter()
        .map(|row| {
            direction_for(&format!(
                "{} {}",
                py_str(row.get("title")),
                py_str(row.get("summary"))
            ))
        })
        .collect();
    let positive_count = source_directions
        .iter()
        .filter(|value| **value == "positive")
        .count();
    let negative_count = source_directions
        .iter()
        .filter(|value| **value == "negative")
        .count();
    let mut tickers: BTreeSet<String> = BTreeSet::new();
    for row in &articles {
        if let Some(items) = row.get("tickers").and_then(Value::as_array) {
            for item in items {
                tickers.insert(py_str(Some(item)));
            }
        }
    }
    let mut map = Map::new();
    map.insert(
        "event_id".into(),
        Value::String(py_str(cluster.get("cluster_id")).replacen("cluster_", "event_", 1)),
    );
    map.insert("ticker".into(), Value::String(news_input.ticker.clone()));
    map.insert("event_type".into(), Value::String(event_type.clone()));
    let event_group = representative
        .get("event_group")
        .filter(|value| truthy(Some(value)))
        .cloned()
        .unwrap_or_else(|| {
            Value::String(
                if event_type == "MACRO" {
                    "macro"
                } else {
                    "company"
                }
                .into(),
            )
        });
    map.insert("event_group".into(), event_group);
    map.insert("eligible_evidence".into(), Value::Bool(eligible));
    map.insert(
        "exclusion_reasons".into(),
        Value::Array(
            exclusion_reasons
                .iter()
                .cloned()
                .map(Value::String)
                .collect(),
        ),
    );
    map.insert(
        "fact_status".into(),
        Value::String("reported_event_not_independently_verified".into()),
    );
    map.insert(
        "url".into(),
        representative.get("url").cloned().unwrap_or(Value::Null),
    );
    for field in ["occurred_at", "announcement_at", "scheduled_at"] {
        map.insert(
            field.into(),
            representative.get(field).cloned().unwrap_or(Value::Null),
        );
    }
    let independent = cluster
        .get("independent_reporting_ids")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let has_independent = independent
        .as_array()
        .map(|items| !items.is_empty())
        .unwrap_or(false);
    map.insert(
        "source_independence".into(),
        Value::String(
            if has_independent {
                "reported_lineage"
            } else {
                "unverified"
            }
            .into(),
        ),
    );
    map.insert("independent_reporting_ids".into(), independent);
    map.insert(
        "first_published_at".into(),
        cluster
            .get("first_published_at")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert(
        "article_count".into(),
        cluster.get("article_count").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "sources".into(),
        cluster.get("sources").cloned().unwrap_or(Value::Null),
    );
    let agreement = if positive_count != 0 && negative_count != 0 {
        "low"
    } else if publishers.len() > 1 && positive_count + negative_count != 0 {
        "high"
    } else if positive_count + negative_count != 0 {
        "single_source"
    } else {
        "unknown"
    };
    map.insert("source_agreement".into(), Value::String(agreement.into()));
    map.insert("direction".into(), Value::String(event_direction.into()));
    for field in [
        "relevance",
        "magnitude",
        "novelty",
        "source_quality",
        "event_strength",
    ] {
        map.insert(
            field.into(),
            scores.get(field).cloned().unwrap_or(Value::Null),
        );
    }
    map.insert(
        "effective_event_score".into(),
        rounded_value(effective_score),
    );
    map.insert(
        "effective_event_score_status".into(),
        Value::String(
            if effective_score.is_some() {
                "computed_from_gateway_scores"
            } else {
                "not_computed_missing_gateway_score_inputs"
            }
            .into(),
        ),
    );
    map.insert("expected_horizon".into(), Value::Null);
    map.insert(
        "age_sessions".into(),
        decay.get("age_sessions").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "half_life_sessions".into(),
        decay
            .get("half_life_sessions")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert(
        "time_decay".into(),
        rounded_value(decay.get("time_decay").and_then(Value::as_f64)),
    );
    map.insert(
        "ticker_tags".into(),
        Value::Array(tickers.into_iter().map(Value::String).collect()),
    );
    map.insert(
        "ticker_relevance".into(),
        Value::String(relevance(&articles, &news_input.ticker).into()),
    );
    map.insert(
        "member_directions".into(),
        Value::Array(
            source_directions
                .iter()
                .map(|value| Value::String((*value).into()))
                .collect(),
        ),
    );
    map.insert(
        "member_direction_conflict".into(),
        Value::Bool(positive_count != 0 && negative_count != 0),
    );
    let mut member_urls: BTreeSet<String> = BTreeSet::new();
    for row in &articles {
        if truthy(row.get("url")) {
            member_urls.insert(py_str(row.get("url")));
        }
    }
    map.insert(
        "member_urls".into(),
        Value::Array(
            member_urls
                .into_iter()
                .take(10)
                .map(Value::String)
                .collect(),
        ),
    );
    map.insert(
        "topic_terms".into(),
        Value::Array(topics.into_iter().map(Value::String).collect()),
    );
    map.insert(
        "headline".into(),
        representative.get("title").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "summary".into(),
        representative
            .get("summary")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert("market_reaction".into(), reaction);
    map.insert("impact".into(), impact);
    map.insert("already_priced_in".into(), priced_in);
    Ok(Value::Object(map))
}

fn build_opinion_card(cluster: &Value, context: &CardContext<'_>) -> Result<Value, String> {
    let news_input = context.news_input;
    let articles = cluster
        .get("articles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let texts: Vec<String> = articles
        .iter()
        .map(|row| {
            format!(
                "{} {}",
                py_str(row.get("title")),
                py_str(row.get("summary"))
            )
        })
        .collect();
    let mut scores: Vec<f64> = Vec::new();
    for text in &texts {
        if let Some(score) = opinion_score(text) {
            scores.push(score);
        }
    }
    let score = mean(&scores).map(|value| python_round(value, 6));
    let source_scores: Vec<f64> = articles
        .iter()
        .filter_map(|row| python_float(row.get("sentiment_score")))
        .collect();
    let representative = cluster
        .get("representative")
        .cloned()
        .unwrap_or(Value::Null);
    let decay = time_decay(
        &py_str(cluster.get("first_published_at")),
        context.bars,
        "OPINION",
        &news_input.horizon,
        context.policy,
        &news_input.market,
    )?;
    let (eligible, exclusion_reasons) = eligibility(cluster, news_input, &decay, context.policy);
    let mut map = Map::new();
    map.insert(
        "opinion_id".into(),
        Value::String(py_str(cluster.get("cluster_id")).replacen("cluster_", "opinion_", 1)),
    );
    map.insert(
        "published_at".into(),
        cluster
            .get("first_published_at")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert(
        "article_count".into(),
        cluster.get("article_count").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "sources".into(),
        cluster.get("sources").cloned().unwrap_or(Value::Null),
    );
    map.insert("eligible_evidence".into(), Value::Bool(eligible));
    map.insert(
        "exclusion_reasons".into(),
        Value::Array(
            exclusion_reasons
                .iter()
                .cloned()
                .map(Value::String)
                .collect(),
        ),
    );
    map.insert(
        "url".into(),
        representative.get("url").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "time_decay".into(),
        decay.get("time_decay").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "record_type".into(),
        representative
            .get("record_type")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert("sentiment".into(), Value::String(label(score)));
    map.insert("sentiment_score".into(), rounded_value(score));
    map.insert(
        "source_sentiment_score".into(),
        rounded_value(mean(&source_scores)),
    );
    map.insert(
        "source_sentiment_score_scale".into(),
        Value::String("source supplied [-1,+1]; not combined with opinion lexicon".into()),
    );
    map.insert(
        "headline".into(),
        representative.get("title").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "summary".into(),
        representative
            .get("summary")
            .cloned()
            .unwrap_or(Value::Null),
    );
    Ok(Value::Object(map))
}

// --------------------------------------------------------------------------- analysis helpers

struct NewsPolicy {
    half_lives: Map<String, Value>,
    age_limit: i64,
    decay_limit: f64,
}

fn parse_policy(policy: Option<&Value>) -> Result<NewsPolicy, String> {
    let value = policy.cloned().unwrap_or_else(|| Value::Object(Map::new()));
    let Some(map) = value.as_object() else {
        return Err("news policy must be an object".into());
    };
    let allowed = [
        "half_life_sessions_by_event_type",
        "max_age_calendar_days",
        "minimum_time_decay",
    ];
    if map.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err("unsupported news policy fields".into());
    }
    let half_lives = match map.get("half_life_sessions_by_event_type") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(rows)) => {
            for (key, raw) in rows {
                let valid = key.chars().count() <= 40
                    && match raw {
                        Value::Number(number) => number
                            .as_f64()
                            .map(|number| number.is_finite() && number > 0.0 && number <= 366.0)
                            .unwrap_or(false),
                        _ => false,
                    };
                if !valid {
                    return Err(
                        "news half-life policy requires bounded names and numeric session counts in (0,366]"
                            .into(),
                    );
                }
            }
            rows.clone()
        }
        Some(_) => return Err(
            "news half-life policy requires bounded names and numeric session counts in (0,366]"
                .into(),
        ),
    };
    let age_limit = match map.get("max_age_calendar_days") {
        None => NEWS_MAX_AGE_CALENDAR_DAYS,
        Some(Value::Number(number)) => {
            let value = number
                .as_i64()
                .ok_or_else(|| "news age window must be an integer in 1..366 days".to_string())?;
            if !(1..=366).contains(&value) {
                return Err("news age window must be an integer in 1..366 days".into());
            }
            value
        }
        Some(_) => return Err("news age window must be an integer in 1..366 days".into()),
    };
    let decay_limit = match map.get("minimum_time_decay") {
        None => 0.0625,
        Some(Value::Number(number)) => {
            let value = number.as_f64().unwrap_or(f64::NAN);
            if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                return Err("news minimum_time_decay must be in [0,1]".into());
            }
            value
        }
        Some(_) => return Err("news minimum_time_decay must be in [0,1]".into()),
    };
    Ok(NewsPolicy {
        half_lives,
        age_limit,
        decay_limit,
    })
}

fn policy_value(policy: &NewsPolicy) -> Value {
    let mut map = Map::new();
    map.insert(
        "half_life_sessions_by_event_type".into(),
        Value::Object(policy.half_lives.clone()),
    );
    map.insert(
        "max_age_calendar_days".into(),
        Value::from(policy.age_limit),
    );
    map.insert("minimum_time_decay".into(), Value::from(policy.decay_limit));
    Value::Object(map)
}

fn attach_ai_lean(
    result: &mut Value,
    news_input: &NewsInput,
    candidates: &[Value],
    ai_assessment: Option<&Value>,
    warnings: &mut Vec<String>,
) -> Value {
    let Some(assessment) = ai_assessment else {
        return build_ai_lean_block(
            None,
            candidates,
            "not_assessed",
            Some("no host AI lean artifact was supplied for this run"),
            None,
        );
    };
    match validate_ai_assessment(
        assessment,
        &news_input.ticker,
        &news_input.asof_timestamp,
        candidates,
    ) {
        Err(error) => {
            let bounded = bounded_text(&Value::String(error), 200);
            warnings.push(format!(
                "host AI lean artifact rejected and excluded from evidence: {bounded}"
            ));
            build_ai_lean_block(
                None,
                candidates,
                "invalid",
                Some(&bounded),
                Some(&[
                    "the deterministic News result is unchanged; no lean evidence is reported"
                        .to_string(),
                ]),
            )
        }
        Ok(validated) => {
            let assessed_count = validated
                .get("items")
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            if assessed_count != candidates.len() {
                warnings.push(format!(
                    "{} canonical news headline(s) were not assessed by the host AI",
                    candidates.len() - assessed_count
                ));
            }
            let (status, reason) = if assessed_count == candidates.len() {
                ("assessed", None)
            } else {
                (
                    "partial",
                    Some(format!(
                        "{} canonical headlines were not assessed",
                        candidates.len() - assessed_count
                    )),
                )
            };
            let block = build_ai_lean_block(
                Some(&validated),
                candidates,
                status,
                reason.as_deref(),
                None,
            );
            attach_lean_to_records(result, &block);
            block
        }
    }
}

fn strings_value(values: &[String]) -> Value {
    Value::Array(values.iter().cloned().map(Value::String).collect())
}

fn finalize_warnings(
    result: &mut Value,
    warnings: &[String],
    assessed_any: bool,
) -> Map<String, Value> {
    let counts = warning_severity_counts(warnings);
    if let Some(map) = result.as_object_mut() {
        map.insert("warnings".into(), strings_value(warnings));
        map.insert("warning_count".into(), Value::from(warnings.len()));
        map.insert("warning_details".into(), warning_details(warnings));
        map.insert(
            "warning_severity_counts".into(),
            Value::Object(counts.clone()),
        );
        if assessed_any {
            let evidence_affecting = counts
                .get(EVIDENCE_AFFECTING)
                .and_then(Value::as_i64)
                .unwrap_or(0);
            map.insert(
                "status".into(),
                Value::String(
                    if evidence_affecting != 0 {
                        "partial"
                    } else {
                        "complete"
                    }
                    .into(),
                ),
            );
        }
    }
    counts
}

fn unclassified_record(cluster: &Value) -> Value {
    let representative = cluster
        .get("representative")
        .cloned()
        .unwrap_or(Value::Null);
    let mut map = Map::new();
    map.insert(
        "news_id".into(),
        Value::String(py_str(cluster.get("cluster_id")).replacen("cluster_", "unclassified_", 1)),
    );
    map.insert(
        "published_at".into(),
        cluster
            .get("first_published_at")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert(
        "article_count".into(),
        cluster.get("article_count").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "sources".into(),
        cluster.get("sources").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "headline".into(),
        representative.get("title").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "summary".into(),
        representative
            .get("summary")
            .cloned()
            .unwrap_or(Value::Null),
    );
    map.insert(
        "url".into(),
        representative.get("url").cloned().unwrap_or(Value::Null),
    );
    map.insert(
        "reason".into(),
        Value::String(
            "no supported event category and no explicit opinion/social record type".into(),
        ),
    );
    Value::Object(map)
}

/// Analyze news records without Quant results, probabilities, or provider access.
pub fn analyze_news(
    news_input: &NewsInput,
    policy: Option<&Value>,
    ai_assessment: Option<&Value>,
) -> Result<Value, String> {
    let parsed_policy = parse_policy(policy)?;
    let cutoff = parse_timestamp(&Value::String(news_input.asof_timestamp.clone()))
        .map_err(|error| error.message().to_string())?;
    // Recheck the boundary at the analyst contract as defense in depth.
    let mut payload = Map::new();
    payload.insert("articles".into(), Value::Array(news_input.articles.clone()));
    let rechecked = normalize_articles(&Value::Object(payload), &news_input.asof_timestamp);
    let mut articles = rechecked.articles;
    let input_warnings = rechecked.warnings;
    let failed_domain =
        ["failed", "invalid", "unavailable", "empty"].contains(&news_input.news_status.as_str());
    if failed_domain {
        articles = Vec::new();
    }
    let deduplicated = deduplicate_articles(&articles);
    let clusters = deduplicated.clusters;
    let duplicate_count = deduplicated.duplicate_count;
    let opinion_clusters: Vec<Value> = clusters
        .iter()
        .filter(|cluster| cluster.get("role").and_then(Value::as_str) == Some("opinion"))
        .cloned()
        .collect();
    let mut event_clusters: Vec<Value> = Vec::new();
    let mut unclassified_clusters: Vec<Value> = Vec::new();
    for cluster in &clusters {
        if cluster.get("role").and_then(Value::as_str) == Some("opinion") {
            continue;
        }
        let representative = cluster
            .get("representative")
            .cloned()
            .unwrap_or(Value::Null);
        let category = event_type_for(
            &event_text(cluster),
            representative
                .get("event_type_hint")
                .and_then(Value::as_str),
        );
        let is_event = cluster.get("role").and_then(Value::as_str) == Some("event");
        if is_event || category != "UNCLASSIFIED" {
            event_clusters.push(cluster.clone());
        } else {
            unclassified_clusters.push(cluster.clone());
        }
    }
    let mut warnings: Vec<String> = news_input.warnings.clone();
    warnings.extend(input_warnings);
    if failed_domain {
        warnings.push("failed/unavailable news domain payload excluded from evidence".into());
    }
    if articles.len() != news_input.articles.len() {
        warnings.push(
            "NewsInput contained post-as-of records; they were excluded again before analysis"
                .into(),
        );
    }
    if articles.is_empty() {
        if news_input.source_sentiment.is_some() {
            warnings.push(
                "no point-in-time article records are available; event analysis is not assessed, provider aggregate sentiment is reported separately"
                    .into(),
            );
        } else {
            warnings.push(
                "no point-in-time news records are available; News/Sentiment is not assessed"
                    .into(),
            );
        }
    }
    if !unclassified_clusters.is_empty() {
        warnings.push(format!(
            "{} records could not be classified as a factual event or explicit opinion and were left unclassified",
            unclassified_clusters.len()
        ));
    }

    let bars = news_input.bars.clone();
    let frozen_closes: Vec<f64> = bars
        .iter()
        .map(|row| python_float(row.get("close")).unwrap_or(0.0))
        .collect();
    let frozen_dates: Vec<String> = bars
        .iter()
        .map(|row| py_str(row.get("date")).chars().take(10).collect::<String>())
        .collect();
    let policy_object = policy_value(&parsed_policy);
    let context = CardContext {
        news_input,
        policy: &policy_object,
        bars: &bars,
        closes: &frozen_closes,
        dates: &frozen_dates,
    };
    let all_events: Vec<Value> = event_clusters
        .iter()
        .map(|cluster| build_event_card(cluster, &context))
        .collect::<Result<_, _>>()?;
    let all_opinions: Vec<Value> = opinion_clusters
        .iter()
        .map(|cluster| build_opinion_card(cluster, &context))
        .collect::<Result<_, _>>()?;
    let unclassified_records: Vec<Value> = unclassified_clusters
        .iter()
        .map(unclassified_record)
        .collect();
    let lean_inputs = lean_candidates(
        &all_events,
        &all_opinions,
        Some(&unclassified_records),
        LEAN_CANDIDATE_LIMIT,
    );
    let events: Vec<Value> = all_events
        .iter()
        .filter(|event| truthy(event.get("eligible_evidence")))
        .cloned()
        .collect();
    let opinions: Vec<Value> = all_opinions
        .iter()
        .filter(|opinion| truthy(opinion.get("eligible_evidence")))
        .cloned()
        .collect();
    let excluded_evidence: Vec<Value> = all_events
        .iter()
        .chain(all_opinions.iter())
        .filter(|item| !truthy(item.get("eligible_evidence")))
        .cloned()
        .collect();
    if !excluded_evidence.is_empty() {
        warnings.push(format!(
            "{} canonical records excluded from current bias due to relevance/time/source gates",
            excluded_evidence.len()
        ));
    }
    let overall_direction = direction_aggregate(
        &events
            .iter()
            .map(|event| py_str(event.get("direction")))
            .collect::<Vec<_>>(),
    );
    let opinion_scores: Vec<f64> = opinions
        .iter()
        .filter_map(|item| item.get("sentiment_score").and_then(Value::as_f64))
        .collect();
    let sentiment_score = if opinion_scores.is_empty() {
        None
    } else {
        rounded(mean(&opinion_scores))
    };
    let provider_sentiment = news_input
        .source_sentiment
        .clone()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let mut provider_for_bias = Value::Object(Map::new());
    if provider_sentiment
        .as_object()
        .map(|map| !map.is_empty())
        .unwrap_or(false)
    {
        let available = provider_sentiment.get("available_at");
        let ticker_matches = provider_sentiment.get("ticker").and_then(Value::as_str)
            == Some(news_input.ticker.as_str());
        let mut background_only = failed_domain || !ticker_matches;
        match available {
            None => background_only = true,
            Some(value) => match parse_timestamp(value) {
                Err(_) => background_only = true,
                Ok(stamp) => {
                    if stamp > cutoff
                        || (cutoff - stamp).num_milliseconds() as f64 / 1000.0 / 86400.0
                            > parsed_policy.age_limit as f64
                    {
                        background_only = true;
                    }
                }
            },
        }
        if background_only {
            warnings.push(
                "provider sentiment aggregate is background-only: target/time/domain eligibility unverified"
                    .into(),
            );
        } else {
            provider_for_bias = provider_sentiment.clone();
        }
    }
    let bullish_percent = python_float(provider_for_bias.get("bullish_percent"));
    let bearish_percent = python_float(provider_for_bias.get("bearish_percent"));
    let provider_direction = match (bullish_percent, bearish_percent) {
        (Some(bullish), Some(bearish)) if bullish > bearish => "positive",
        (Some(bullish), Some(bearish)) if bearish > bullish => "negative",
        (Some(_), Some(_)) => "neutral",
        _ => "unknown",
    };
    let sentiment_label = match sentiment_score {
        Some(score) => label(Some(score)),
        None => {
            if ["positive", "negative"].contains(&provider_direction) {
                format!("provider_{provider_direction}")
            } else {
                provider_direction.to_string()
            }
        }
    };
    let (source_agreement_state, mut cross_source_divergence) = source_agreement(&opinions);
    let opinion_direction = match sentiment_score {
        Some(score) if score > 0.15 => "positive",
        Some(score) if score < -0.15 => "negative",
        Some(_) => "neutral",
        None => "unknown",
    };
    let mut sentiment_direction = if opinion_direction != "unknown" {
        opinion_direction
    } else {
        provider_direction
    };
    if cross_source_divergence == "high" && !opinions.is_empty() {
        sentiment_direction = "mixed";
    }
    if opinion_direction != "unknown" && provider_direction != "unknown" {
        if opinion_direction != provider_direction {
            cross_source_divergence = if (opinion_direction == "positive"
                && provider_direction == "negative")
                || (opinion_direction == "negative" && provider_direction == "positive")
            {
                "high"
            } else {
                "medium"
            }
            .into();
            sentiment_direction = "mixed";
        } else if cross_source_divergence != "high"
            && (!opinions.is_empty() || provider_direction != "unknown")
        {
            cross_source_divergence = "low".into();
        }
    }
    let reaction_statuses: Vec<&str> = events
        .iter()
        .filter_map(|event| {
            event
                .get("market_reaction")
                .and_then(|reaction| reaction.get("status"))
                .and_then(Value::as_str)
        })
        .filter(|status| {
            [
                "confirmed_positive",
                "confirmed_negative",
                "event_price_divergence_negative",
                "event_price_divergence_positive",
            ]
            .contains(status)
        })
        .collect();
    let market_confirmation = if reaction_statuses.is_empty() {
        "not_assessed"
    } else if reaction_statuses
        .iter()
        .all(|status| *status == "confirmed_positive")
    {
        "positive"
    } else if reaction_statuses
        .iter()
        .all(|status| *status == "confirmed_negative")
    {
        "negative"
    } else {
        let distinct: BTreeSet<&str> = reaction_statuses.iter().copied().collect();
        let first = reaction_statuses.first().copied().unwrap_or("");
        if distinct.len() == 1
            && [
                "event_price_divergence_negative",
                "event_price_divergence_positive",
            ]
            .contains(&first)
        {
            "divergent"
        } else {
            "mixed"
        }
    };

    // Sort: (effective score present, effective score or 0, first published) descending.
    let mut sorted_events = events;
    sorted_events.sort_by(|left, right| {
        let key = |event: &Value| {
            let score = event.get("effective_event_score").and_then(Value::as_f64);
            (
                score.is_some(),
                score.unwrap_or(0.0),
                py_str(event.get("first_published_at")),
            )
        };
        key(right)
            .partial_cmp(&key(left))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let events = sorted_events;
    let positive: Vec<&Value> = events
        .iter()
        .filter(|event| event.get("direction").and_then(Value::as_str) == Some("positive"))
        .collect();
    let negative: Vec<&Value> = events
        .iter()
        .filter(|event| event.get("direction").and_then(Value::as_str) == Some("negative"))
        .collect();
    let mixed_events: Vec<&Value> = events
        .iter()
        .filter(|event| event.get("direction").and_then(Value::as_str) == Some("mixed"))
        .collect();
    let mut independent: BTreeSet<String> = BTreeSet::new();
    for event in &events {
        if let Some(items) = event
            .get("independent_reporting_ids")
            .and_then(Value::as_array)
        {
            for item in items {
                independent.insert(py_str(Some(item)));
            }
        }
    }
    let calendar_verified = matches!(
        news_input
            .coverage
            .get("calendar_verification")
            .and_then(Value::as_str),
        Some("source_supplied_schedule" | "library_verified")
    );
    let mut confidence = if events.len() >= 3
        && independent.len() >= 2
        && events.iter().all(|event| {
            event
                .get("source_quality")
                .and_then(Value::as_f64)
                .map(|value| value >= 0.5)
                .unwrap_or(false)
        })
        && ["positive", "negative"].contains(&market_confirmation)
        && calendar_verified
    {
        "medium"
    } else {
        "low"
    };
    let limitations: Vec<Value> = [
        "Event classification and opinion tone use deterministic keyword screening; they are not a calibrated semantic model.",
        "News direction and sentiment are descriptive and do not generate or adjust Quant P(up).",
        "Daily-bar market reactions are temporal associations, not proof that a headline caused the price move.",
        "Events whose member headlines disagree are listed as mixed and are never counted as one-sided bullish or bearish evidence.",
    ]
    .iter()
    .map(|text| Value::String((*text).into()))
    .collect();
    if events.is_empty() && opinion_scores.is_empty() && provider_direction == "unknown" {
        confidence = if articles.is_empty() {
            "unavailable"
        } else {
            "low"
        };
    }
    // Three states, one meaning each: only evidence-affecting warnings downgrade.
    let details = warning_details(&warnings);
    let severity_counts = warning_severity_counts(&warnings);
    let assessed_any = !events.is_empty()
        || !opinions.is_empty()
        || !provider_for_bias
            .as_object()
            .map(Map::is_empty)
            .unwrap_or(true);
    let status = if !assessed_any {
        "not_assessed"
    } else if severity_counts
        .get(EVIDENCE_AFFECTING)
        .and_then(Value::as_i64)
        .unwrap_or(0)
        != 0
    {
        "partial"
    } else {
        "complete"
    };
    let volatility = volatility_regime(&events, news_input.horizon_sessions());
    let priced_in = aggregate_priced_in(&events);
    let mut bucket_counts: Map<String, Value> = Map::new();
    for event in &events {
        let bucket = event
            .get("impact")
            .and_then(|impact| impact.get("event_time_bucket"))
            .and_then(|bucket| bucket.get("bucket"))
            .map(|value| py_str(Some(value)))
            .unwrap_or_else(|| "unknown".into());
        let current = bucket_counts
            .get(&bucket)
            .and_then(Value::as_i64)
            .unwrap_or(0);
        bucket_counts.insert(bucket, Value::from(current + 1));
    }

    let mut result = Map::new();
    result.insert(
        "schema_version".into(),
        Value::String(NEWS_SCHEMA_VERSION.into()),
    );
    result.insert("status".into(), Value::String(status.into()));
    result.insert(
        "news_input_digest".into(),
        Value::String(news_input_digest(news_input)),
    );
    result.insert("warning_count".into(), Value::from(warnings.len()));
    result.insert("ticker".into(), Value::String(news_input.ticker.clone()));
    result.insert("market".into(), Value::String(news_input.market.clone()));
    result.insert("horizon".into(), Value::String(news_input.horizon.clone()));
    result.insert(
        "asof_timestamp".into(),
        Value::String(news_input.asof_timestamp.clone()),
    );
    result.insert(
        "snapshot_id".into(),
        Value::String(news_input.snapshot_id.clone()),
    );
    let mut method = Map::new();
    method.insert(
        "event_analyst".into(),
        Value::String("deterministic_keyword_screen_v1".into()),
    );
    method.insert(
        "sentiment_analyst".into(),
        Value::String("separate_opinion_lexicon_v1".into()),
    );
    method.insert(
        "event_lexicon_version".into(),
        Value::String(LEXICON_VERSION.into()),
    );
    method.insert(
        "supported_languages".into(),
        Value::Array(
            SUPPORTED_LANGUAGES
                .iter()
                .map(|value| Value::String((*value).into()))
                .collect(),
        ),
    );
    method.insert(
        "lexicon_coverage_note".into(),
        Value::String(COVERAGE_NOTE.into()),
    );
    method.insert(
        "source_tier_rule_version".into(),
        Value::String(SOURCE_TIER_RULE_VERSION.into()),
    );
    method.insert("event_opinion_separation".into(), Value::Bool(true));
    method.insert(
        "news_probability".into(),
        Value::String("not_emitted".into()),
    );
    method.insert("ai_lean_layer".into(), Value::String(AI_LEAN_LAYER.into()));
    method.insert(
        "market_data".into(),
        Value::String("snapshot_frozen_OHLCV_only".into()),
    );
    method.insert("calibrated".into(), Value::Bool(false));
    result.insert("method".into(), Value::Object(method));

    let over_analysis_cap = news_input
        .coverage
        .get("excluded_at_normalization")
        .and_then(|excluded| excluded.get("over_analysis_cap"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let mut coverage = Map::new();
    coverage.insert(
        "news_domain_status".into(),
        Value::String(news_input.news_status.clone()),
    );
    coverage.insert("asof_article_count".into(), Value::from(articles.len()));
    coverage.insert("canonical_event_count".into(), Value::from(events.len()));
    coverage.insert(
        "canonical_opinion_count".into(),
        Value::from(opinions.len()),
    );
    coverage.insert(
        "unclassified_record_count".into(),
        Value::from(unclassified_records.len()),
    );
    coverage.insert(
        "duplicate_records_merged".into(),
        Value::from(duplicate_count),
    );
    coverage.insert("analyzed_market_bar_count".into(), Value::from(bars.len()));
    coverage.insert("eligible_event_count".into(), Value::from(events.len()));
    coverage.insert("eligible_opinion_count".into(), Value::from(opinions.len()));
    coverage.insert(
        "excluded_evidence_count".into(),
        Value::from(excluded_evidence.len() as i64 + over_analysis_cap),
    );
    coverage.insert(
        "retrieved_window".into(),
        news_input
            .coverage
            .get("retrieved_window")
            .cloned()
            .unwrap_or(Value::Null),
    );
    let mut publication_times: Vec<String> = Vec::new();
    for article in &articles {
        if let Some(value) = article.get("published_at").and_then(Value::as_str) {
            publication_times.push(value.to_string());
        }
    }
    coverage.insert(
        "observed_publication_start".into(),
        publication_times
            .iter()
            .cloned()
            .min()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    coverage.insert(
        "observed_publication_end".into(),
        publication_times
            .into_iter()
            .max()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    coverage.insert(
        "evidence_max_age_calendar_days".into(),
        Value::from(parsed_policy.age_limit),
    );
    result.insert("coverage".into(), Value::Object(coverage));
    result.insert("overall_direction".into(), Value::String(overall_direction));

    let event_strength_values: Vec<f64> = events
        .iter()
        .filter_map(|event| event.get("event_strength").and_then(Value::as_f64))
        .collect();
    result.insert(
        "event_strength".into(),
        rounded_value(mean(&event_strength_values)),
    );
    let mut sentiment = Map::new();
    sentiment.insert("label".into(), Value::String(sentiment_label));
    sentiment.insert("score".into(), rounded_value(sentiment_score));
    sentiment.insert(
        "score_scale".into(),
        Value::String("heuristic -1 to +1; uncalibrated".into()),
    );
    sentiment.insert(
        "evidence_basis".into(),
        Value::String(
            if sentiment_score.is_some() {
                "opinion_text_lexicon"
            } else if provider_direction != "unknown" {
                "provider_aggregate"
            } else {
                "unavailable"
            }
            .into(),
        ),
    );
    sentiment.insert(
        "provider_direction".into(),
        Value::String(provider_direction.into()),
    );
    sentiment.insert(
        "consensus_direction".into(),
        Value::String(sentiment_direction.into()),
    );
    sentiment.insert(
        "provider_aggregate".into(),
        if provider_sentiment
            .as_object()
            .map(Map::is_empty)
            .unwrap_or(true)
        {
            Value::Null
        } else {
            provider_sentiment.clone()
        },
    );
    sentiment.insert(
        "confidence".into(),
        Value::String(
            if !opinion_scores.is_empty() || provider_direction != "unknown" {
                "low"
            } else {
                "unavailable"
            }
            .into(),
        ),
    );
    sentiment.insert("dominant_narrative".into(), Value::Null);
    let mut sentiment_topics: BTreeSet<String> = BTreeSet::new();
    for opinion in &opinions {
        for term in matched_topics(&format!(
            "{} {}",
            py_str(opinion.get("headline")),
            py_str(opinion.get("summary"))
        )) {
            sentiment_topics.insert(term);
        }
    }
    sentiment.insert(
        "topic_terms".into(),
        Value::Array(sentiment_topics.into_iter().map(Value::String).collect()),
    );
    sentiment.insert(
        "cross_source_divergence".into(),
        Value::String(cross_source_divergence.clone()),
    );
    result.insert("sentiment".into(), Value::Object(sentiment));
    result.insert(
        "source_agreement".into(),
        Value::String(source_agreement_state.clone()),
    );
    let mut opinion_agreement = Map::new();
    opinion_agreement.insert("state".into(), Value::String(source_agreement_state));
    opinion_agreement.insert(
        "cross_source_divergence".into(),
        Value::String(cross_source_divergence),
    );
    opinion_agreement.insert(
        "basis".into(),
        Value::String("opinion_lexicon_and_provider_aggregate_only".into()),
    );
    result.insert(
        "opinion_source_agreement".into(),
        Value::Object(opinion_agreement),
    );
    result.insert(
        "event_source_agreement".into(),
        event_source_agreement(&events),
    );
    result.insert("expected_volatility_regime".into(), volatility);
    result.insert("already_priced_in".into(), priced_in);
    result.insert(
        "event_time_bucket_counts".into(),
        Value::Object(bucket_counts),
    );
    result.insert(
        "market_confirmation".into(),
        Value::String(market_confirmation.into()),
    );
    let mut price_reaction = Map::new();
    price_reaction.insert(
        "basis".into(),
        Value::String("frozen_snapshot_ohlcv".into()),
    );
    price_reaction.insert(
        "independence".into(),
        Value::String("not_independent_same_market_data".into()),
    );
    price_reaction.insert(
        "confirmation".into(),
        Value::String(market_confirmation.into()),
    );
    result.insert("price_reaction".into(), Value::Object(price_reaction));
    result.insert(
        "major_events".into(),
        Value::Array(events.iter().take(10).cloned().collect()),
    );
    result.insert(
        "background_records".into(),
        Value::Array(
            excluded_evidence
                .iter()
                .take(10)
                .map(|item| {
                    let mut row = Map::new();
                    for key in [
                        "event_id",
                        "opinion_id",
                        "headline",
                        "url",
                        "first_published_at",
                        "published_at",
                        "ticker_relevance",
                        "exclusion_reasons",
                    ] {
                        row.insert(key.into(), item.get(key).cloned().unwrap_or(Value::Null));
                    }
                    Value::Object(row)
                })
                .collect(),
        ),
    );
    let summarize = |item: &Value| {
        let mut row = Map::new();
        row.insert(
            "event_id".into(),
            item.get("event_id").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "event_type".into(),
            item.get("event_type").cloned().unwrap_or(Value::Null),
        );
        row.insert(
            "headline".into(),
            item.get("headline").cloned().unwrap_or(Value::Null),
        );
        Value::Object(row)
    };
    result.insert(
        "catalysts".into(),
        Value::Array(
            positive
                .iter()
                .take(5)
                .map(|event| summarize(event))
                .collect(),
        ),
    );
    result.insert(
        "risks".into(),
        Value::Array(
            negative
                .iter()
                .take(5)
                .map(|event| summarize(event))
                .collect(),
        ),
    );
    result.insert(
        "mixed_events".into(),
        Value::Array(
            mixed_events
                .iter()
                .take(5)
                .map(|event| summarize(event))
                .collect(),
        ),
    );
    result.insert(
        "opinion_records".into(),
        Value::Array(opinions.iter().take(20).cloned().collect()),
    );
    result.insert(
        "unclassified_records".into(),
        Value::Array(unclassified_records.iter().take(20).cloned().collect()),
    );
    result.insert("confidence".into(), Value::String(confidence.into()));
    result.insert("warnings".into(), strings_value(&warnings));
    result.insert("warning_details".into(), details);
    result.insert(
        "warning_severity_counts".into(),
        Value::Object(severity_counts),
    );
    result.insert("limitations".into(), Value::Array(limitations));
    result.insert(
        "ai_lean_candidates".into(),
        Value::Array(lean_inputs.clone()),
    );
    let mut result = Value::Object(result);
    let lean_block = attach_ai_lean(
        &mut result,
        news_input,
        &lean_inputs,
        ai_assessment,
        &mut warnings,
    );
    if let Some(map) = result.as_object_mut() {
        map.insert("ai_lean".into(), lean_block);
    }
    finalize_warnings(&mut result, &warnings, assessed_any);
    Ok(result)
}

/// Run News on the restricted in-memory contract; this entrypoint has no filesystem path.
///
/// `base_result` lets a previously computed, frozen News result be reused when its
/// `news_input_digest` matches the current input. Only the cheap host AI attachment is
/// redone.
pub fn run_news(
    news_input: &NewsInput,
    policy: Option<&Value>,
    ai_assessment: Option<&Value>,
    base_result: Option<&Value>,
) -> Result<Value, String> {
    let digest = news_input_digest(news_input);
    if let Some(base) = base_result.filter(|value| value.is_object()) {
        let matches =
            base.get("news_input_digest").and_then(Value::as_str) == Some(digest.as_str());
        if matches {
            let mut reused = base.clone();
            let candidates: Vec<Value> = reused
                .get("ai_lean_candidates")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let mut warnings: Vec<String> = reused
                .get("warnings")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(|item| py_str(Some(item))).collect())
                .unwrap_or_default();
            let block = attach_ai_lean(
                &mut reused,
                news_input,
                &candidates,
                ai_assessment,
                &mut warnings,
            );
            let assessed_any = reused.get("status").and_then(Value::as_str) != Some("not_assessed");
            if let Some(map) = reused.as_object_mut() {
                map.insert("ai_lean".into(), block);
            }
            finalize_warnings(&mut reused, &warnings, assessed_any);
            if let Some(map) = reused.as_object_mut() {
                map.insert("reused_from_preview".into(), Value::Bool(true));
            }
            return Ok(reused);
        }
    }
    analyze_news(news_input, policy, ai_assessment)
}

/// Build the SHA-256 freeze receipt for one result name (`freeze_result` value part).
pub fn freeze_receipt(result_name: &str, result: &Value, frozen_at: &str) -> Value {
    json!({
        "result_file": result_name,
        "sha256": result_sha256(result),
        "frozen_at": frozen_at,
    })
}

/// Verify a result against its adjacent freeze receipt (`load_frozen_result` value part).
pub fn verify_frozen_result(
    result_file_name: &str,
    result: &Value,
    freeze: &Value,
) -> Result<(), String> {
    let actual = result_sha256(result);
    let recorded_name = freeze.get("result_file").and_then(Value::as_str);
    let recorded_hash = freeze.get("sha256").and_then(Value::as_str);
    if recorded_name != Some(result_file_name) || recorded_hash != Some(actual.as_str()) {
        return Err(format!("frozen result digest mismatch: {result_file_name}"));
    }
    Ok(())
}
