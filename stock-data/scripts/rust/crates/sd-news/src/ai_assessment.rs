//! Validate and attach a host-supplied AI headline-level news lean
//! (`news/ai_assessment.py`).
//!
//! The Skill package stays offline and deterministic: it never calls a model itself.
//! The host may produce a structured `news_ai_assessment` artifact exactly like the
//! collection adapter receives provider DATA; this module only validates that artifact
//! against the records actually analysed and attaches it as display evidence.
//!
//! Hard invariants: the artifact may only reference news ids that exist in this run's
//! candidate set; only allow-listed fields are accepted; Quant vocabulary inside string
//! values rejects the artifact. The merged lean never touches `direction`,
//! `overall_direction`, `confidence`, `sentiment`, `market_confirmation`,
//! `event_strength`, `effective_event_score` or Quant `P(up)`.

use std::collections::{BTreeSet, HashMap};

use serde_json::{Map, Value};

use sd_core::contracts::{parse_timestamp, python_repr, python_str_repr};
use sd_core::security::{bounded_text, safe_url};

/// Schema version accepted from the host.
pub const AI_ASSESSMENT_SCHEMA_VERSION: &str = "1.0";
/// Declared basis of the lean layer.
pub const AI_LEAN_BASIS: &str = "headline_level_ai_screen_v1";
/// Display layer name.
pub const AI_LEAN_LAYER: &str = "host_supplied_headline_lean_v1";

/// Accepted lean labels.
pub const LEAN_LABELS: [&str; 5] = ["bullish", "bearish", "mixed", "neutral", "unclear"];
/// Accepted evidence grades.
pub const EVIDENCE_GRADES: [&str; 5] = [
    "reported_event",
    "analyst_opinion",
    "headline_only",
    "rumour",
    "unclear",
];
/// Accepted target relevance values.
pub const TARGET_RELEVANCE: [&str; 4] = ["direct", "indirect", "unrelated", "unclear"];
/// Accepted importance values.
pub const IMPORTANCE: [&str; 2] = ["major", "routine"];

const MAX_ITEMS: usize = 500;
const REASON_LIMIT: usize = 400;
const HEADLINE_LIMIT: usize = 300;

const TOP_KEYS: [&str; 6] = [
    "schema_version",
    "ticker",
    "asof_timestamp",
    "basis",
    "model",
    "items",
];
const ITEM_KEYS: [&str; 11] = [
    "news_id",
    "headline",
    "source",
    "published_at",
    "url",
    "lean",
    "reason",
    "evidence_grade",
    "relevance_basis",
    "target_relevance",
    "importance",
];
const MODEL_KEYS: [&str; 3] = ["id", "prompt_sha256", "generated_at_utc"];

const FORBIDDEN_TOKENS: [&str; 10] = [
    "prob_up",
    "p(up)",
    "probability",
    "probabilities",
    "alignment",
    "consensus",
    "quant_result",
    "confidence_score",
    "calibrated_probability",
    "报告概率",
];

fn reject<T>(message: &str) -> Result<T, String> {
    Err(message.to_string())
}

fn text_field(
    value: Option<&Value>,
    field: &str,
    limit: usize,
    required: bool,
) -> Result<Option<String>, String> {
    let Some(value) = value else {
        return if required {
            reject(&format!("ai assessment {field} is required"))
        } else {
            Ok(None)
        };
    };
    let Value::String(raw) = value else {
        return reject(&format!("ai assessment {field} must be a string"));
    };
    let bounded = bounded_text(&Value::String(raw.clone()), limit);
    if required && bounded.is_empty() {
        return reject(&format!("ai assessment {field} must not be empty"));
    }
    Ok(if bounded.is_empty() {
        None
    } else {
        Some(bounded)
    })
}

fn scan_forbidden(value: &str, field: &str) -> Result<(), String> {
    let folded = value.to_lowercase();
    for token in FORBIDDEN_TOKENS {
        if folded.contains(token) {
            return reject(&format!(
                "ai assessment {field} contains Quant vocabulary '{}'; the lean layer must not carry Quant fields",
                token
            ));
        }
    }
    Ok(())
}

fn lean_identity(assessment: &Map<String, Value>) -> String {
    let mut pairs: Vec<(String, String)> = assessment
        .iter()
        .map(|(key, value)| (key.clone(), python_repr(value)))
        .collect();
    pairs.sort();
    let rendered: Vec<String> = pairs
        .iter()
        .map(|(key, value)| format!("({}, {})", python_str_repr(key), value))
        .collect();
    let repr = format!("[{}]", rendered.join(", "));
    let digest = sha256_hex(repr.as_bytes());
    format!("ai_lean_{}", &digest[..12])
}

