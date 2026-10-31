//! Conservative deterministic event and opinion screening (`news/classify.py`).
//!
//! The keyword lists are compiled in (so an offline install cannot fail), every artifact
//! records the lexicon version and supported languages, and an unknown language is
//! disclosed instead of being treated as "no event". Negation, hedges and unverified
//! claims stay separate: a denial is a factual statement about a claim, not missing data,
//! and a bare `no` is deliberately not a denial ("No. 1 vendor", "no comment").

use regex::Regex;
use serde_json::Value;

/// Version of the deterministic keyword vocabulary.
pub const LEXICON_VERSION: &str = "news_keyword_en_zh_v1";
/// Languages the deterministic vocabulary covers.
pub const SUPPORTED_LANGUAGES: [&str; 2] = ["en", "zh"];
/// Disclosure that other languages are under-classified.
pub const COVERAGE_NOTE: &str = "deterministic English/Chinese keyword screening; other languages and paraphrases are under-classified and surface as unclassified records";

/// Event categories and their trigger phrases, in match order.
pub const EVENT_PATTERNS: &[(&str, &[&str])] = &[
    (
        "EARNINGS",
        &[
            "earnings",
            "quarterly results",
            "quarter results",
            "eps",
            "revenue beat",
            "revenue miss",
        ],
    ),
    (
        "GUIDANCE",
        &["guidance", "outlook", "forecast raised", "forecast cut"],
    ),
    (
        "ANALYST_REVISION",
        &[
            "analyst upgrade",
            "analyst downgrade",
            "price target",
            "eps estimate",
            "rating raised",
            "rating cut",
        ],
    ),
    (
        "PRODUCT",
        &[
            "product launch",
            "launches",
            "unveils",
            "new product",
            "product release",
        ],
    ),
    (
        "MERGER_ACQUISITION",
        &["acquisition", "acquires", "merger", "to acquire", "buyout"],
    ),
    (
        "BUYBACK",
        &["buyback", "share repurchase", "repurchase program"],
    ),
    ("DIVIDEND", &["dividend", "special distribution"]),
    (
        "CAPITAL_RAISE",
        &[
            "share offering",
            "capital raise",
            "secondary offering",
            "debt offering",
            "convertible notes",
        ],
    ),
    (
        "REGULATORY",
        &[
            "regulator",
            "regulatory",
            "antitrust",
            "ftc",
            "doj probe",
            "sec investigation",
            "license revoked",
        ],
    ),
    (
        "LEGAL",
        &[
            "lawsuit",
            "sued",
            "court ruling",
            "settlement",
            "class action",
            "indictment",
        ],
    ),
    (
        "MANAGEMENT",
        &[
            "ceo resign",
            "ceo retires",
            "appoints ceo",
            "chief executive",
            "management change",
        ],
    ),
    (
        "INSIDER",
        &[
            "insider buying",
            "insider selling",
            "director buys",
            "director sells",
            "filed form 4",
        ],
    ),
    (
        "SUPPLY_CHAIN",
        &[
            "supply chain",
            "component shortage",
            "production disruption",
            "factory shutdown",
            "supplier",
        ],
    ),
    (
        "COMPETITION",
        &[
            "market share",
            "competitor",
            "competitive pressure",
            "price war",
        ],
    ),
    (
        "MACRO",
        &[
            "federal reserve",
            "fed rate",
            "interest rate",
            "inflation",
            "cpi",
            "pce",
            "unemployment",
            "tariff",
            "geopolitical",
            "central bank",
        ],
    ),
];

/// Directional positive phrases.
pub const POSITIVE_TERMS: &[&str] = &[
    "raises guidance",
    "raised guidance",
    "guidance raised",
    "beats estimates",
    "beat estimates",
    "beats revenue",
    "revenue beats",
    "record revenue",
    "record profit",
    "approval granted",
    "wins contract",
    "contract win",
    "strong demand",
    "revenue growth",
    "profit rises",
    "upgrade",
    "dividend increase",
    "no. 1",
    "number one",
    "market leader",
    "top-ranked",
    "positive",
    "bullish",
    "增长",
    "上调",
    "获批",
    "超预期",
    "利好",
    "增长强劲",
];

/// Directional negative phrases.
pub const NEGATIVE_TERMS: &[&str] = &[
    "cuts guidance",
    "cut guidance",
    "guidance cut",
    "misses estimates",
    "missed estimates",
    "revenue decline",
    "profit falls",
    "demand weak",
    "downgrade",
    "investigation",
    "lawsuit",
    "recall",
    "layoff",
    "default",
    "dilution",
    "negative",
    "bearish",
    "下调",
    "不及预期",
    "调查",
    "诉讼",
    "裁员",
    "违约",
    "利空",
    "需求疲软",
];

/// Topic tags reported for matched headlines.
pub const TOPIC_TERMS: &[&str] = &[
    "AI",
    "data center",
    "demand",
    "guidance",
    "earnings",
    "rates",
    "regulation",
    "China",
    "supply",
    "product",
];

fn pattern(source: &str) -> Regex {
    Regex::new(source).expect("compiled-in lexicon regex")
}

