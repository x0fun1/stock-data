//! Deterministic impact magnitude, pattern comparison, breadth scope and source tiering
//! (`news/impact.py`).
//!
//! The upstream composite Impact Score is deliberately not reproduced: it is an
//! uncalibrated pseudo-precise number that this runtime's probability and confidence
//! contracts forbid. Every field here is a disclosed heuristic band, never a calibrated
//! expectation, and nothing reads Quant output or emits a probability.

use serde_json::{Map, Value};

/// Single-instrument absolute-return bands, strongest first.
pub const MAGNITUDE_BANDS: &[(&str, f64)] = &[
    ("severe", 0.10),
    ("major", 0.05),
    ("moderate", 0.02),
    ("minor", 0.005),
];
/// Ratio of |event-session return| to own trailing sigma.
pub const PATTERN_BANDS: &[(&str, f64)] = &[("amplified", 1.0), ("consistent", 0.5)];
/// Publisher tier explanations.
pub const SOURCE_TIERS: &[(&str, &str)] = &[
    (
        "tier1_official_primary",
        "Official primary record: regulator, central bank, exchange or issuer filing.",
    ),
    (
        "tier2_major_financial_news",
        "Major financial newsroom or wire service.",
    ),
    (
        "tier3_specialized_real_time",
        "Specialized or real-time market coverage.",
    ),
    (
        "tier4_analysis_and_aggregator",
        "Analysis, commentary, or an aggregator relaying another publisher.",
    ),
    (
        "unclassified_publisher",
        "Publisher identity is absent or unrecognized; tier not asserted.",
    ),
];
/// Exact-alias matching rule version.
pub const SOURCE_TIER_RULE_VERSION: &str = "publisher_tier_exact_alias_v1";
/// Comparison slack for band edges: 90/100-1 differs from 110/100-1 in the last bits.
pub const BAND_TOLERANCE: f64 = 1e-9;
/// Below this daily sigma a volatility ratio is not a usable baseline (1bp/session).
pub const MINIMUM_USABLE_SIGMA: f64 = 1e-4;

const TIER1_ALIASES: &[&str] = &[
    "federal reserve",
    "federal reserve board",
    "federalreserve.gov",
    "u.s. federal reserve",
    "sec",
    "sec.gov",
    "u.s. securities and exchange commission",
    "edgar",
    "sec edgar",
    "bls",
    "u.s. bureau of labor statistics",
    "treasury",
    "treasury.gov",
    "u.s. department of the treasury",
    "cftc",
    "federal trade commission",
    "ftc",
    "european central bank",
    "ecb",
    "bank of japan",
    "boj",
    "people's bank of china",
    "pboc",
    "company press release",
    "company filing",
    "press release",
    "investor relations",
];
const TIER2_ALIASES: &[&str] = &[
    "reuters",
    "bloomberg",
    "bloomberg news",
    "bloomberg.com",
    "wall street journal",
    "wsj",
    "financial times",
    "associated press",
    "ap",
    "ap news",
    "apnews",
    "apnews.com",
    "cnbc",
    "barron's",
    "barrons",
    "marketwatch",
    "marketwatch.com",
    "the economist",
];
const TIER3_ALIASES: &[&str] = &[
    "seeking alpha",
    "seekingalpha.com",
    "zacks",
    "investing.com",
    "stocktwits",
    "benzinga",
    "chartmill",
    "tipranks",
    "morningstar",
    "quartz",
    "fortune",
    "forbes",
];
const TIER4_ALIASES: &[&str] = &[
    "yahoo finance",
    "yahoo",
    "finance.yahoo.com",
    "msn",
    "aol",
    "newsfile",
    "globenewswire",
    "business wire",
    "pr newswire",
    "accesswire",
    "ein presswire",
    "simply wall st",
    "yahoo news",
];
const TIER_ORDER: &[&str] = &[
    "tier1_official_primary",
    "tier2_major_financial_news",
    "tier3_specialized_real_time",
    "tier4_analysis_and_aggregator",
    "unclassified_publisher",
];

/// Attribution caveats reported with every impact bundle.
pub const ATTRIBUTION_CAVEATS: &[&str] = &[
    "Daily-bar association is temporal only; it does not establish that the headline caused the move.",
    "Absence of magnitude is not evidence that the event was unimportant; it may be priced in, offset, or observed on the wrong session.",
    "Conflicting same-session events cannot be separated in a single close-to-close return; do not attribute the whole move to one headline.",
    "Recency is not importance; a low-magnitude event can still matter for a longer horizon than the observed session.",
    "Multiple concurrent events are not separable here; treat any single-event attribution as one candidate explanation.",
    "Publisher tier reflects an editorial label, not authentication, independence or factual accuracy of a specific claim.",
];

