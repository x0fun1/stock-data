//! Market-data integrity checks and the prediction/reporting gate.
//!
//! Port of `quant_research/data_checks.py`. The checks are kept separate from
//! forecast eligibility: data quality and strategy evidence may lower the reporting
//! state, but only explicit fatal integrity codes veto a probability.
//!
//! Ordering matters: `reason_codes` are appended in check order and the gate
//! classification reads them positionally, so the sub-checks are called in the same
//! order as the Python implementation.

use chrono::{DateTime, Datelike, NaiveDate, Utc};
use chrono_tz::Tz;
use serde_json::{Map, Value};

use crate::calendar::CalendarResource;
use crate::contracts::parse_timestamp;
use crate::error::{SdError, SdResult};
use crate::gate_policy::classify_reasons;
use crate::interfaces::CalendarSession;
use crate::policy;

/// Adjustment conventions the runtime recognises.
pub const ADJUSTMENTS: [&str; 4] = ["adjusted", "unadjusted", "split_adjusted", "total_return"];
/// The collector's spelling for "provider series is split-adjusted, ex-dividend".
pub const ADJUSTMENT_ALIASES: [(&str, &str); 1] = [("provider", "split_adjusted")];
/// Minimum input for the predeclared 20-observation baseline.
pub const MIN_OBSERVATION_BARS: usize = 21;
/// Cross-source close tolerance (0.3%).
pub const PRICE_MATCH_TOLERANCE: f64 = 0.003;
/// A daily bar's source timestamp may label the period start, accepted within one day.
const DAILY_BAR_COVERAGE_SECONDS: i64 = 86_400;

/// Everything one market check reports; codes are appended in check order.
#[derive(Debug, Clone, Default)]
pub struct Findings {
    pub errors: Vec<String>,
    pub blockers: Vec<String>,
    pub warnings: Vec<String>,
    pub reason_codes: Vec<String>,
}

impl Findings {
    /// A fatal integrity problem: adds a code and a blocker message.
    pub fn fatal(&mut self, code: &str, message: &str) {
        self.reason_codes.push(code.to_string());
        self.blockers.push(message.to_string());
    }

    /// A degraded state: adds a code and a warning message.
    pub fn degraded(&mut self, code: &str, message: &str) {
        self.reason_codes.push(code.to_string());
        self.warnings.push(message.to_string());
    }
}

/// Options for [`check_market`].
pub struct CheckOptions<'a> {
    /// Collection capture timestamp (`captured_at_utc`), required.
    pub captured_at: Option<&'a str>,
    /// Library schedule persisted in a manifest (reload path).
    pub resolved_calendar_evidence: Option<&'a CalendarResource>,
    /// A freeze-time schedule built from a verified calendar resource.
    pub library_calendar: Option<&'a CalendarResource>,
    /// Whether the freeze path may use the verified resource (mirrors `allow_library_calendar`).
    pub allow_library_calendar: bool,
    /// Legacy snapshots skip the fixed-policy checks.
    pub enforce_policy: bool,
}

impl Default for CheckOptions<'_> {
    fn default() -> Self {
        Self {
            captured_at: None,
            resolved_calendar_evidence: None,
            library_calendar: None,
            allow_library_calendar: false,
            enforce_policy: true,
        }
    }
}

fn text(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str)
}

fn object(value: Option<&Value>) -> Option<&Map<String, Value>> {
    value.and_then(Value::as_object)
}

fn array(value: Option<&Value>) -> &[Value] {
    value
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(number)) => number.as_f64(),
        _ => None,
    }
}

fn date_only(value: &Value) -> SdResult<NaiveDate> {
    let text = crate::contracts::py_str(value);
    let trimmed = text.trim();
    if let Some(position) = trimmed.find('T') {
        return Ok(parse_timestamp(&Value::String(trimmed[..position].to_string()))?.date_naive());
    }
    policy::parse_iso_date(trimmed.get(..10).unwrap_or(trimmed))
}

fn market_zone(market: &str) -> SdResult<Tz> {
    policy::exchange_timezone(market)
        .and_then(|zone| zone.parse().ok())
        .ok_or_else(|| SdError::validation(format!("unsupported market {market}")))
}

fn bar_close(bar: &Value) -> Option<f64> {
    number(bar.get("close"))
}

/// `_check_price_observations`: compare optional same-session closes with the last bar.
pub fn check_price_observations(
    block: &Value,
    bars: &[Value],
    last: &str,
) -> (String, Vec<String>, Vec<String>, Vec<String>) {
    let mut codes: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    let overall = object(Some(block));
    let mut observations: &[Value] =
        array(overall.and_then(|value| value.get("latest_price_observations")));
    if overall.and_then(|value| value.get("latest_price_observations")) == Some(&Value::Null) {
        observations = &[];
    }
    if let Some(value) = overall.and_then(|value| value.get("latest_price_observations")) {
        if !value.is_array() && !value.is_null() {
            observations = &[];
            codes.push("LATEST_CLOSE_SINGLE_PROVIDER".into());
            warnings.push(
                "Additional latest-price observations were malformed and not used for cross-source verification.".into(),
            );
        }
    }

    fn price_basis(row: &Map<String, Value>) -> Option<String> {
        let adjustment = row.get("adjustment");
        let basis = row.get("price_basis").or(adjustment);
        let text = basis.and_then(Value::as_str)?;
        if !ADJUSTMENTS.contains(&text) {
            return None;
        }
        if row.contains_key("adjustment") && adjustment != Some(&Value::String(text.to_string())) {
            return None;
        }
        Some(text.to_string())
    }

    let base_basis = overall
        .and_then(price_basis)
        .or_else(|| {
            overall
                .and_then(|value| text(value.get("adjustment")))
                .map(str::to_string)
        })
        .unwrap_or_else(|| "unspecified".to_string());
    let mut accepted: Vec<(String, String, f64, String, String)> = Vec::new();
    let base_source = overall
        .and_then(|value| text(value.get("actual_source")))
        .unwrap_or_default();
    if !base_source.is_empty() {
        accepted.push((
            base_source.to_string(),
            last.to_string(),
            bars.last().and_then(bar_close).unwrap_or(0.0),
            overall
                .and_then(|value| text(value.get("currency")))
                .unwrap_or_default()
                .to_string(),
            base_basis.clone(),
        ));
    }
    let base_currency = overall
        .and_then(|value| text(value.get("currency")))
        .unwrap_or_default()
        .to_string();
    for item in observations {
        let Some(row) = item.as_object() else {
            continue;
        };
        let kind = text(row.get("kind")).unwrap_or("daily_close");
        if !matches!(kind, "daily_close" | "close") {
            continue;
        }
        let source = text(row.get("source")).unwrap_or_default();
        let session_date = row
            .get("session_date")
            .and_then(|value| date_only(value).ok())
            .map(policy::iso_date);
        let price = number(row.get("price"));
        let currency = text(row.get("currency")).unwrap_or_default();
        let (Some(session_date), Some(price)) = (session_date, price) else {
            continue;
        };
        if source.is_empty() || !price.is_finite() || price <= 0.0 || currency.is_empty() {
            continue;
        }
        accepted.push((
            source.to_string(),
            session_date,
            price,
            currency.to_string(),
            price_basis(row).unwrap_or_else(|| "unspecified".to_string()),
        ));
    }
    let same_session_currency: Vec<&(String, String, f64, String, String)> = accepted
        .iter()
        .filter(|row| row.1 == last && row.3 == base_currency)
        .collect();
    let same_session: Vec<&&(String, String, f64, String, String)> = same_session_currency
        .iter()
        .filter(|row| ADJUSTMENTS.contains(&base_basis.as_str()) && row.4 == base_basis)
        .collect();
    if !observations.is_empty() && same_session.len() < same_session_currency.len() {
        warnings.push("Same-session price observations with unknown or incompatible adjustment/price_basis were retained but not used for cross-source verification.".into());
    }
    let mut distinct: Vec<&str> = Vec::new();
    for row in &same_session {
        if !distinct.contains(&row.0.as_str()) {
            distinct.push(&row.0);
        }
    }
    for (index, left) in same_session.iter().enumerate() {
        for right in same_session.iter().skip(index + 1) {
            if left.0 == right.0 {
                continue;
            }
            let scale = left.2.min(right.2);
            if scale > 0.0 && (left.2 - right.2).abs() / scale > PRICE_MATCH_TOLERANCE {
                codes.push("LATEST_PRICE_CONFLICT".into());
                errors.push(format!(
                    "latest same-session closes from {} and {} differ by more than 0.3%",
                    left.0, right.0
                ));
            }
        }
    }
    if distinct.len() >= 2 && !codes.iter().any(|code| code == "LATEST_PRICE_CONFLICT") {
        codes.push("LATEST_CLOSE_CROSS_VERIFIED".into());
        return ("cross_source_verified".to_string(), codes, warnings, errors);
    }
    if distinct.len() < 2 {
        codes.push("LATEST_CLOSE_SINGLE_PROVIDER".into());
        warnings.push("Latest close has one usable provider; source-level timestamp checks passed, but cross-source matching is unavailable.".into());
    }
    ("provider_verified".to_string(), codes, warnings, errors)
}