fn hedge_pattern() -> Regex {
    pattern(
        r"\b(?:reportedly|allegedly|may|might|could|possibly|speculat(?:e|es|ed|ion)|if)\b|据报道|可能|或将|如果",
    )
}

fn unverified_pattern() -> Regex {
    pattern(r"\b(?:rumou?rs?|rumou?red|unconfirmed)\b|传闻|未经证实")
}

fn negation_pattern() -> Regex {
    pattern(
        r"\b(?:denies|denied|deny|refutes|refuted|rebuts|rebutted|dismisses|dismissed|no longer|does not|doesn't|did not|didn't|will not|won't|is not|isn't|are not|aren't|fails? to|failed to|without|rejects|rejected)\b|否认|驳斥|拒绝|没有|未能|不再",
    )
}

/// Python `str.casefold()` approximation sufficient for the built-in lexicon.
fn casefold(text: &str) -> String {
    text.to_lowercase()
}

/// Phrase containment with Python's ASCII word-boundary rule.
fn contains(text: &str, phrase: &str) -> bool {
    let phrase = casefold(phrase);
    let normalized = casefold(text);
    if phrase
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == ' ')
    {
        // `(?<![a-z0-9])phrase(?![a-z0-9])` without lookaround: scan matching windows.
        let bytes = normalized.as_bytes();
        let needle = phrase.as_bytes();
        if needle.is_empty() || needle.len() > bytes.len() {
            return false;
        }
        let mut start = 0usize;
        while let Some(found) = normalized[start..].find(&phrase) {
            let position = start + found;
            let before_ok = position == 0 || !bytes[position - 1].is_ascii_alphanumeric();
            let after = position + needle.len();
            let after_ok = after >= bytes.len() || !bytes[after].is_ascii_alphanumeric();
            if before_ok && after_ok {
                return true;
            }
            start = position + 1;
            if start > normalized.len() {
                break;
            }
        }
        return false;
    }
    normalized.contains(&phrase)
}

/// Deterministic event type, honouring a source-declared hint when recognised.
pub fn event_type_for(text: &str, hint: Option<&str>) -> String {
    if let Some(hint) = hint {
        let value = hint.trim().to_uppercase();
        if EVENT_PATTERNS.iter().any(|(kind, _)| *kind == value) {
            return value;
        }
        for (kind, _) in EVENT_PATTERNS {
            if value.starts_with(&format!("{kind}_")) {
                return (*kind).to_string();
            }
        }
    }
    let normalized = casefold(text);
    for (kind, phrases) in EVENT_PATTERNS {
        if phrases.iter().any(|phrase| contains(&normalized, phrase)) {
            return (*kind).to_string();
        }
    }
    "UNCLASSIFIED".to_string()
}

/// Directional classification (`positive`, `negative`, `mixed`, `neutral`, `unknown`).
pub fn direction_for(text: &str) -> &'static str {
    let normalized = casefold(text);
    let negated = negation_pattern().is_match(&normalized);
    if hedge_pattern().is_match(&normalized) {
        return "unknown";
    }
    if unverified_pattern().is_match(&normalized) && !negated {
        return "unknown";
    }
    let positive = POSITIVE_TERMS
        .iter()
        .any(|phrase| contains(&normalized, phrase));
    let negative = NEGATIVE_TERMS
        .iter()
        .any(|phrase| contains(&normalized, phrase));
    if negated && (positive || negative) {
        // The scope of a denial is not parsed: "denies the lawsuit" and "denies the
        // upgrade" have opposite polarity, so the honest answer is unknown.
        return "unknown";
    }
    if positive && negative {
        return "mixed";
    }
    if positive {
        return "positive";
    }
    if negative {
        return "negative";
    }
    if negated {
        // An explicit denial with no directional phrase is a factual statement.
        return "neutral";
    }
    "unknown"
}

/// Opinion score in `[-1, 1]`, or `None` when the direction is unknown.
pub fn opinion_score(text: &str) -> Option<f64> {
    if direction_for(text) == "unknown" {
        return None;
    }
    let normalized = casefold(text);
    let positive_hits = POSITIVE_TERMS
        .iter()
        .filter(|phrase| contains(&normalized, phrase))
        .count();
    let negative_hits = NEGATIVE_TERMS
        .iter()
        .filter(|phrase| contains(&normalized, phrase))
        .count();
    let total = positive_hits + negative_hits;
    if total == 0 {
        return None;
    }
    Some((positive_hits as f64 - negative_hits as f64) / total as f64)
}

/// Topic tags matched in the text, in vocabulary order.
pub fn matched_topics(text: &str) -> Vec<String> {
    TOPIC_TERMS
        .iter()
        .filter(|term| contains(text, term))
        .map(|term| (*term).to_string())
        .collect()
}

/// Concatenated title/summary text of a cluster's articles.
pub fn event_text(cluster: &Value) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(articles) = cluster.get("articles").and_then(Value::as_array) {
        for article in articles {
            for key in ["title", "summary"] {
                if let Some(value) = article.get(key).and_then(Value::as_str) {
                    if !value.is_empty() {
                        parts.push(value.to_string());
                    }
                }
            }
        }
    }
    parts.join(" ")
}