fn identity_mismatch(
    provided: Option<&str>,
    recorded: Option<&Value>,
    field: &str,
) -> Option<String> {
    let provided = provided?;
    let recorded_text = recorded
        .map(|value| sd_core::contracts::py_str(value).trim().to_string())
        .unwrap_or_default();
    if recorded_text.is_empty() {
        return Some(format!(
            "ai assessment {field} was supplied but the analysed record has no {field} to verify it against"
        ));
    }
    let provided_text = provided.trim();
    if !provided_text.is_empty()
        && (provided_text == recorded_text || recorded_text.starts_with(provided_text))
    {
        return None;
    }
    Some(format!(
        "ai assessment {field} does not match the analysed record; identity fields must be copied, not authored"
    ))
}

/// Map every analyzable news id to its record, including pre-gate records.
pub fn candidate_index(candidates: &[Value]) -> Map<String, Value> {
    let mut index = Map::new();
    for row in candidates {
        if let Some(news_id) = row.get("news_id").and_then(Value::as_str) {
            if !news_id.is_empty() && !index.contains_key(news_id) {
                index.insert(news_id.to_string(), row.clone());
            }
        }
    }
    index
}

fn sorted_unknown(map: &Map<String, Value>, allowed: &[&str]) -> Vec<String> {
    let mut unknown: Vec<String> = map
        .keys()
        .filter(|key| !allowed.contains(&key.as_str()))
        .cloned()
        .collect();
    unknown.sort();
    unknown
}