/// Probe used by the migrated F22 check: run only the latest-close classification.
///
/// The gate calls [`check_latest_close`] internally; exposing it keeps the legacy
/// check testing the real implementation instead of a copy of the rule.
#[allow(clippy::too_many_arguments)] // mirrors the legacy positional signature
pub fn latest_close_probe(
    block: &Value,
    bars: &[Value],
    sessions: &[CalendarSession],
    last: &str,
    asof: DateTime<Utc>,
    calendar_status: &str,
    source_time: Option<DateTime<Utc>>,
    fetch_time: Option<DateTime<Utc>>,
) -> (Findings, Value) {
    let mut findings = Findings::default();
    let block_object = object(Some(block)).cloned().unwrap_or_default();
    let completion_verified =
        !sessions.is_empty() && sessions.iter().all(|session| session.complete_at.is_some());
    let latest = check_latest_close(
        &block_object,
        block,
        bars,
        sessions,
        last,
        asof,
        calendar_status,
        completion_verified,
        source_time,
        fetch_time,
        &mut findings,
    );
    (findings, latest)
}

/// Probe used by the migrated T40 check: run only the adjustment reconciliation.
pub fn adjustment_probe(
    block: &Value,
    first: &str,
    last: &str,
    bars: &[Value],
) -> (Findings, String, bool, Vec<Value>) {
    let mut findings = Findings::default();
    let block_object = object(Some(block)).cloned().unwrap_or_default();
    let (status, known, reconciliation) =
        check_adjustment(&block_object, first, last, bars, &mut findings);
    (findings, status, known, reconciliation)
}

/// Assemble the gate block (`_gate`), with the legacy key order.
fn gate(
    findings: &Findings,
    latest_close: Option<Value>,
    calendar_status: &str,
    horizon_type: &str,
    resolved_calendar_evidence: Option<&CalendarResource>,
) -> Value {
    let classification = classify_reasons(findings.reason_codes.clone());
    let fatal = classification.blockers.clone();
    let warning_codes = classification.warnings.clone();
    let mut map = Map::new();
    map.insert(
        "forecast_eligible".into(),
        Value::Bool(classification.prediction_eligible),
    );
    let mut eligibility = Map::new();
    eligibility.insert(
        "status".into(),
        Value::String(
            if fatal.is_empty() {
                "eligible"
            } else {
                "blocked"
            }
            .into(),
        ),
    );
    eligibility.insert(
        "blockers".into(),
        Value::Array(
            fatal
                .iter()
                .map(|code| Value::String(code.clone()))
                .collect(),
        ),
    );
    map.insert("prediction_eligibility".into(), Value::Object(eligibility));
    let mut reporting = Map::new();
    reporting.insert("status".into(), Value::String(classification.status.into()));
    reporting.insert(
        "warnings".into(),
        Value::Array(
            warning_codes
                .iter()
                .map(|code| Value::String(code.clone()))
                .collect(),
        ),
    );
    reporting.insert(
        "informational".into(),
        Value::Array(
            classification
                .informational
                .iter()
                .map(|code| Value::String(code.clone()))
                .collect(),
        ),
    );
    map.insert("reporting_eligibility".into(), Value::Object(reporting));
    map.insert(
        "reporting_status".into(),
        Value::String(classification.status.into()),
    );
    map.insert("errors".into(), strings(&findings.errors));
    map.insert("blockers".into(), strings(&findings.blockers));
    map.insert("warnings".into(), strings(&findings.warnings));
    map.insert("reason_codes".into(), strings(&findings.reason_codes));
    map.insert(
        "latest_confirmed_close".into(),
        latest_close.clone().unwrap_or(Value::Null),
    );
    map.insert(
        "latest_close_status".into(),
        Value::String(match &latest_close {
            Some(value) => value
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("unavailable")
                .to_string(),
            None => "unavailable".to_string(),
        }),
    );
    map.insert(
        "calendar_verification".into(),
        Value::String(calendar_status.into()),
    );
    map.insert(
        "resolved_calendar_evidence".into(),
        resolved_calendar_evidence
            .map(calendar_resource_value)
            .unwrap_or(Value::Null),
    );
    map.insert("horizon_type".into(), Value::String(horizon_type.into()));
    map.insert(
        "horizon_confidence".into(),
        Value::String(
            if horizon_type == "exchange_sessions" {
                "high"
            } else {
                "low"
            }
            .into(),
        ),
    );
    map.insert(
        "source_authentication".into(),
        Value::String("not_independently_verified".into()),
    );
    map.insert(
        "session_horizon_verified".into(),
        Value::Bool(horizon_type == "exchange_sessions"),
    );
    Value::Object(map)
}

fn strings(values: &[String]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|value| Value::String(value.clone()))
            .collect(),
    )
}

fn calendar_resource_value(resource: &CalendarResource) -> Value {
    let mut map = Map::new();
    map.insert("source".into(), Value::String(resource.source.clone()));
    map.insert("timezone".into(), Value::String(resource.timezone.clone()));
    map.insert(
        "coverage_start".into(),
        Value::String(resource.coverage_start.clone()),
    );
    map.insert(
        "coverage_end".into(),
        Value::String(resource.coverage_end.clone()),
    );
    // A-CAL-01: completion semantics travel with the evidence where they can differ
    // from the continuous close (a market with a closing-auction regime). Markets under
    // the sourced no-extension rule (US) need no extra metadata: re-deriving
    // `complete_at = close_at` from that documented rule keeps historical US evidence
    // byte-identical, which is required for frozen snapshot compatibility.
    let auction_extension = crate::calendar::market_has_auction_extension(&resource.market);
    if auction_extension {
        map.insert(
            "completion_semantics".into(),
            Value::String(resource.completion.label().to_string()),
        );
        map.insert(
            "completion".into(),
            resource
                .completion
                .provenance()
                .map(|provenance| provenance.value().clone())
                .unwrap_or(Value::Null),
        );
    }
    map.insert(
        "sessions".into(),
        Value::Array(
            resource
                .sessions
                .iter()
                .map(|session| {
                    let mut row = Map::new();
                    row.insert("date".into(), Value::String(session.date.clone()));
                    row.insert("close_at".into(), Value::String(session.close_at.clone()));
                    if auction_extension {
                        row.insert(
                            "auction_end_at".into(),
                            session
                                .auction_end_at
                                .clone()
                                .map(Value::String)
                                .unwrap_or(Value::Null),
                        );
                        row.insert(
                            "complete_at".into(),
                            session
                                .complete_at
                                .clone()
                                .map(Value::String)
                                .unwrap_or(Value::Null),
                        );
                    }
                    Value::Object(row)
                })
                .collect(),
        ),
    );
    Value::Object(map)
}