fn python_round(value: f64, digits: u32) -> f64 {
    let factor = 10f64.powi(digits as i32);
    (value * factor).round() / factor
}

fn tier_rank(tier: &str) -> usize {
    TIER_ORDER
        .iter()
        .position(|candidate| *candidate == tier)
        .unwrap_or(TIER_ORDER.len())
}

/// Python `str.casefold()` approximation sufficient for the curated aliases.
fn casefold(text: &str) -> String {
    text.to_lowercase()
}

fn normalize_publisher(name: &str) -> String {
    let collapsed = name.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed
        .trim()
        .trim_end_matches([' ', '.', '·', '—', '–', '-'])
        .trim_start_matches([' ', '.', '·', '—', '–', '-'])
        .to_string()
        .pipe_casefold()
}

trait PipeCasefold {
    fn pipe_casefold(self) -> String;
}

impl PipeCasefold for String {
    fn pipe_casefold(self) -> String {
        casefold(&self)
    }
}

fn registry() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        ("tier1_official_primary", TIER1_ALIASES),
        ("tier2_major_financial_news", TIER2_ALIASES),
        ("tier3_specialized_real_time", TIER3_ALIASES),
        ("tier4_analysis_and_aggregator", TIER4_ALIASES),
    ]
}

fn lookup_alias(name: &str) -> Option<&'static str> {
    let stripped = name
        .trim_end_matches(".com")
        .trim_end_matches(".org")
        .trim_end_matches(".net");
    let mut candidates: Vec<String> = Vec::new();
    for raw in [name, stripped] {
        let folded = raw.trim();
        let folded = folded.strip_prefix("the ").unwrap_or(folded);
        if !folded.is_empty() {
            candidates.push(folded.to_string());
        }
    }
    for candidate in candidates {
        for (tier, aliases) in registry() {
            if aliases.contains(&candidate.as_str()) {
                return Some(tier);
            }
        }
    }
    None
}

fn classify_alias(name: &str) -> &'static str {
    lookup_alias(&normalize_publisher(name)).unwrap_or("unclassified_publisher")
}

fn classify_publisher(name: &str) -> &'static str {
    // Classify original publishers, never upgrade them using a named relay.
    let mut normalized = normalize_publisher(name);
    let mut explicit_roles = false;
    if let Some((before, relation, after)) = find_syndication_credit(&normalized) {
        explicit_roles = true;
        normalized = if relation == "via" {
            before.to_string()
        } else {
            after.to_string()
        };
    }
    let parts: Vec<String> = split_credit(&normalized)
        .into_iter()
        .map(|part| part.trim().to_string())
        .filter(|part| !part.is_empty())
        .collect();
    let tiers: Vec<&'static str> = parts.iter().map(|part| classify_alias(part)).collect();
    if tiers.is_empty() {
        return "unclassified_publisher";
    }
    if !explicit_roles
        && tiers.len() > 1
        && tiers[1..]
            .iter()
            .all(|tier| *tier == "tier4_analysis_and_aggregator")
    {
        // Retain documented "Reuters - Yahoo Finance" original/relay credits.
        return tiers[0];
    }
    tiers
        .into_iter()
        .max_by_key(|tier| tier_rank(tier))
        .unwrap_or("unclassified_publisher")
}

fn find_syndication_credit(text: &str) -> Option<(&str, &str, &str)> {
    for relation in ["via", "from"] {
        if let Some(position) = text.find(&format!(" {relation} ")) {
            let before = &text[..position];
            let after = &text[position + relation.len() + 2..];
            if !before.is_empty() && !after.is_empty() {
                return Some((before, relation, after));
            }
        }
    }
    None
}