/// Return the normalized lean items or an error message.
pub fn validate_ai_assessment(
    assessment: &Value,
    ticker: &str,
    asof_timestamp: &str,
    candidates: &[Value],
) -> Result<Value, String> {
    let Some(assessment_map) = assessment.as_object() else {
        return reject("ai assessment must be a JSON object");
    };
    let unknown = sorted_unknown(assessment_map, &TOP_KEYS);
    if !unknown.is_empty() {
        return reject(&format!(
            "ai assessment has unsupported field(s): {}",
            unknown.join(", ")
        ));
    }
    let schema_version = assessment_map.get("schema_version");
    if schema_version.and_then(Value::as_str) != Some(AI_ASSESSMENT_SCHEMA_VERSION) {
        return reject(&format!(
            "ai assessment schema_version must be '{}'",
            AI_ASSESSMENT_SCHEMA_VERSION
        ));
    }
    if assessment_map.get("ticker").and_then(Value::as_str) != Some(ticker) {
        return reject("ai assessment ticker does not match the analyzed ticker");
    }
    if assessment_map.get("asof_timestamp").and_then(Value::as_str) != Some(asof_timestamp) {
        return reject("ai assessment as-of does not match the snapshot as-of");
    }
    if let Some(basis) = assessment_map.get("basis") {
        if !basis.is_null() && basis.as_str() != Some(AI_LEAN_BASIS) {
            return reject(&format!(
                "ai assessment basis must be '{}' when supplied",
                AI_LEAN_BASIS
            ));
        }
    }
    let model = assessment_map.get("model");
    if let Some(model) = model {
        if !model.is_null() {
            let Some(model_map) = model.as_object() else {
                return reject("ai assessment model must be an object");
            };
            let model_unknown = sorted_unknown(model_map, &MODEL_KEYS);
            if !model_unknown.is_empty() {
                return reject(&format!(
                    "ai assessment model has unsupported field(s): {}",
                    model_unknown.join(", ")
                ));
            }
            let mut keys: Vec<String> = model_map.keys().cloned().collect();
            keys.sort();
            for key in keys {
                text_field(model_map.get(&key), &format!("model.{key}"), 200, true)?;
            }
        }
    }
    let Some(raw_items) = assessment_map.get("items") else {
        return reject("ai assessment items must be a non-empty array");
    };
    let Some(raw_items) = raw_items.as_array() else {
        return reject("ai assessment items must be a non-empty array");
    };
    if raw_items.is_empty() {
        return reject("ai assessment items must be a non-empty array");
    }
    if raw_items.len() > MAX_ITEMS {
        return reject(&format!(
            "ai assessment may carry at most {MAX_ITEMS} items"
        ));
    }
    let known = candidate_index(candidates);
    if known.is_empty() {
        return reject("ai assessment was supplied but this run has no analyzable news records");
    }
    let mut items: Vec<Value> = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    for (position, raw) in raw_items.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return reject(&format!("ai assessment item {position} must be an object"));
        };
        let item_unknown = sorted_unknown(raw_map, &ITEM_KEYS);
        if !item_unknown.is_empty() {
            return reject(&format!(
                "ai assessment item {position} has unsupported field(s): {}",
                item_unknown.join(", ")
            ));
        }
        let Some(news_id) = text_field(
            raw_map.get("news_id"),
            &format!("item {position} news_id"),
            120,
            true,
        )?
        else {
            return reject("ai assessment news_id is required");
        };
        if !known.contains_key(&news_id) {
            return reject(&format!(
                "ai assessment item {position} references unknown news id '{news_id}'"
            ));
        }
        if seen.contains(&news_id) {
            return reject(&format!("ai assessment repeats news id '{news_id}'"));
        }
        seen.insert(news_id.clone());
        let Some(lean) = raw_map.get("lean").and_then(Value::as_str) else {
            return reject(&format!(
                "ai assessment item {position} lean must be one of {}",
                LEAN_LABELS.join(", ")
            ));
        };
        if !LEAN_LABELS.contains(&lean) {
            return reject(&format!(
                "ai assessment item {position} lean must be one of {}",
                LEAN_LABELS.join(", ")
            ));
        }
        let evidence_grade = raw_map
            .get("evidence_grade")
            .and_then(Value::as_str)
            .unwrap_or("unclear");
        if !EVIDENCE_GRADES.contains(&evidence_grade) {
            return reject(&format!(
                "ai assessment item {position} evidence_grade must be one of {}",
                EVIDENCE_GRADES.join(", ")
            ));
        }
        let target_relevance = raw_map
            .get("target_relevance")
            .and_then(Value::as_str)
            .unwrap_or("unclear");
        if !TARGET_RELEVANCE.contains(&target_relevance) {
            return reject(&format!(
                "ai assessment item {position} target_relevance must be one of {}",
                TARGET_RELEVANCE.join(", ")
            ));
        }
        let importance = raw_map
            .get("importance")
            .and_then(Value::as_str)
            .unwrap_or("routine");
        if !IMPORTANCE.contains(&importance) {
            return reject(&format!(
                "ai assessment item {position} importance must be one of {}",
                IMPORTANCE.join(", ")
            ));
        }
        let mut headline = text_field(
            raw_map.get("headline"),
            &format!("item {position} headline"),
            HEADLINE_LIMIT,
            false,
        )?;
        let reason = text_field(
            raw_map.get("reason"),
            &format!("item {position} reason"),
            REASON_LIMIT,
            true,
        )?;
        let source = text_field(
            raw_map.get("source"),
            &format!("item {position} source"),
            120,
            false,
        )?;
        let published_at = text_field(
            raw_map.get("published_at"),
            &format!("item {position} published_at"),
            40,
            false,
        )?;
        let relevance_basis = text_field(
            raw_map.get("relevance_basis"),
            &format!("item {position} relevance_basis"),
            60,
            false,
        )?;
        let mut url: Option<String> = None;
        if let Some(raw_url) = raw_map.get("url") {
            if !raw_url.is_null() {
                let text = text_field(Some(raw_url), &format!("item {position} url"), 600, true)?;
                url = text
                    .as_deref()
                    .and_then(|text| safe_url(&Value::String(text.to_string())));
            }
        }
        for (field, value) in [
            ("headline", headline.as_deref()),
            ("reason", reason.as_deref()),
            ("source", source.as_deref()),
        ] {
            if let Some(value) = value {
                scan_forbidden(value, &format!("item {position} {field}"))?;
            }
        }
        let record = known.get(&news_id).cloned().unwrap_or(Value::Null);
        if let Some(value) = &headline {
            headline = Some(value.replace(['\n', '\r'], " "));
        }
        for (field, provided, recorded_key) in [
            ("headline", headline.clone(), "headline"),
            ("source", source.clone(), "source"),
            ("url", url.clone(), "url"),
        ] {
            let recorded = record.get(recorded_key);
            if let Some(mismatch) = identity_mismatch(
                provided.as_deref(),
                recorded,
                &format!("item {position} {field}"),
            ) {
                return reject(&mismatch);
            }
        }
        if let Some(published_at) = &published_at {
            let recorded_at = record
                .get("published_at")
                .filter(|value| !value.is_null())
                .or_else(|| record.get("first_published_at"));
            let same_instant = match recorded_at {
                Some(recorded_at) if !recorded_at.is_null() => {
                    match (
                        parse_timestamp(&Value::String(published_at.clone())),
                        parse_timestamp(recorded_at),
                    ) {
                        (Ok(left), Ok(right)) => left == right,
                        _ => false,
                    }
                }
                _ => false,
            };
            if !same_instant {
                return reject(&format!(
                    "ai assessment item {position} published_at does not match the analysed record"
                ));
            }
        }
        let mut item = Map::new();
        item.insert("news_id".into(), Value::String(news_id));
        item.insert("lean".into(), Value::String(lean.into()));
        item.insert(
            "reason".into(),
            reason.map(Value::String).unwrap_or(Value::Null),
        );
        item.insert(
            "evidence_grade".into(),
            Value::String(evidence_grade.into()),
        );
        item.insert(
            "headline".into(),
            record.get("headline").cloned().unwrap_or(Value::Null),
        );
        item.insert(
            "source".into(),
            record.get("source").cloned().unwrap_or(Value::Null),
        );
        item.insert(
            "published_at".into(),
            record
                .get("published_at")
                .filter(|value| !value.is_null())
                .or_else(|| record.get("first_published_at"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        item.insert(
            "url".into(),
            record.get("url").cloned().unwrap_or(Value::Null),
        );
        item.insert(
            "ticker_relevance".into(),
            record
                .get("ticker_relevance")
                .cloned()
                .unwrap_or(Value::Null),
        );
        item.insert(
            "record_kind".into(),
            record.get("record_kind").cloned().unwrap_or(Value::Null),
        );
        item.insert(
            "eligible_evidence".into(),
            record
                .get("eligible_evidence")
                .cloned()
                .unwrap_or(Value::Null),
        );
        item.insert(
            "target_relevance".into(),
            Value::String(target_relevance.into()),
        );
        item.insert("importance".into(), Value::String(importance.into()));
        item.insert(
            "relevance_basis".into(),
            Value::String(relevance_basis.unwrap_or_else(|| "source_recorded".to_string())),
        );
        item.insert("display_only".into(), Value::Bool(true));
        items.push(Value::Object(item));
    }
    let mut result = Map::new();
    result.insert(
        "schema_version".into(),
        Value::String(AI_ASSESSMENT_SCHEMA_VERSION.into()),
    );
    result.insert("basis".into(), Value::String(AI_LEAN_BASIS.into()));
    result.insert(
        "lean_id".into(),
        Value::String(lean_identity(assessment_map)),
    );
    result.insert("model".into(), model.cloned().unwrap_or(Value::Null));
    result.insert("candidate_count".into(), Value::from(known.len()));
    result.insert("items".into(), Value::Array(items));
    Ok(Value::Object(result))
}

fn bucket(items: &[Value], labels: &[&str]) -> Vec<Value> {
    items
        .iter()
        .filter(|item| {
            item.get("lean")
                .and_then(Value::as_str)
                .map(|lean| labels.contains(&lean))
                .unwrap_or(false)
        })
        .cloned()
        .collect()
}

/// Assemble the reportable lean block; never touches any aggregated News field.
pub fn build_ai_lean_block(
    validated: Option<&Value>,
    candidates: &[Value],
    status: &str,
    reason: Option<&str>,
    warnings: Option<&[String]>,
) -> Value {
    let empty = Vec::new();
    let items: &Vec<Value> = validated
        .and_then(|value| value.get("items"))
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let blocks = [
        ("bullish", bucket(items, &["bullish"])),
        ("bearish", bucket(items, &["bearish"])),
        ("mixed", bucket(items, &["mixed"])),
        ("neutral_or_unclear", bucket(items, &["neutral", "unclear"])),
    ];
    let direct: Vec<&Value> = items
        .iter()
        .filter(|item| item.get("target_relevance").and_then(Value::as_str) == Some("direct"))
        .collect();
    let mut direct_counts = Map::new();
    for label in LEAN_LABELS {
        let count = direct
            .iter()
            .filter(|item| item.get("lean").and_then(Value::as_str) == Some(label))
            .count();
        direct_counts.insert(label.to_string(), Value::from(count));
    }
    let major: Vec<Value> = direct
        .iter()
        .filter(|item| {
            item.get("importance").and_then(Value::as_str) == Some("major")
                && matches!(
                    item.get("lean").and_then(Value::as_str),
                    Some("bullish" | "bearish")
                )
                && item.get("url").map(|url| !url.is_null()).unwrap_or(false)
        })
        .map(|item| (*item).clone())
        .collect();
    let mut limitations = vec![
        "Lean is a headline-level AI judgement by the host model, not a calibrated sentiment model and not an event fact.",
        "Several provider feeds supply headlines without body text; a lean cannot verify the underlying report.",
        "Headline judgements and importance are qualitative; repeated coverage may describe the same underlying event.",
    ];
    if items.iter().any(|item| {
        item.get("ticker_relevance").and_then(Value::as_str) == Some("not_explicitly_matched")
    }) {
        limitations.push("Some judged records did not carry explicit ticker evidence in the headline; relevance rests on the provider query symbol and stays unverified.");
    }
    let mut coverage = Map::new();
    coverage.insert("candidate_count".into(), Value::from(candidates.len()));
    coverage.insert("assessed_count".into(), Value::from(items.len()));
    coverage.insert(
        "unassessed_count".into(),
        Value::from(candidates.len().saturating_sub(items.len())),
    );
    let mut label_counts = Map::new();
    for (label, rows) in &blocks {
        label_counts.insert((*label).to_string(), Value::from(rows.len()));
    }
    coverage.insert("label_counts".into(), Value::Object(label_counts));
    coverage.insert("direct_count".into(), Value::from(direct.len()));
    coverage.insert("direct_label_counts".into(), Value::Object(direct_counts));
    let mut block = Map::new();
    block.insert("status".into(), Value::String(status.into()));
    block.insert("basis".into(), Value::String(AI_LEAN_BASIS.into()));
    block.insert("layer".into(), Value::String(AI_LEAN_LAYER.into()));
    block.insert(
        "reason".into(),
        reason
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    block.insert(
        "model".into(),
        validated
            .and_then(|value| value.get("model"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    block.insert(
        "lean_id".into(),
        validated
            .and_then(|value| value.get("lean_id"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    block.insert("display_only".into(), Value::Bool(true));
    block.insert(
        "probability_effect".into(),
        Value::String(
            "none: lean never enters aggregation, confidence, cross-validation or Quant P(up)"
                .into(),
        ),
    );
    block.insert("coverage".into(), Value::Object(coverage));
    block.insert("items".into(), Value::Array(items.clone()));
    block.insert("major_direct_items".into(), Value::Array(major));
    for (label, rows) in &blocks {
        let key = if *label == "neutral_or_unclear" {
            "neutral_or_unclear"
        } else {
            label
        };
        let mut sorted_rows = rows.clone();
        sorted_rows.truncate(10);
        block.insert((*key).to_string(), Value::Array(sorted_rows));
    }
    block.insert(
        "limitations".into(),
        Value::Array(
            limitations
                .into_iter()
                .map(|value| Value::String(value.into()))
                .collect(),
        ),
    );
    if let Some(warnings) = warnings {
        if !warnings.is_empty() {
            block.insert(
                "warnings".into(),
                Value::Array(warnings.iter().cloned().map(Value::String).collect()),
            );
        }
    }
    Value::Object(block)
}

#[allow(clippy::too_many_arguments)] // mirrors the legacy positional row literal
fn candidate_row(
    news_id: Option<&Value>,
    record_kind: &str,
    headline: Option<&Value>,
    summary: Option<&Value>,
    sources: Option<&Value>,
    published_at: Option<&Value>,
    url: Option<&Value>,
    direction: Value,
    event_type: Option<&Value>,
    ticker_relevance: Option<&Value>,
    eligible_evidence: Value,
    exclusion_reasons: Value,
) -> Value {
    let mut row = Map::new();
    row.insert("news_id".into(), news_id.cloned().unwrap_or(Value::Null));
    row.insert("record_kind".into(), Value::String(record_kind.into()));
    row.insert("headline".into(), headline.cloned().unwrap_or(Value::Null));
    row.insert("summary".into(), summary.cloned().unwrap_or(Value::Null));
    row.insert(
        "source".into(),
        sources
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .cloned()
            .unwrap_or(Value::Null),
    );
    row.insert(
        "published_at".into(),
        published_at.cloned().unwrap_or(Value::Null),
    );
    row.insert("url".into(), url.cloned().unwrap_or(Value::Null));
    row.insert("direction".into(), direction);
    row.insert(
        "event_type".into(),
        event_type.cloned().unwrap_or(Value::Null),
    );
    row.insert(
        "ticker_relevance".into(),
        ticker_relevance.cloned().unwrap_or(Value::Null),
    );
    row.insert("eligible_evidence".into(), eligible_evidence);
    row.insert("exclusion_reasons".into(), exclusion_reasons);
    Value::Object(row)
}

/// Every canonical headline, including unclassified and pre-gate records.
pub fn lean_candidates(
    events: &[Value],
    opinions: &[Value],
    unclassified: Option<&[Value]>,
    limit: usize,
) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::new();
    for event in events {
        rows.push(candidate_row(
            event.get("event_id"),
            "event",
            event.get("headline"),
            event.get("summary"),
            event.get("sources"),
            event.get("first_published_at"),
            event.get("url"),
            event.get("direction").cloned().unwrap_or(Value::Null),
            event.get("event_type"),
            event.get("ticker_relevance"),
            event
                .get("eligible_evidence")
                .cloned()
                .unwrap_or(Value::Null),
            event
                .get("exclusion_reasons")
                .cloned()
                .unwrap_or(Value::Null),
        ));
    }
    for opinion in opinions {
        rows.push(candidate_row(
            opinion.get("opinion_id"),
            "opinion",
            opinion.get("headline"),
            opinion.get("summary"),
            opinion.get("sources"),
            opinion.get("published_at"),
            opinion.get("url"),
            opinion.get("sentiment").cloned().unwrap_or(Value::Null),
            None,
            opinion.get("ticker_relevance"),
            opinion
                .get("eligible_evidence")
                .cloned()
                .unwrap_or(Value::Null),
            opinion
                .get("exclusion_reasons")
                .cloned()
                .unwrap_or(Value::Null),
        ));
    }
    for record in unclassified.unwrap_or(&[]) {
        rows.push(candidate_row(
            record.get("news_id"),
            "unclassified",
            record.get("headline"),
            record.get("summary"),
            record.get("sources"),
            record.get("published_at"),
            record.get("url"),
            Value::String("unknown".into()),
            None,
            record.get("ticker_relevance"),
            Value::Bool(false),
            Value::Array(vec![record.get("reason").cloned().unwrap_or(Value::Null)]),
        ));
    }
    rows.retain(|row| {
        row.get("news_id")
            .and_then(Value::as_str)
            .map(|news_id| !news_id.is_empty())
            .unwrap_or(false)
    });
    rows.sort_by(|left, right| {
        let left_key = left
            .get("published_at")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .or_else(|| left.get("first_published_at").and_then(Value::as_str))
            .unwrap_or("");
        let right_key = right
            .get("published_at")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .or_else(|| right.get("first_published_at").and_then(Value::as_str))
            .unwrap_or("");
        right_key.cmp(left_key)
    });
    rows.truncate(limit);
    rows
}

/// Copy lean fields onto matching cards; leave every aggregate field untouched.
pub fn attach_lean_to_records(result: &mut Value, block: &Value) {
    let Some(items) = block.get("items").and_then(Value::as_array) else {
        return;
    };
    let mut by_id: HashMap<String, Value> = HashMap::new();
    for item in items {
        by_id.insert(
            sd_core::contracts::py_str(item.get("news_id").unwrap_or(&Value::Null)),
            item.clone(),
        );
    }
    if by_id.is_empty() {
        return;
    }
    for key in ["major_events", "opinion_records", "background_records"] {
        let Some(records) = result.get_mut(key).and_then(Value::as_array_mut) else {
            continue;
        };
        for record in records {
            let identifier = record
                .get("event_id")
                .filter(|value| !value.is_null())
                .or_else(|| record.get("opinion_id"))
                .cloned()
                .unwrap_or(Value::Null);
            if let Some(item) = by_id.get(&sd_core::contracts::py_str(&identifier)) {
                let Some(record_map) = record.as_object_mut() else {
                    continue;
                };
                record_map.insert(
                    "ai_lean".into(),
                    item.get("lean").cloned().unwrap_or(Value::Null),
                );
                record_map.insert(
                    "ai_lean_reason".into(),
                    item.get("reason").cloned().unwrap_or(Value::Null),
                );
                record_map.insert(
                    "ai_lean_evidence_grade".into(),
                    item.get("evidence_grade").cloned().unwrap_or(Value::Null),
                );
            }
        }
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}