fn check_provenance_strings(block: &Value, findings: &mut Findings) {
    for name in ["actual_source", "currency", "unit"] {
        let Some(value) = block.get(name) else {
            continue;
        };
        if value.is_null() {
            continue;
        }
        let valid =
            matches!(value.as_str(), Some(text) if !text.is_empty() && text.chars().count() <= 200);
        if !valid {
            findings.errors.push(format!(
                "market {name} must be a bounded non-empty provenance string"
            ));
            findings.reason_codes.push(
                if name == "actual_source" {
                    "SOURCE_IDENTITY_CONFLICT"
                } else {
                    "MARKET_UNITS_CONFLICT"
                }
                .to_string(),
            );
        }
    }
}

fn check_identity(
    data: &Map<String, Value>,
    block: &Map<String, Value>,
    instrument: &Map<String, Value>,
    expected_symbol: &str,
    findings: &mut Findings,
) {
    let symbol = data.get("symbol").or_else(|| block.get("source_symbol"));
    match symbol {
        None | Some(Value::Null) => findings.fatal(
            "SOURCE_IDENTITY_UNVERIFIED",
            "market source symbol is missing",
        ),
        Some(Value::String(text)) if text.to_uppercase() == expected_symbol.to_uppercase() => {}
        Some(_) => {
            findings
                .errors
                .push("market source symbol does not match the resolved request instrument".into());
            findings
                .reason_codes
                .push("SOURCE_IDENTITY_CONFLICT".into());
        }
    }
    let frequency = data.get("frequency").or_else(|| block.get("frequency"));
    match frequency {
        None | Some(Value::Null) => {
            findings.fatal("FREQUENCY_UNVERIFIED", "market frequency is missing")
        }
        Some(Value::String(text)) if matches!(text.as_str(), "1d" | "1D" | "daily") => {}
        Some(_) => {
            findings.errors.push(
                "direction research requires daily frequency, not a compressed/weekly series"
                    .into(),
            );
            findings.reason_codes.push("FREQUENCY_UNVERIFIED".into());
        }
    }
    let has_source = block
        .get("actual_source")
        .and_then(Value::as_str)
        .map(|text| !text.is_empty())
        .unwrap_or(false);
    if !has_source {
        findings.fatal(
            "SOURCE_IDENTITY_UNVERIFIED",
            "market actual_source is missing",
        );
    }
    let has_currency = block
        .get("currency")
        .and_then(Value::as_str)
        .map(|text| !text.is_empty())
        .unwrap_or(false);
    let has_unit = block
        .get("unit")
        .and_then(Value::as_str)
        .map(|text| !text.is_empty())
        .unwrap_or(false);
    if !has_currency || !has_unit {
        findings.degraded(
            "MARKET_UNITS_UNVERIFIED",
            "market currency/unit provenance is missing",
        );
    }
    for name in ["currency", "unit"] {
        let payload = data.get(name).filter(|value| !value.is_null());
        let block_value = block.get(name).filter(|value| !value.is_null());
        if let (Some(payload), Some(block_value)) = (payload, block_value) {
            if payload != block_value {
                findings.errors.push(format!(
                    "market {name} differs between metadata and payload"
                ));
                findings.reason_codes.push("MARKET_UNITS_CONFLICT".into());
            }
        }
        if let (Some(expected), Some(block_value)) = (
            instrument.get(name).filter(|value| !value.is_null()),
            block_value,
        ) {
            if block_value != expected {
                findings.errors.push(format!(
                    "market {name} differs from the resolved instrument"
                ));
                findings.reason_codes.push("MARKET_UNITS_CONFLICT".into());
            }
        }
    }
}

fn check_timing(
    block: &Map<String, Value>,
    asof: DateTime<Utc>,
    zone: Tz,
    last: &str,
    captured_at: Option<&str>,
    findings: &mut Findings,
) -> (Option<DateTime<Utc>>, Option<DateTime<Utc>>) {
    let fetched = block
        .get("fetched_at_utc")
        .or_else(|| block.get("fetched_at"));
    let mut parsed: Vec<(&str, Option<DateTime<Utc>>)> = Vec::new();
    for (name, value) in [
        ("source_timestamp", block.get("source_timestamp")),
        ("fetched_at", fetched),
    ] {
        match value {
            None | Some(Value::Null) => {
                findings.fatal(
                    "TEMPORAL_PROVENANCE_MISSING",
                    &format!("market {name} is missing"),
                );
                parsed.push((name, None));
            }
            Some(value) => match parse_timestamp(value) {
                Ok(instant) => parsed.push((name, Some(instant))),
                Err(_) => {
                    findings
                        .errors
                        .push(format!("market {name} must be a timezone-aware timestamp"));
                    findings
                        .reason_codes
                        .push("TEMPORAL_PROVENANCE_MISSING".into());
                    parsed.push((name, None));
                }
            },
        }
    }
    let source_time = parsed
        .iter()
        .find(|(name, _)| *name == "source_timestamp")
        .and_then(|(_, value)| *value);
    let fetch_time = parsed
        .iter()
        .find(|(name, _)| *name == "fetched_at")
        .and_then(|(_, value)| *value);
    if let Some(source_time) = source_time {
        if source_time > asof {
            findings
                .errors
                .push("market source_timestamp occurs after the requested as-of".into());
            findings
                .reason_codes
                .push("TEMPORAL_PROVENANCE_MISSING".into());
        }
        if let Some(fetch_time) = fetch_time {
            if source_time > fetch_time {
                findings
                    .errors
                    .push("market source_timestamp occurs after fetched_at".into());
                findings
                    .reason_codes
                    .push("TEMPORAL_PROVENANCE_MISSING".into());
            }
        }
        if source_time.with_timezone(&zone).date_naive()
            < policy::parse_iso_date(last).unwrap_or(source_time.date_naive())
        {
            findings.fatal(
                "LATEST_OBSERVATION_UNRESOLVED",
                "market source timestamp predates the latest OHLCV observation",
            );
        }
    }
    match captured_at {
        None => findings.fatal(
            "TEMPORAL_PROVENANCE_MISSING",
            "collection capture timestamp is missing",
        ),
        Some(captured_at) => match crate::contracts::parse_timestamp_str(captured_at) {
            Ok(capture_time) => {
                if let Some(fetch_time) = fetch_time {
                    if fetch_time > capture_time {
                        findings
                            .errors
                            .push("market fetched_at occurs after collection capture".into());
                        findings
                            .reason_codes
                            .push("TEMPORAL_PROVENANCE_MISSING".into());
                    }
                }
            }
            Err(_) => {
                findings
                    .errors
                    .push("collection capture timestamp must be timezone-aware".into());
                findings
                    .reason_codes
                    .push("TEMPORAL_PROVENANCE_MISSING".into());
            }
        },
    }
    (source_time, fetch_time)
}