fn split_credit(text: &str) -> Vec<&str> {
    // `\s+[-–—|:]\s+`
    let mut parts: Vec<&str> = Vec::new();
    let bytes = text.as_bytes();
    let mut start = 0usize;
    let mut index = 0usize;
    while index < text.len() {
        let character = text[index..].chars().next().unwrap_or(' ');
        if character.is_whitespace() {
            // Look ahead for separator punctuation surrounded by whitespace.
            let mut cursor = index;
            while cursor < text.len()
                && text[cursor..]
                    .chars()
                    .next()
                    .map(char::is_whitespace)
                    .unwrap_or(false)
            {
                cursor += text[cursor..]
                    .chars()
                    .next()
                    .map(char::len_utf8)
                    .unwrap_or(1);
            }
            if cursor < text.len() {
                let separator = text[cursor..].chars().next().unwrap_or(' ');
                if matches!(separator, '-' | '–' | '—' | '|' | ':') {
                    let after_separator = cursor + separator.len_utf8();
                    if after_separator < text.len()
                        && text[after_separator..]
                            .chars()
                            .next()
                            .map(char::is_whitespace)
                            .unwrap_or(false)
                    {
                        parts.push(&text[start..index]);
                        let mut next_start = after_separator;
                        while next_start < text.len()
                            && text[next_start..]
                                .chars()
                                .next()
                                .map(char::is_whitespace)
                                .unwrap_or(false)
                        {
                            next_start += text[next_start..]
                                .chars()
                                .next()
                                .map(char::len_utf8)
                                .unwrap_or(1);
                        }
                        start = next_start;
                        index = next_start;
                        continue;
                    }
                }
            }
        }
        index = bytes.len().min(index + character.len_utf8());
    }
    parts.push(&text[start..]);
    parts
}