fn check_history_window(
    data: &Map<String, Value>,
    request: &Value,
    first: &str,
    last: &str,
    findings: &mut Findings,
) {
    let window = data.get("history_window");
    let policy_window = data.get("policy_window");
    if let Some(policy_window) = object(policy_window) {
        if policy_window.get("status").and_then(Value::as_str) == Some("resolved") {
            let start = text(policy_window.get("window_start_exclusive"))
                .unwrap_or_default()
                .to_string();
            let end = text(policy_window.get("window_end_inclusive"))
                .unwrap_or_default()
                .to_string();
            let window_matches = object(window)
                .map(|window| {
                    text(window.get("start")) == Some(start.as_str())
                        && text(window.get("end")) == Some(end.as_str())
                })
                .unwrap_or(false);
            if !window_matches {
                findings.fatal(
                    "FIXED_WINDOW_CONFLICT",
                    "market history_window must equal the frozen policy boundary",
                );
            } else if !(start.as_str() < first && first <= last && last <= end.as_str()) {
                findings.fatal(
                    "OHLCV_OUT_OF_WINDOW",
                    "market bars fall outside the frozen policy window",
                );
            }
            let has_selection = object(window)
                .and_then(|window| window.get("selection"))
                .map(|value| {
                    !value.is_null() && !matches!(value, Value::String(text) if text.is_empty())
                })
                .unwrap_or(false);
            if !has_selection {
                findings.fatal(
                    "SOURCE_IDENTITY_UNVERIFIED",
                    "market history_window selection is missing",
                );
            }
            return;
        }
    }
    match object(window) {
        None => findings.fatal(
            "SOURCE_IDENTITY_UNVERIFIED",
            "market actual history_window is missing",
        ),
        Some(window) => {
            let start =
                text(window.get("start")).and_then(|value| policy::parse_iso_date(value).ok());
            let end = text(window.get("end")).and_then(|value| policy::parse_iso_date(value).ok());
            match (start, end) {
                (Some(start), Some(end)) => {
                    if policy::iso_date(start) != first || policy::iso_date(end) != last {
                        findings.errors.push(
                            "market history_window must describe the actual normalized closed bars"
                                .into(),
                        );
                        findings
                            .reason_codes
                            .push("SOURCE_IDENTITY_CONFLICT".into());
                    }
                }
                _ => {
                    findings
                        .errors
                        .push("market history_window has invalid start/end".into());
                    findings
                        .reason_codes
                        .push("SOURCE_IDENTITY_CONFLICT".into());
                }
            }
            let has_selection = window
                .get("selection")
                .map(|value| {
                    !value.is_null() && !matches!(value, Value::String(text) if text.is_empty())
                })
                .unwrap_or(false);
            if !has_selection {
                findings.fatal(
                    "SOURCE_IDENTITY_UNVERIFIED",
                    "market history_window selection is missing",
                );
            }
        }
    }
    let requested = object(request.get("history_window"));
    let requested_start = requested.and_then(|value| text(value.get("start")));
    let requested_end = requested.and_then(|value| text(value.get("end")));
    if requested_start.map(|start| first < start).unwrap_or(false)
        || requested_end.map(|end| last > end).unwrap_or(false)
    {
        findings
            .errors
            .push("market bars extend outside the user-requested history window".into());
        findings
            .reason_codes
            .push("SOURCE_IDENTITY_CONFLICT".into());
    }
}

fn resource_from_block(block: &Value, market: &str) -> SdResult<CalendarResource> {
    let calendar = block
        .get("session_calendar")
        .filter(|value| value.is_object())
        .ok_or_else(|| SdError::validation("source-backed session_calendar is unavailable"))?;
    CalendarResource::from_snapshot_calendar(calendar, market)
}

/// `_resolve_session_axis`: supplied schedule -> library schedule -> observed dates.
///
/// The returned `completion_verified` flag is false when the schedule cannot establish
/// a sourced completion boundary: such a calendar may still resolve sessions for
/// diagnostics, but it can never be `library_verified` evidence. Sessions whose
/// completion boundary is unknown are skipped by the latest-complete-close resolver.
#[allow(clippy::too_many_arguments)] // mirrors the legacy signature positionally
fn resolve_session_axis(
    block: &Value,
    request: &Value,
    bars: &[Value],
    first: &str,
    last: &str,
    asof: DateTime<Utc>,
    local_day: NaiveDate,
    resolved: Option<&CalendarResource>,
    allow_library_calendar: bool,
    library: Option<&CalendarResource>,
    findings: &mut Findings,
) -> (Vec<CalendarSession>, String, String, Option<Value>, bool) {
    let mut sessions: Vec<CalendarSession> = Vec::new();
    let mut completion_verified = false;
    let mut calendar_status = "observation_fallback".to_string();
    let mut resolved_out: Option<CalendarResource> = resolved.cloned();
    let block_calendar = block
        .get("session_calendar")
        .filter(|value| !value.is_null());
    let has_calendar = block_calendar.is_some() || resolved.is_some();
    if has_calendar {
        let candidate =
            match resource_from_block(block, text(request.get("market")).unwrap_or_default()) {
                Ok(resource) => Ok(resource),
                Err(_) => match resolved {
                    Some(resource) => Ok(resource.clone()),
                    None => Err(()),
                },
            };
        match candidate {
            Ok(candidate) => {
                let coverage_start = block
                    .get("session_calendar")
                    .and_then(|value| text(value.get("coverage_start")))
                    .map(str::to_string)
                    .or_else(|| resolved.map(|resource| resource.coverage_start.clone()))
                    .unwrap_or_default();
                let coverage_end = block
                    .get("session_calendar")
                    .and_then(|value| text(value.get("coverage_end")))
                    .map(str::to_string)
                    .or_else(|| resolved.map(|resource| resource.coverage_end.clone()))
                    .unwrap_or_default();
                if coverage_start.as_str() <= first
                    && coverage_end.as_str() >= policy::iso_date(local_day).as_str()
                {
                    let unverified_reason =
                        candidate.completion_unverified_reason().map(str::to_string);
                    completion_verified = candidate.completion_is_verified();
                    sessions = candidate.sessions;
                    calendar_status = if resolved.is_some() && block_calendar.is_none() {
                        if completion_verified {
                            "library_verified".to_string()
                        } else {
                            "library_completion_unverified".to_string()
                        }
                    } else {
                        "source_supplied_schedule".to_string()
                    };
                    if let Some(reason) = unverified_reason {
                        findings.degraded(
                            "CALENDAR_COMPLETION_UNVERIFIED",
                            &format!(
                                "exchange calendar has no verified session-completion boundary ({reason}); \
                                 the latest complete close cannot be treated as verified and a formal \
                                 probability is not publishable"
                            ),
                        );
                    }
                } else {
                    findings.degraded(
                        "CALENDAR_OFFICIAL_MISSING",
                        "supplied exchange calendar does not cover the full history through as-of",
                    );
                }
            }
            Err(()) => findings.degraded(
                "CALENDAR_SOURCE_INVALID",
                "supplied exchange calendar could not be used; resolving a fallback (ValueError)",
            ),
        }
    }
    if sessions.is_empty() && allow_library_calendar {
        if let Some(resource) = library {
            completion_verified = resource.completion_is_verified();
            sessions = resource.sessions.clone();
            if completion_verified {
                calendar_status = "library_verified".to_string();
            } else {
                // A legacy/incomplete pack stays readable for diagnostics but cannot
                // establish a verified latest complete close.
                calendar_status = "library_completion_unverified".to_string();
                if let Some(reason) = resource.completion_unverified_reason() {
                    findings.degraded(
                        "CALENDAR_COMPLETION_UNVERIFIED",
                        &format!(
                            "calendar library resource has no verified session-completion boundary ({reason}); \
                             the latest complete close cannot be treated as verified and a formal \
                             probability is not publishable"
                        ),
                    );
                }
            }
            resolved_out = Some(resource.clone());
        }
    }
    if sessions.is_empty() {
        if !has_calendar {
            findings.degraded(
                "CALENDAR_OFFICIAL_MISSING",
                "no complete source-backed exchange calendar is available",
            );
        }
        findings.degraded(
            "CALENDAR_FALLBACK_USED",
            "session horizon uses ordered OHLCV observations; calendar holidays and missing sessions are not independently verified",
        );
        let mut previous: Option<NaiveDate> = None;
        for bar in bars {
            let day = match bar.get("date").map(date_only) {
                Some(Ok(day)) => day,
                _ => {
                    findings.fatal(
                        "INVALID_TIME_ORDER",
                        "an OHLCV observation has an invalid session date",
                    );
                    continue;
                }
            };
            if day.weekday().num_days_from_monday() >= 5 {
                findings.fatal(
                    "INVALID_TIME_ORDER",
                    &format!(
                        "daily stock observation falls on a weekend: {}",
                        policy::iso_date(day)
                    ),
                );
            }
            if let Some(previous) = previous {
                let gap = (day - previous).num_days();
                if gap > 14 {
                    findings.fatal(
                        "SEVERE_MISSING_BARS",
                        &format!("OHLCV observations have an unexplained {gap}-calendar-day gap"),
                    );
                }
            }
            previous = Some(day);
        }
        return (
            sessions,
            calendar_status,
            "estimated_observed_sessions".into(),
            None,
            false,
        );
    }
    let unverified_in_window = sessions
        .iter()
        .filter(|session| {
            session.complete_at.is_none()
                && session.date.as_str() >= first
                && session.date.as_str() <= last
        })
        .count();
    if unverified_in_window > 0 {
        findings.degraded(
            "CALENDAR_COMPLETION_UNVERIFIED",
            &format!(
                "{unverified_in_window} exchange session(s) in the checked window have no verified \
                 session-completion boundary; those sessions are skipped when resolving the latest \
                 complete close and a formal probability is not publishable across this window"
            ),
        );
    }
    let closed: Vec<&CalendarSession> = sessions
        .iter()
        .filter(|session| {
            session
                .parsed_complete()
                .ok()
                .flatten()
                .map(|complete| complete <= asof)
                .unwrap_or(false)
        })
        .collect();
    let expected_latest = closed.last().map(|session| session.date.clone());
    if expected_latest.as_deref() != Some(last) {
        findings.fatal(
            "LATEST_OBSERVATION_UNRESOLVED",
            &format!(
                "latest bar {last} does not match the latest complete exchange session {}",
                expected_latest.unwrap_or_else(|| "None".to_string())
            ),
        );
    }
    let expected_axis: Vec<String> = closed
        .iter()
        .map(|session| session.date.clone())
        .filter(|date| first <= date.as_str() && date.as_str() <= last)
        .collect();
    let actual_axis: Vec<String> = bars
        .iter()
        .map(|bar| {
            bar.get("date")
                .map(crate::contracts::py_str)
                .unwrap_or_default()
        })
        .map(|value| value.get(..10).unwrap_or(&value).to_string())
        .collect();
    if expected_axis != actual_axis {
        findings.fatal(
            "SEVERE_MISSING_BARS",
            "daily bars do not match the resolved exchange session axis; confirmed sessions are missing",
        );
    }
    let resolved_value = resolved_out.as_ref().map(calendar_resource_value);
    (
        sessions,
        calendar_status,
        "exchange_sessions".into(),
        resolved_value,
        completion_verified,
    )
}