/// Grade the absolute event-session move into a disclosed band.
pub fn magnitude_band(observed_return: Option<f64>, reaction_status: Option<&str>) -> Value {
    let Some(observed) = observed_return.filter(|value| value.is_finite()) else {
        let mut map = Map::new();
        map.insert("band".into(), Value::String("not_assessed".into()));
        map.insert("observed_abs_return".into(), Value::Null);
        map.insert(
            "thresholds".into(),
            Value::String("single_instrument_absolute_return_bands".into()),
        );
        map.insert(
            "warning".into(),
            Value::String(
                "no mature frozen session follows this event; magnitude is not assessed".into(),
            ),
        );
        return Value::Object(map);
    };
    let magnitude = observed.abs();
    let withheld = match reaction_status {
        Some("intraday_daily_association") => Some("whole-session return includes pre-publication trading; the move is not attributable to the event window"),
        Some("not_classifiable") => Some("reaction direction is not classifiable; the impact band is not asserted"),
        _ => None,
    };
    if let Some(withheld) = withheld {
        let mut map = Map::new();
        map.insert("band".into(), Value::String("not_assessed".into()));
        map.insert(
            "observed_abs_return".into(),
            Value::from(python_round(magnitude, 6)),
        );
        map.insert(
            "thresholds".into(),
            Value::String("single_instrument_absolute_return_bands".into()),
        );
        map.insert(
            "reaction_status".into(),
            Value::String(reaction_status.unwrap_or("").into()),
        );
        map.insert("warning".into(), Value::String(withheld.into()));
        map.insert(
            "basis".into(),
            Value::String(
                "frozen_daily_close_to_close_return_only; not an index, sector or intraday move"
                    .into(),
            ),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        return Value::Object(map);
    }
    let band = MAGNITUDE_BANDS
        .iter()
        .find(|(_, threshold)| magnitude + BAND_TOLERANCE >= *threshold)
        .map(|(name, _)| *name)
        .unwrap_or("negligible");
    let mut map = Map::new();
    map.insert("band".into(), Value::String(band.into()));
    map.insert(
        "observed_abs_return".into(),
        Value::from(python_round(magnitude, 6)),
    );
    map.insert(
        "thresholds".into(),
        Value::String("single_instrument_absolute_return_bands".into()),
    );
    map.insert("band_tolerance".into(), Value::from(BAND_TOLERANCE));
    map.insert(
        "basis".into(),
        Value::String(
            "frozen_daily_close_to_close_return_only; not an index, sector or intraday move".into(),
        ),
    );
    map.insert(
        "calibration_status".into(),
        Value::String("heuristic_uncalibrated".into()),
    );
    Value::Object(map)
}

/// Public alias for the shared volatility baseline.
pub fn trailing_sigma(closes: &[f64], index: usize, window: usize) -> (Option<f64>, i64) {
    crate::timing::trailing_sigma(closes, index, window)
}

/// Compare the observed move to this symbol's own recent volatility.
pub fn pattern_comparison(reaction: &Value, closes: &[f64], session_index: Option<i64>) -> Value {
    let status = reaction.get("status").and_then(Value::as_str).unwrap_or("");
    if ![
        "confirmed_positive",
        "confirmed_negative",
        "event_price_divergence_positive",
        "event_price_divergence_negative",
        "intraday_daily_association",
    ]
    .contains(&status)
    {
        let mut map = Map::new();
        map.insert("pattern".into(), Value::String("not_assessed".into()));
        map.insert(
            "reason".into(),
            Value::String("no usable frozen market reaction".into()),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        return Value::Object(map);
    }
    let Some(index) = session_index.filter(|index| *index > 0) else {
        let mut map = Map::new();
        map.insert("pattern".into(), Value::String("not_assessed".into()));
        map.insert(
            "reason".into(),
            Value::String("event session index unavailable in frozen bars".into()),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        return Value::Object(map);
    };
    if closes.is_empty() || index > closes.len() as i64 - 1 {
        let mut map = Map::new();
        map.insert("pattern".into(), Value::String("not_assessed".into()));
        map.insert(
            "reason".into(),
            Value::String("event session index unavailable in frozen bars".into()),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        return Value::Object(map);
    }
    let (sigma, observations) = trailing_sigma(closes, index as usize, 20);
    let observed = reaction.get("daily_close_return").and_then(Value::as_f64);
    let usable = sigma
        .filter(|sigma| *sigma >= MINIMUM_USABLE_SIGMA)
        .is_some()
        && observed.filter(|value| value.is_finite()).is_some();
    if !usable {
        let mut map = Map::new();
        map.insert("pattern".into(), Value::String("not_assessed".into()));
        map.insert(
            "reason".into(),
            Value::String(
                "insufficient trailing returns or a degenerate volatility baseline for a volatility ratio"
                    .into(),
            ),
        );
        map.insert(
            "trailing_sigma_observations".into(),
            Value::from(observations),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        return Value::Object(map);
    }
    let sigma = sigma.unwrap_or(0.0);
    let observed = observed.unwrap_or(0.0);
    let ratio = observed.abs() / sigma;
    let (pattern, reason) = if matches!(
        status,
        "event_price_divergence_positive" | "event_price_divergence_negative"
    ) {
        (
            "inverse",
            "observed price direction opposes the event direction",
        )
    } else if status == "intraday_daily_association" {
        (
            "not_assessed",
            "whole-session window includes pre-publication trading",
        )
    } else {
        (
            PATTERN_BANDS
                .iter()
                .find(|(_, threshold)| ratio >= *threshold)
                .map(|(name, _)| *name)
                .unwrap_or("dampened"),
            "observed move relative to this symbol's own trailing return sigma",
        )
    };
    let mut map = Map::new();
    map.insert("pattern".into(), Value::String(pattern.into()));
    map.insert("reason".into(), Value::String(reason.into()));
    map.insert(
        "observed_abs_return".into(),
        Value::from(python_round(observed.abs(), 6)),
    );
    map.insert("trailing_sigma".into(), Value::from(python_round(sigma, 6)));
    map.insert(
        "trailing_sigma_observations".into(),
        Value::from(observations),
    );
    map.insert(
        "ratio_to_trailing_sigma".into(),
        Value::from(python_round(ratio, 4)),
    );
    map.insert(
        "baseline".into(),
        Value::String("own_trailing_return_sigma_up_to_20_sessions".into()),
    );
    map.insert(
        "calibration_status".into(),
        Value::String("heuristic_uncalibrated".into()),
    );
    Value::Object(map)
}

/// Declare the event's potential transmission scope (never an observed measurement).
pub fn breadth_scope(event_type: Option<&str>) -> Value {
    let kind = event_type.unwrap_or("").to_uppercase();
    let (scope, note) = match kind.as_str() {
        "MACRO" => (
            "cross_asset_candidate",
            "macro or policy event may transmit beyond this ticker; not measured here",
        ),
        "EARNINGS" | "GUIDANCE" | "ANALYST_REVISION" => (
            "company_and_sector_candidate",
            "earnings, guidance or revision may transmit to a sector; not measured here",
        ),
        "SUPPLY_CHAIN" | "COMPETITION" | "REGULATORY" => (
            "sector_candidate",
            "industry or regulatory transmission is possible; not measured here",
        ),
        _ => (
            "company_specific_candidate",
            "no declared transmission beyond the analysed ticker",
        ),
    };
    let mut map = Map::new();
    map.insert("declared_scope".into(), Value::String(scope.into()));
    map.insert("note".into(), Value::String(note.into()));
    map.insert(
        "observed_breadth".into(),
        Value::String("not_assessed".into()),
    );
    map.insert(
        "observation_limit".into(),
        Value::String("single_frozen_ticker_ohlcv_only".into()),
    );
    Value::Object(map)
}

fn tier_meaning(tier: &str) -> String {
    SOURCE_TIERS
        .iter()
        .find(|(name, _)| *name == tier)
        .map(|(_, meaning)| (*meaning).to_string())
        .unwrap_or_default()
}

/// Assign the weakest tier present across a cluster's publishers.
pub fn source_tier(publishers: &[String]) -> Value {
    let mut names: Vec<String> = publishers
        .iter()
        .map(|name| name.trim().to_string())
        .filter(|name| !name.is_empty())
        .collect();
    names.sort();
    names.dedup();
    if names.is_empty() {
        let mut map = Map::new();
        map.insert(
            "tier".into(),
            Value::String("unclassified_publisher".into()),
        );
        map.insert(
            "tier_meaning".into(),
            Value::String(tier_meaning("unclassified_publisher")),
        );
        map.insert("publishers".into(), Value::Array(Vec::new()));
        map.insert("per_publisher".into(), Value::Object(Map::new()));
        map.insert(
            "selection_rule".into(),
            Value::String("weakest_publisher_in_cluster".into()),
        );
        map.insert(
            "match_rule".into(),
            Value::String("exact_normalized_alias_only".into()),
        );
        map.insert(
            "calibration_status".into(),
            Value::String("heuristic_uncalibrated".into()),
        );
        map.insert(
            "rule_version".into(),
            Value::String(SOURCE_TIER_RULE_VERSION.into()),
        );
        map.insert("unrecognized_ratio".into(), Value::Null);
        return Value::Object(map);
    }
    let mut per_publisher = Map::new();
    let mut weakest = "tier1_official_primary";
    let mut unrecognized = 0i64;
    for name in &names {
        let tier = classify_publisher(name);
        if tier == "unclassified_publisher" {
            unrecognized += 1;
        }
        if tier_rank(tier) > tier_rank(weakest) {
            weakest = tier;
        }
        per_publisher.insert(name.clone(), Value::String(tier.into()));
    }
    let mut map = Map::new();
    map.insert("tier".into(), Value::String(weakest.into()));
    map.insert("tier_meaning".into(), Value::String(tier_meaning(weakest)));
    map.insert(
        "publishers".into(),
        Value::Array(names.iter().cloned().map(Value::String).collect()),
    );
    map.insert("per_publisher".into(), Value::Object(per_publisher));
    map.insert(
        "selection_rule".into(),
        Value::String("weakest_publisher_in_cluster".into()),
    );
    map.insert(
        "match_rule".into(),
        Value::String("exact_normalized_alias_only".into()),
    );
    map.insert(
        "calibration_status".into(),
        Value::String("heuristic_uncalibrated".into()),
    );
    map.insert(
        "rule_version".into(),
        Value::String(SOURCE_TIER_RULE_VERSION.into()),
    );
    map.insert(
        "unrecognized_ratio".into(),
        Value::from(python_round(unrecognized as f64 / names.len() as f64, 4)),
    );
    Value::Object(map)
}

/// Bundle every deterministic News-side impact judgement for one event card.
pub fn assess(
    event_type: Option<&str>,
    reaction: &Value,
    publishers: &[String],
    closes: &[f64],
    session_index: Option<i64>,
) -> Value {
    let daily = reaction.get("daily_close_return").and_then(Value::as_f64);
    let status = reaction.get("status").and_then(Value::as_str);
    let mut map = Map::new();
    map.insert("magnitude".into(), magnitude_band(daily, status));
    map.insert(
        "pattern_comparison".into(),
        pattern_comparison(reaction, closes, session_index),
    );
    map.insert("breadth_scope".into(), breadth_scope(event_type));
    map.insert("source_tier".into(), source_tier(publishers));
    map.insert(
        "attribution_caveats".into(),
        Value::Array(
            ATTRIBUTION_CAVEATS
                .iter()
                .map(|caveat| Value::String((*caveat).into()))
                .collect(),
        ),
    );
    map.insert(
        "composite_impact_score".into(),
        Value::String("not_implemented".into()),
    );
    map.insert(
        "composite_impact_score_reason".into(),
        Value::String(
            "an uncalibrated pseudo-precise score is not produced by this runtime".into(),
        ),
    );
    Value::Object(map)
}