#[allow(clippy::too_many_arguments)] // mirrors the legacy signature positionally
fn check_latest_close(
    block: &Map<String, Value>,
    block_value: &Value,
    bars: &[Value],
    sessions: &[CalendarSession],
    last: &str,
    asof: DateTime<Utc>,
    calendar_status: &str,
    completion_verified: bool,
    source_time: Option<DateTime<Utc>>,
    fetch_time: Option<DateTime<Utc>>,
    findings: &mut Findings,
) -> Value {
    let (latest_status, latest_codes, latest_warnings, latest_errors) =
        check_price_observations(block_value, bars, last);
    findings.reason_codes.extend(latest_codes.clone());
    findings.warnings.extend(latest_warnings);
    if latest_codes
        .iter()
        .any(|code| code == "LATEST_PRICE_CONFLICT")
    {
        findings.blockers.extend(latest_errors);
    } else {
        findings.errors.extend(latest_errors);
    }
    let mut latest_close = Map::new();
    latest_close.insert(
        "price".into(),
        bars.last()
            .and_then(bar_close)
            .map(Value::from)
            .unwrap_or(Value::Null),
    );
    latest_close.insert("session_date".into(), Value::String(last.to_string()));
    latest_close.insert("close_at".into(), Value::Null);
    latest_close.insert(
        "currency".into(),
        block.get("currency").cloned().unwrap_or(Value::Null),
    );
    latest_close.insert("kind".into(), Value::String(latest_status));
    latest_close.insert(
        "source".into(),
        block.get("actual_source").cloned().unwrap_or(Value::Null),
    );
    if block.get("last_bar_closed") == Some(&Value::Bool(false)) {
        findings.fatal(
            "LATEST_OBSERVATION_UNRESOLVED",
            "provider marked the latest OHLCV bar as not closed",
        );
        latest_close.insert("kind".into(), Value::String("unverified".into()));
    }
    let Some(close_row) = sessions.iter().find(|session| session.date == last) else {
        return Value::Object(latest_close);
    };
    let close_at = close_row.close_at.clone();
    latest_close.insert("close_at".into(), Value::String(close_at.clone()));
    // The resolution boundary is the sourced completion instant, not the continuous
    // close: a session without a verified completion boundary cannot confirm a bar.
    let Some(completion_instant) = close_row.completion_boundary() else {
        findings.fatal(
            "LATEST_OBSERVATION_UNRESOLVED",
            "the latest bar's exchange session has no verified completion boundary; the bar is not confirmed complete",
        );
        latest_close.insert("kind".into(), Value::String("unverified".into()));
        return Value::Object(latest_close);
    };
    if let (Some(fetch_time), Some(source_time)) = (fetch_time, source_time) {
        if completion_instant <= asof {
            let label_resolves = source_time
                >= completion_instant - chrono::Duration::seconds(DAILY_BAR_COVERAGE_SECONDS);
            if fetch_time < completion_instant || !label_resolves {
                findings.fatal(
                    "LATEST_OBSERVATION_UNRESOLVED",
                    "market source/fetch timestamp predates the latest session close",
                );
                latest_close.insert("kind".into(), Value::String("unverified".into()));
                findings
                    .reason_codes
                    .retain(|code| code != "LATEST_CLOSE_CROSS_VERIFIED");
            }
        }
    }
    if latest_close.get("kind").and_then(Value::as_str) == Some("provider_verified")
        && completion_verified
        && matches!(
            calendar_status,
            "source_supplied_schedule" | "library_verified"
        )
    {
        latest_close.insert("kind".into(), Value::String("calendar_verified".into()));
    }
    Value::Object(latest_close)
}

fn positive_number(value: Option<&Value>) -> Option<f64> {
    let number = match value? {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    if number.is_finite() && number > 0.0 {
        Some(number)
    } else {
        None
    }
}

fn action_type(action: &Map<String, Value>) -> String {
    const ALIASES: [(&str, &str); 10] = [
        ("stock_splits", "split"),
        ("stock_split", "split"),
        ("splits", "split"),
        ("split", "split"),
        ("reverse_split", "reverse_split"),
        ("reverse_split_action", "reverse_split"),
        ("dividends", "dividend"),
        ("dividend", "dividend"),
        ("capital_gains", "capital_gain"),
        ("capital_gain", "capital_gain"),
    ];
    for key in ["type", "kind", "action_type"] {
        let Some(value) = action.get(key).and_then(Value::as_str) else {
            continue;
        };
        let normalized = value.trim().to_lowercase().replace(['-', ' '], "_");
        if let Some((_, mapped)) = ALIASES.iter().find(|(name, _)| *name == normalized) {
            return (*mapped).to_string();
        }
        if !normalized.is_empty() {
            return normalized;
        }
    }
    String::new()
}

fn check_adjustment(
    block: &Map<String, Value>,
    first: &str,
    last: &str,
    bars: &[Value],
    findings: &mut Findings,
) -> (String, bool, Vec<Value>) {
    let adjustment = text(block.get("adjustment")).map(str::to_string);
    let normalized_adjustment = adjustment.clone().map(|value| {
        ADJUSTMENT_ALIASES
            .iter()
            .find(|(alias, _)| *alias == value)
            .map(|(_, mapped)| (*mapped).to_string())
            .unwrap_or(value)
    });
    let adjustment_known = normalized_adjustment
        .as_ref()
        .map(|value| ADJUSTMENTS.contains(&value.as_str()))
        .unwrap_or(false);
    let evidence = block
        .get("adjustment_evidence")
        .filter(|value| value.is_object());
    let mut corporate_action_status = "unavailable".to_string();
    let mut reconciliation: Vec<Value> = Vec::new();
    if !adjustment_known {
        findings.degraded(
            "ADJUSTMENT_UNKNOWN",
            "OHLCV adjustment convention is unknown; short-horizon direction remains computable with lower confidence",
        );
    }
    let Some(evidence) = evidence else {
        findings.degraded(
            "ADJUSTMENT_EVIDENCE_UNAVAILABLE",
            "source-backed adjustment/company-action coverage is unavailable; unknown does not mean contaminated",
        );
        findings.degraded(
            "CORPORATE_ACTION_UNKNOWN",
            "recent corporate actions could not be independently assessed",
        );
        return (corporate_action_status, adjustment_known, reconciliation);
    };
    if text(evidence.get("price_basis")) != normalized_adjustment.as_deref() {
        findings.degraded(
            "ADJUSTMENT_EVIDENCE_CONFLICT",
            "adjustment evidence does not establish the OHLCV price basis",
        );
    }
    let checked = text(evidence.get("checked_through"))
        .and_then(|value| policy::parse_iso_date(value).ok())
        .map(policy::iso_date);
    let Some(checked) = checked else {
        findings.degraded(
            "CORPORATE_ACTION_UNKNOWN",
            "adjustment/company-action evidence is malformed and was not relied on",
        );
        return (corporate_action_status, adjustment_known, reconciliation);
    };
    let evidence_covered = checked.as_str() >= last;
    if !evidence_covered {
        findings.degraded(
            "CORPORATE_ACTION_UNKNOWN",
            "company-action evidence does not cover the latest market bar",
        );
    } else if evidence
        .get("action_coverage_confirmed")
        .unwrap_or(&Value::Bool(true))
        != &Value::Bool(true)
    {
        findings.degraded(
            "CORPORATE_ACTION_UNKNOWN",
            "company-action evidence does not confirm complete action coverage; an empty actions list does not establish that there were no actions",
        );
    } else {
        corporate_action_status = "clear".to_string();
    }
    let ordered: Vec<String> = bars
        .iter()
        .map(|bar| {
            bar.get("date")
                .map(crate::contracts::py_str)
                .unwrap_or_default()
        })
        .map(|value| value.get(..10).unwrap_or(&value).to_string())
        .collect();
    let closes: Vec<(String, Option<f64>)> = bars
        .iter()
        .map(|bar| {
            (
                bar.get("date")
                    .map(crate::contracts::py_str)
                    .unwrap_or_default()
                    .get(..10)
                    .unwrap_or_default()
                    .to_string(),
                bar_close(bar),
            )
        })
        .collect();
    for action in array(evidence.get("actions")) {
        let Some(action) = action.as_object() else {
            continue;
        };
        let action_day = text(action.get("date"))
            .and_then(|value| policy::parse_iso_date(value).ok())
            .map(policy::iso_date);
        let Some(action_day) = action_day else {
            findings.degraded(
                "CORPORATE_ACTION_UNKNOWN",
                "adjustment/company-action evidence is malformed and was not relied on",
            );
            return (corporate_action_status, adjustment_known, reconciliation);
        };
        let in_window = first <= action_day.as_str() && action_day.as_str() <= last;
        if in_window {
            corporate_action_status = "recent_action_detected".to_string();
        }
        let kind = action_type(action);
        let contaminating = [
            "split",
            "reverse_split",
            "spinoff",
            "major_adjustment_discontinuity",
        ]
        .contains(&kind.as_str());
        let prices_unadjusted = adjustment.as_deref() == Some("unadjusted")
            || text(evidence.get("price_basis")) == Some("unadjusted");
        if contaminating && in_window && prices_unadjusted {
            findings.fatal(
                "CORPORATE_ACTION_CONTAMINATION",
                "confirmed corporate action falls in the selected window while OHLCV is evidenced as unadjusted",
            );
            continue;
        }
        if !in_window || kind != "split" {
            continue;
        }
        let ratio = positive_number(action.get("value").or_else(|| action.get("ratio")));
        let Some(ratio) = ratio else { continue };
        if ratio == 1.0 || !ordered.contains(&action_day) {
            continue;
        }
        let Some(position) = ordered.iter().position(|day| *day == action_day) else {
            continue;
        };
        if position == 0 {
            continue;
        }
        let before = closes
            .get(position - 1)
            .and_then(|(_, close)| *close)
            .filter(|value| value.is_finite() && *value > 0.0);
        let after = closes
            .get(position)
            .and_then(|(_, close)| *close)
            .filter(|value| value.is_finite() && *value > 0.0);
        let (Some(before), Some(after)) = (before, after) else {
            continue;
        };
        let step = after / before;
        let adjusted_step = 1.0 / ratio;
        let verdict = if step.ln().abs() <= 0.10 {
            "consistent_with_split_adjusted"
        } else if (step / adjusted_step).ln().abs() <= 0.10 {
            "contradicts_split_adjusted"
        } else {
            "unreconciled"
        };
        let mut row = Map::new();
        row.insert("action_date".into(), Value::String(action_day.clone()));
        row.insert("declared_ratio".into(), Value::from(ratio));
        row.insert("observed_close_step".into(), Value::from(round6(step)));
        row.insert("verdict".into(), Value::String(verdict.into()));
        reconciliation.push(Value::Object(row));
        if verdict == "contradicts_split_adjusted"
            && normalized_adjustment.as_deref() == Some("split_adjusted")
        {
            findings.fatal(
                "CORPORATE_ACTION_CONTAMINATION",
                &format!(
                    "a {}:1 split on {action_day} produced a close step of {:.4}; the series behaves as unadjusted although the basis is declared split-adjusted",
                    format_general(ratio),
                    step
                ),
            );
        } else if verdict == "unreconciled" {
            findings.degraded(
                "ADJUSTMENT_EVIDENCE_CONFLICT",
                &format!(
                    "a {}:1 split on {action_day} could not be reconciled with the price series",
                    format_general(ratio)
                ),
            );
        }
    }
    (corporate_action_status, adjustment_known, reconciliation)
}

fn round6(value: f64) -> f64 {
    (value * 1_000_000.0).round() / 1_000_000.0
}

/// Python `%g` formatting for the split ratio in a message.
fn format_general(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e16 {
        format!("{}", value as i64)
    } else {
        crate::json_text::format_python_float(value)
    }
}

fn check_price_scale(bars: &[Value], findings: &mut Findings) {
    let mut extreme = false;
    let mut invalid = false;
    for pair in bars.windows(2) {
        let (Some(previous), Some(current)) = (bar_close(&pair[0]), bar_close(&pair[1])) else {
            invalid = true;
            continue;
        };
        if previous == 0.0 || !previous.is_finite() || !current.is_finite() {
            invalid = true;
            continue;
        }
        if (current / previous - 1.0).abs() > 0.5 {
            extreme = true;
        }
    }
    if invalid {
        findings.fatal(
            "INVALID_PRICE_VALUES",
            "OHLCV price values cannot be evaluated",
        );
    }
    if extreme {
        findings.degraded(
            "EXTREME_PRICE_MOVE_UNRECONCILED",
            "an extreme close-to-close move needs review; it was not automatically treated as a confirmed corporate action",
        );
    }
}

#[allow(clippy::too_many_arguments)] // mirrors the legacy signature positionally
fn check_forecast_policy(
    block: &Map<String, Value>,
    request: &Value,
    data: &Map<String, Value>,
    bars: &[Value],
    last: &str,
    calendar_status: &str,
    completion_verified: bool,
    sessions: &[CalendarSession],
    findings: &mut Findings,
) -> Value {
    if text(request.get("intent")) != Some("forecast") {
        let mut map = Map::new();
        map.insert("applicable".into(), Value::Bool(false));
        return Value::Object(map);
    }
    let mut output = Map::new();
    output.insert("applicable".into(), Value::Bool(true));
    output.insert(
        "policy_version".into(),
        Value::String(policy::POLICY_VERSION.into()),
    );
    let declared = request
        .get("policy")
        .and_then(|value| value.get("policy_version"))
        .and_then(Value::as_str);
    if declared != Some(policy::POLICY_VERSION) {
        findings.fatal(
            policy::ERROR_POLICY_MISMATCH,
            &format!("request policy_version must be {}", policy::POLICY_VERSION),
        );
        findings.errors.push(format!(
            "request policy_version must be {}",
            policy::POLICY_VERSION
        ));
    }
    let window = object(data.get("policy_window"));
    let Some(window) =
        window.filter(|window| window.get("status").and_then(Value::as_str) == Some("resolved"))
    else {
        findings.fatal(
            "DECISION_SESSION_UNRESOLVED",
            "no completed decision session could be resolved",
        );
        findings
            .errors
            .push("no completed decision session could be resolved".into());
        return Value::Object(output);
    };
    let decision = text(window.get("decision_session"))
        .unwrap_or_default()
        .to_string();
    let (start, end) = match policy::parse_iso_date(&decision) {
        Ok(date) => policy::window_bounds(date),
        Err(_) => (decision.clone(), decision.clone()),
    };
    let window_matches = text(window.get("window_start_exclusive")) == Some(start.as_str())
        && text(window.get("window_end_inclusive")) == Some(end.as_str());
    if !window_matches {
        findings.fatal(
            policy::ERROR_FIXED_WINDOW_CONFLICT,
            "snapshot window does not match the policy boundary",
        );
        findings
            .errors
            .push("snapshot window does not match the policy boundary".into());
    }
    let declared_window = object(request.get("history_window"));
    if let Some(declared_start) = declared_window.and_then(|value| text(value.get("start"))) {
        if declared_start != start {
            findings.fatal(
                policy::ERROR_FIXED_WINDOW_CONFLICT,
                "requested history start conflicts with the policy boundary",
            );
            findings
                .errors
                .push("requested history start conflicts with the policy boundary".into());
        }
    }
    if let Some(declared_end) = declared_window.and_then(|value| text(value.get("end"))) {
        if declared_end != end {
            findings.fatal(
                policy::ERROR_FIXED_WINDOW_CONFLICT,
                "requested history end conflicts with the policy boundary",
            );
            findings
                .errors
                .push("requested history end conflicts with the policy boundary".into());
        }
    }
    if last != decision {
        findings.fatal(
            "DECISION_SESSION_UNRESOLVED",
            "last retained bar is not the decision session close",
        );
        findings
            .errors
            .push("last retained bar is not the decision session close".into());
    }
    let outside = bars
        .iter()
        .filter(|bar| {
            let date = bar
                .get("date")
                .map(crate::contracts::py_str)
                .unwrap_or_default();
            !policy::window_contains(&date, &start, &end)
        })
        .count();
    if outside > 0 {
        findings.fatal(
            "OHLCV_OUT_OF_WINDOW",
            &format!("{outside} bar(s) fall outside the frozen policy window"),
        );
        findings.errors.push(format!(
            "{outside} bar(s) fall outside the frozen policy window"
        ));
    }
    let declared_coverage = text(window.get("coverage_status")).map(str::to_string);
    let calendar_verified_flag = window
        .get("calendar_verified")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let verified_calendar = matches!(
        calendar_status,
        "source_supplied_schedule" | "library_verified"
    ) && calendar_verified_flag
        && completion_verified;
    let mut recomputed: Option<crate::calendar::CoverageRecompute> = None;
    let mut coverage_basis = "declared_only";
    let mut coverage = "unverified";
    if verified_calendar {
        let session_dates: Vec<NaiveDate> = sessions
            .iter()
            .filter_map(|session| policy::parse_iso_date(&session.date).ok())
            .collect();
        let observed: Vec<String> = bars
            .iter()
            .map(|bar| {
                bar.get("date")
                    .map(crate::contracts::py_str)
                    .unwrap_or_default()
            })
            .map(|value| value.get(..10).unwrap_or(&value).to_string())
            .collect();
        let recompute = crate::calendar::recompute_window_coverage_from_dates(
            &session_dates,
            &observed,
            &start,
            &end,
            window,
        );
        coverage_basis = "recomputed_from_calendar_and_bars";
        if !recompute.calendar_covers_window {
            findings.fatal(
                policy::ERROR_INSUFFICIENT_COVERAGE,
                "the resolved exchange calendar does not span the whole 52-week policy window",
            );
        } else if !recompute.complete {
            findings.fatal(
                policy::ERROR_INSUFFICIENT_COVERAGE,
                &format!(
                    "52-week window coverage is incomplete: expected {} sessions, observed {}, missing {}, unexpected {}, duplicates {}",
                    recompute.expected_sessions,
                    recompute.observed_sessions,
                    recompute.missing_session_count,
                    recompute.unexpected_sessions.len(),
                    recompute.duplicate_sessions.len()
                ),
            );
        } else if declared_coverage.as_deref() != Some("complete")
            || !recompute.declaration_mismatches.is_empty()
        {
            let detail = if recompute.declaration_mismatches.is_empty() {
                format!("status {}", python_opt_repr(&declared_coverage))
            } else {
                recompute.declaration_mismatches.join("; ")
            };
            findings.fatal(
                policy::ERROR_INSUFFICIENT_COVERAGE,
                &format!("declared coverage contradicts the recomputation: {detail}"),
            );
        } else {
            coverage = "complete";
        }
        recomputed = Some(recompute);
    }
    if !verified_calendar {
        findings.degraded(
            policy::ERROR_CALENDAR_UNVERIFIED,
            "exchange calendar is unverified; no formal 5-session probability is publishable",
        );
    }
    let price_basis = block.get("price_basis").cloned().unwrap_or(Value::Null);
    let price_basis_text = text(block.get("price_basis"));
    if price_basis_text != Some(policy::PRICE_BASIS) {
        let message = format!(
            "price basis {} is not the required {} split-adjusted close",
            crate::contracts::py_repr(&price_basis),
            crate::contracts::py_repr(&Value::String(policy::PRICE_BASIS.to_string()))
        );
        findings.fatal(policy::ERROR_PRICE_BASIS_UNVERIFIED, &message);
        findings.errors.push(message);
    }
    if bars.iter().any(|bar| !bar.get("available_at").is_some()) {
        findings.degraded(
            "POINT_IN_TIME_UNVERIFIED",
            "historical bars expose dates but no per-bar available_at; provider revision status is disclosed",
        );
    }
    let calendar_source = window
        .get("calendar_source")
        .filter(|value| !value.is_null())
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            block
                .get("session_calendar")
                .and_then(|value| text(value.get("source")))
                .map(str::to_string)
        })
        .unwrap_or_else(|| "unverified".to_string());
    output.insert("decision_session".into(), Value::String(decision));
    output.insert("window_start_exclusive".into(), Value::String(start));
    output.insert("window_end_inclusive".into(), Value::String(end));
    output.insert(
        "expected_sessions".into(),
        recomputed
            .as_ref()
            .map(|value| Value::from(value.expected_sessions))
            .or_else(|| window.get("expected_sessions").cloned())
            .unwrap_or(Value::Null),
    );
    output.insert(
        "observed_sessions".into(),
        recomputed
            .as_ref()
            .map(|value| Value::from(value.observed_sessions))
            .or_else(|| window.get("observed_sessions").cloned())
            .unwrap_or(Value::Null),
    );
    output.insert(
        "missing_sessions".into(),
        recomputed
            .as_ref()
            .map(|value| Value::from(value.missing_session_count))
            .or_else(|| window.get("missing_session_count").cloned())
            .unwrap_or(Value::Null),
    );
    output.insert(
        "missing_session_dates".into(),
        recomputed
            .as_ref()
            .map(|value| {
                Value::Array(
                    value
                        .missing_sessions
                        .iter()
                        .map(|day| Value::String(day.clone()))
                        .collect(),
                )
            })
            .or_else(|| window.get("missing_sessions").cloned())
            .unwrap_or(Value::Null),
    );
    output.insert(
        "declared_coverage_status".into(),
        declared_coverage.map(Value::String).unwrap_or(Value::Null),
    );
    output.insert("coverage_status".into(), Value::String(coverage.into()));
    output.insert(
        "coverage_basis".into(),
        Value::String(coverage_basis.into()),
    );
    output.insert(
        "coverage_recomputed".into(),
        Value::Bool(recomputed.is_some()),
    );
    output.insert("calendar_verified".into(), Value::Bool(verified_calendar));
    output.insert("calendar_source".into(), Value::String(calendar_source));
    output.insert("price_basis".into(), price_basis);
    output.insert(
        "price_basis_source".into(),
        block
            .get("price_basis_source")
            .cloned()
            .unwrap_or(Value::Null),
    );
    output.insert(
        "historical_availability_status".into(),
        Value::String("date_only_no_per_bar_available_at".into()),
    );
    output.insert(
        "provider_revision_status".into(),
        Value::String("provider_history_may_be_revised_not_point_in_time_certified".into()),
    );
    // The legacy function closes with the finding-derived status, after the payload
    // keys, so the readable artifact keeps that order.
    if !findings.blockers.is_empty() {
        output.insert("status".into(), Value::String("blocked".into()));
    } else if !findings.warnings.is_empty() {
        output.insert("status".into(), Value::String("degraded".into()));
    } else {
        output.insert("status".into(), Value::String("verified".into()));
    }
    Value::Object(output)
}

/// Python `repr()` of an optional string used in one error message.
fn python_opt_repr(value: &Option<String>) -> String {
    match value {
        Some(text) => crate::contracts::py_repr(&Value::String(text.clone())),
        None => "None".to_string(),
    }
}

/// Run every market check and return the gate block (`check_market`).
pub fn check_market(block: &Value, request: &Value, options: &CheckOptions<'_>) -> SdResult<Value> {
    let mut findings = Findings::default();
    check_provenance_strings(block, &mut findings);
    let block_object = object(Some(block)).cloned().unwrap_or_default();
    let data = object(block.get("data")).cloned().unwrap_or_default();
    let bars: Vec<Value> = array(data.get("bars")).to_vec();
    let bars_are_objects = bars.iter().all(Value::is_object);
    if bars.is_empty() || !bars_are_objects {
        findings.fatal("INSUFFICIENT_OHLCV", "no closed OHLCV bars");
        return Ok(gate(&findings, None, "unavailable", "unavailable", None));
    }
    let market = text(request.get("market")).unwrap_or("US");
    let asof = parse_timestamp(request.get("asof").unwrap_or(&Value::Null))?;
    let zone = market_zone(market)?;
    let local_day = asof.with_timezone(&zone).date_naive();
    let first = crate::contracts::py_str(&bars[0].get("date").cloned().unwrap_or(Value::Null));
    let first = first.get(..10).unwrap_or(&first).to_string();
    let last = crate::contracts::py_str(
        &bars
            .last()
            .and_then(|bar| bar.get("date"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    let last = last.get(..10).unwrap_or(&last).to_string();
    if bars.len() < MIN_OBSERVATION_BARS {
        findings.fatal(
            "INSUFFICIENT_OHLCV",
            &format!(
                "only {} daily observations; at least {MIN_OBSERVATION_BARS} are required for the minimum 20-observation feature",
                bars.len()
            ),
        );
    }
    let instrument = object(request.get("instrument"))
        .cloned()
        .unwrap_or_default();
    let expected_symbol = {
        let actual_source = text(block.get("actual_source"));
        let aliased = actual_source
            .and_then(|source| {
                instrument
                    .get("provider_symbols")
                    .and_then(|symbols| symbols.get(source))
            })
            .and_then(Value::as_str)
            .map(str::to_string);
        aliased.unwrap_or_else(|| text(request.get("ticker")).unwrap_or_default().to_string())
    };
    check_identity(
        &data,
        &block_object,
        &instrument,
        &expected_symbol,
        &mut findings,
    );
    let (source_time, fetch_time) = check_timing(
        &block_object,
        asof,
        zone,
        &last,
        options.captured_at,
        &mut findings,
    );
    check_history_window(&data, request, &first, &last, &mut findings);
    let (sessions, calendar_status, horizon_type, resolved_value, completion_verified) =
        resolve_session_axis(
            block,
            request,
            &bars,
            &first,
            &last,
            asof,
            local_day,
            options.resolved_calendar_evidence,
            options.allow_library_calendar,
            options.library_calendar,
            &mut findings,
        );
    if (local_day - policy::parse_iso_date(&last)?).num_days() > 7 {
        findings.fatal(
            "STALE_MARKET_DATA",
            "market history is more than seven calendar days old relative to as-of",
        );
    }
    let latest_close = check_latest_close(
        &block_object,
        block,
        &bars,
        &sessions,
        &last,
        asof,
        &calendar_status,
        completion_verified,
        source_time,
        fetch_time,
        &mut findings,
    );
    let (corporate_action_status, adjustment_known, reconciliation) =
        check_adjustment(&block_object, &first, &last, &bars, &mut findings);
    check_price_scale(&bars, &mut findings);
    let policy_block = if options.enforce_policy {
        check_forecast_policy(
            &block_object,
            request,
            &data,
            &bars,
            &last,
            &calendar_status,
            completion_verified,
            &sessions,
            &mut findings,
        )
    } else {
        let mut map = Map::new();
        map.insert("applicable".into(), Value::Bool(false));
        map.insert("status".into(), Value::String("legacy_not_enforced".into()));
        Value::Object(map)
    };
    let asset_type = text(request.get("asset_type"));
    if asset_type.is_none() || matches!(asset_type, Some("unknown")) {
        findings.degraded(
            "ASSET_TYPE_UNVERIFIED",
            "asset type is unverified; ETF-specific exposure/NAV/leverage checks are not assessed",
        );
    }
    if asset_type == Some("etf") {
        findings.degraded(
            "ASSET_TYPE_UNVERIFIED",
            "ETF NAV, holdings-date, tracking error and leverage/path risks are not assessed by the OHLCV engine",
        );
    }
    let mut result = gate(
        &findings,
        Some(latest_close),
        &calendar_status,
        &horizon_type,
        None,
    );
    if let Value::Object(map) = &mut result {
        map.insert(
            "resolved_calendar_evidence".into(),
            resolved_value.unwrap_or(Value::Null),
        );
        map.insert(
            "adjustment_status".into(),
            Value::String(
                if block_object.get("adjustment").and_then(Value::as_str) == Some("provider") {
                    "provider_declared_split_adjusted"
                } else if adjustment_known {
                    "provider_declared"
                } else {
                    "unknown"
                }
                .into(),
            ),
        );
        map.insert(
            "corporate_action_status".into(),
            Value::String(corporate_action_status),
        );
        map.insert(
            "corporate_action_reconciliation".into(),
            Value::Array(reconciliation),
        );
        map.insert("policy".into(), policy_block.clone());
        if text(request.get("intent")) == Some("forecast") {
            map.insert(
                "forecast_policy_status".into(),
                policy_block.get("status").cloned().unwrap_or(Value::Null),
            );
        }
    }
    Ok(result)
}
