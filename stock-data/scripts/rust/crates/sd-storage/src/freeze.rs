//! Freeze a collected receipt into a validated, content-addressed snapshot.
//!
//! Port of `snapshot.freeze_snapshot`, `snapshot.validate_collection`,
//! `snapshot._unwrap_domain` and `snapshot._derived_metadata`.
//!
//! Order is the contract: the collection is validated first, an unconfirmed
//! same-as-of bar is excluded next, then the data gate runs (twice when a library
//! schedule was resolved), digests are computed over the *normalized* domains and
//! finally the directory is written atomically through a scratch directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use chrono::NaiveDate;
use chrono_tz::Tz;
use serde_json::{Map, Value};

use sd_core::calendar::CalendarResource;
use sd_core::clock::Clock;
use sd_core::contracts::{normalize_request, parse_timestamp, py_str};
use sd_core::data_checks::{check_market, CheckOptions};
use sd_core::error::{SdError, SdResult};
use sd_core::gate_policy::classify_reasons;
use sd_core::policy;
use sd_core::security::{provider_failed, sanitize_data};

use crate::atomic_write::atomic_write_bytes;
use crate::legacy_json::{canonical_bytes, canonical_sha256, readable_payload, sha256_hex};
use crate::path_guard::{safe_output_destination, validate_path_component};
use crate::snapshot::DOMAINS;

/// The fixed-policy domains, in legacy order.
const DOMAIN_ORDER: [&str; 9] = DOMAINS;

fn market_zone(market: &str) -> SdResult<Tz> {
    policy::exchange_timezone(market)
        .and_then(|zone| zone.parse().ok())
        .ok_or_else(|| SdError::validation(format!("unsupported market {market}")))
}

fn date_only(value: &Value) -> SdResult<NaiveDate> {
    let text = py_str(value);
    let trimmed = text.trim();
    if let Some(position) = trimmed.find('T') {
        return Ok(parse_timestamp(&Value::String(trimmed[..position].to_string()))?.date_naive());
    }
    policy::parse_iso_date(trimmed.get(..10).unwrap_or(trimmed))
}

fn array(value: Option<&Value>) -> &[Value] {
    value
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

/// Unwrap one gateway response into a normalized domain block (`_unwrap_domain`).
///
/// The original envelope stays in the block; only the documented `data` field is
/// unwrapped. Key order matches the Python dict merge: copied keys keep their
/// position and `data`/`status`/`actual_source` are appended in that order when new.
pub fn unwrap_domain(block: Option<&Value>) -> Value {
    let Some(Value::Object(block)) = block else {
        let mut map = Map::new();
        map.insert("status".into(), Value::String("unavailable".into()));
        map.insert("data".into(), Value::Null);
        map.insert("actual_source".into(), Value::Null);
        return Value::Object(map);
    };
    let mut output = block.clone();
    let mut data = block.get("data").cloned();
    if data.as_ref().map(Value::is_null).unwrap_or(true) {
        if let Some(payload) = block.get("payload") {
            data = Some(payload.clone());
        }
    }
    let failed = provider_failed(&Value::Object(block.clone()));
    let status = if failed {
        "failed".to_string()
    } else {
        match block.get("status") {
            Some(Value::String(text)) => text.clone(),
            _ if data.is_some() && !data.as_ref().map(Value::is_null).unwrap_or(true) => {
                "complete".to_string()
            }
            _ => "unavailable".to_string(),
        }
    };
    output.insert(
        "data".into(),
        if failed {
            Value::Null
        } else {
            data.unwrap_or(Value::Null)
        },
    );
    output.insert("status".into(), Value::String(status));
    let actual_source = block
        .get("actual_source")
        .cloned()
        .or_else(|| block.get("source").cloned())
        .unwrap_or(Value::Null);
    output.insert("actual_source".into(), actual_source);
    Value::Object(output)
}

/// Validate the frozen collection shape and bar axis (`validate_collection`).
///
/// Returns `(errors, warnings, kept_bar_count)`.
pub fn validate_collection(collection: &Value, request: &Value) -> (Vec<String>, Vec<String>, i64) {
    let mut errors: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    let market = unwrap_domain(collection.get("market"));
    let Some(market_object) = market.as_object() else {
        return (
            vec!["market OHLCV collection is missing or failed".into()],
            warnings,
            0,
        );
    };
    let data = market_object.get("data").and_then(Value::as_object);
    let failed = provider_failed(&market);
    let status = market_object
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unavailable");
    if failed || matches!(status, "unavailable" | "failed" | "invalid" | "empty") || data.is_none()
    {
        return (
            vec!["market OHLCV collection is missing or failed".into()],
            warnings,
            0,
        );
    }
    let Some(data) = data else {
        return (
            vec!["market OHLCV collection is missing or failed".into()],
            warnings,
            0,
        );
    };
    let bars = data.get("bars").and_then(Value::as_array);
    let Some(bars) = bars.filter(|bars| !bars.is_empty()) else {
        return (
            vec!["market.data.bars must be a non-empty normalized OHLCV array".into()],
            warnings,
            0,
        );
    };
    let asof = request.get("asof");
    let asof_day = match asof {
        None | Some(Value::Null) => None,
        Some(value) => match parse_timestamp(value) {
            Ok(instant) => {
                let market_text = request
                    .get("market")
                    .and_then(Value::as_str)
                    .unwrap_or("US");
                match market_zone(market_text) {
                    Ok(zone) => Some(instant.with_timezone(&zone).date_naive()),
                    Err(_) => None,
                }
            }
            Err(_) => None,
        },
    };
    let mut previous_day: Option<NaiveDate> = None;
    let mut seen: Vec<NaiveDate> = Vec::new();
    let mut kept: i64 = 0;
    for (index, bar) in bars.iter().enumerate() {
        let Some(bar) = bar.as_object() else {
            errors.push(format!("market bar {index} is not an object"));
            continue;
        };
        let Ok(bar_day) = bar
            .get("date")
            .map(date_only)
            .unwrap_or(Err(SdError::validation("invalid date")))
        else {
            errors.push(format!("market bar {index} has an invalid date"));
            continue;
        };
        if seen.contains(&bar_day) {
            errors.push(format!(
                "duplicate market session: {}",
                policy::iso_date(bar_day)
            ));
        }
        if let Some(previous_day) = previous_day {
            if bar_day <= previous_day {
                errors.push("market bars must be strictly sorted by ascending session date".into());
            }
        }
        seen.push(bar_day);
        previous_day = Some(bar_day);
        if let Some(asof_day) = asof_day {
            if bar_day > asof_day {
                errors.push(format!(
                    "market bar {} occurs after the requested as-of date",
                    policy::iso_date(bar_day)
                ));
            }
        }
        let mut values: Vec<(&str, f64)> = Vec::new();
        for field in ["open", "high", "low", "close", "volume"] {
            let raw = bar.get(field);
            if matches!(raw, Some(Value::Bool(_))) {
                errors.push(format!(
                    "market bar {} is missing numeric {field}",
                    policy::iso_date(bar_day)
                ));
                continue;
            }
            let number = match raw {
                Some(Value::Number(number)) => number.as_f64(),
                Some(Value::String(text)) => text.trim().parse::<f64>().ok(),
                _ => None,
            };
            let Some(number) = number else {
                errors.push(format!(
                    "market bar {} is missing numeric {field}",
                    policy::iso_date(bar_day)
                ));
                continue;
            };
            if !number.is_finite() {
                errors.push(format!(
                    "market bar {} has non-finite {field}",
                    policy::iso_date(bar_day)
                ));
            } else if field != "volume" && number <= 0.0 {
                errors.push(format!(
                    "market bar {} has non-positive {field}",
                    policy::iso_date(bar_day)
                ));
            } else if field == "volume" && number < 0.0 {
                errors.push(format!(
                    "market bar {} has negative volume",
                    policy::iso_date(bar_day)
                ));
            }
            values.push((field, number));
        }
        if values.len() == 5 {
            let get = |name: &str| {
                values
                    .iter()
                    .find(|(field, _)| *field == name)
                    .map(|(_, value)| *value)
                    .unwrap_or(0.0)
            };
            let (open, high, low, close) = (get("open"), get("high"), get("low"), get("close"));
            if high < open.max(close).max(low) {
                errors.push(format!(
                    "market bar {} violates high >= open/close/low",
                    policy::iso_date(bar_day)
                ));
            }
            if low > open.min(close).min(high) {
                errors.push(format!(
                    "market bar {} violates low <= open/close/high",
                    policy::iso_date(bar_day)
                ));
            }
        }
        if asof_day.map(|asof_day| bar_day <= asof_day).unwrap_or(true) {
            kept += 1;
        }
    }
    // Collection-level warnings, in the legacy order: adjustment convention, staleness,
    // bar availability, then per-domain status and each block's own warnings.
    let adjustment = market_object.get("adjustment").and_then(Value::as_str);
    if !matches!(adjustment, Some(value) if sd_core::data_checks::ADJUSTMENTS.contains(&value)) {
        warnings.push("OHLCV adjustment convention is missing or unrecognized".into());
    }
    if let (Some(asof_day), Some(latest)) = (asof_day, seen.iter().max()) {
        let age = (asof_day - *latest).num_days();
        if age > 7 {
            warnings.push(format!(
                "latest market session is {age} calendar days before as-of; market history may be stale"
            ));
        }
    }
    if kept < 120 {
        warnings.push(format!(
            "only {kept} daily bars are available; statistical research may be insufficient"
        ));
    }
    for name in DOMAIN_ORDER.iter().skip(1) {
        let block = unwrap_domain(collection.get(*name));
        let status = block
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unavailable");
        if matches!(status, "unavailable" | "failed" | "partial") {
            warnings.push(format!("{name} domain status is {status}"));
        }
    }
    if let Some(collection_object) = collection.as_object() {
        for block in collection_object.values() {
            if let Some(rows) = block.get("warnings").and_then(Value::as_array) {
                warnings.extend(rows.iter().map(sd_core::contracts::py_str));
            }
        }
    }
    (errors, warnings, kept)
}

/// One gate pass over the normalized domains (`_derived_metadata`).
///
/// Mutates the market `data.policy_window` with the recomputed coverage block before
/// the caller decides whether to keep the result, exactly like the Python helper.
pub fn derived_metadata(
    domains: &mut BTreeMap<String, Value>,
    request: &Value,
    captured_at: Option<&str>,
    legacy: bool,
    resolved_calendar_evidence: Option<&CalendarResource>,
    allow_library_calendar: bool,
    library_calendar: Option<&CalendarResource>,
) -> SdResult<Value> {
    let collection = Value::Object(
        domains
            .iter()
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect(),
    );
    let (mut errors, mut warnings, count) = validate_collection(&collection, request);
    let market = domains.get("market").cloned().unwrap_or(Value::Null);
    let options = CheckOptions {
        captured_at,
        resolved_calendar_evidence,
        library_calendar,
        allow_library_calendar,
        enforce_policy: !legacy,
    };
    let mut gate = check_market(&market, request, &options)?;
    if let Some(policy_block) = gate
        .get("policy")
        .filter(|value| value.as_object().is_some())
    {
        if policy_block
            .get("applicable")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            let market_entry = domains
                .entry("market".to_string())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Value::Object(market_object) = market_entry {
                let data = market_object
                    .entry("data")
                    .or_insert_with(|| Value::Object(Map::new()));
                if let Value::Object(data_object) = data {
                    let window = data_object
                        .entry("policy_window")
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Value::Object(window_object) = window {
                        for key in [
                            "expected_sessions",
                            "observed_sessions",
                            "missing_sessions",
                            "missing_session_dates",
                            "coverage_status",
                            "coverage_basis",
                            "coverage_recomputed",
                            "calendar_verified",
                            "declared_coverage_status",
                            "calendar_source",
                        ] {
                            if let Some(value) = policy_block.get(key) {
                                window_object.insert(key.to_string(), value.clone());
                            }
                        }
                    }
                }
            }
        }
    }
    let gate_errors: Vec<String> = gate
        .get("errors")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    if !errors.is_empty() || !gate_errors.is_empty() {
        let mut combined: Vec<String> = std::mem::take(&mut errors);
        combined.extend(gate_errors);
        combined.sort();
        combined.dedup();
        return Err(SdError::validation(format!(
            "snapshot rejected: {}",
            combined.join("; ")
        )));
    }
    if legacy {
        let reason_codes = gate
            .get("reason_codes")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(|row| row.as_str().map(str::to_string))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let mut codes = reason_codes.clone();
        codes.push("SNAPSHOT_IDENTITY_INVALID".to_string());
        let classification = classify_reasons(codes.clone());
        let mut eligibility = Map::new();
        eligibility.insert("status".into(), Value::String("blocked".into()));
        eligibility.insert(
            "blockers".into(),
            Value::Array(
                classification
                    .blockers
                    .iter()
                    .map(|code| Value::String(code.clone()))
                    .collect(),
            ),
        );
        let mut reporting = Map::new();
        reporting.insert("status".into(), Value::String("veto".into()));
        reporting.insert(
            "warnings".into(),
            Value::Array(
                classification
                    .warnings
                    .iter()
                    .map(|code| Value::String(code.clone()))
                    .collect(),
            ),
        );
        if let Value::Object(gate_object) = &mut gate {
            gate_object.insert(
                "reason_codes".into(),
                Value::Array(
                    codes
                        .iter()
                        .map(|code| Value::String(code.clone()))
                        .collect(),
                ),
            );
            gate_object.insert(
                "blockers".into(),
                Value::Array(
                    classification
                        .blockers
                        .iter()
                        .map(|code| Value::String(code.clone()))
                        .collect(),
                ),
            );
            gate_object.insert("prediction_eligibility".into(), Value::Object(eligibility));
            gate_object.insert("reporting_eligibility".into(), Value::Object(reporting));
            gate_object.insert("reporting_status".into(), Value::String("veto".into()));
            gate_object.insert("forecast_eligible".into(), Value::Bool(false));
        }
    }
    let gate_blockers: Vec<String> = gate
        .get("blockers")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    let gate_warnings: Vec<String> = gate
        .get("warnings")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    warnings.extend(gate_blockers);
    warnings.extend(gate_warnings);
    warnings.sort();
    warnings.dedup();

    let mut statuses = Map::new();
    let mut sources = Map::new();
    for name in DOMAIN_ORDER {
        let block = domains.get(name).cloned().unwrap_or(Value::Null);
        statuses.insert(
            name.to_string(),
            Value::String(
                block
                    .get("status")
                    .map(py_str)
                    .unwrap_or_else(|| "unavailable".to_string()),
            ),
        );
        let mut source = Map::new();
        for key in [
            "actual_source",
            "source_timestamp",
            "currency",
            "unit",
            "adjustment",
            "price_basis",
            "price_basis_source",
            "adjustment_evidence",
            "session_calendar",
            "latest_price_observations",
            "fallback_used",
            "fallback_reason",
        ] {
            source.insert(
                key.to_string(),
                block.get(key).cloned().unwrap_or(Value::Null),
            );
        }
        let fetched = block
            .get("fetched_at_utc")
            .cloned()
            .or_else(|| block.get("fetched_at").cloned())
            .unwrap_or(Value::Null);
        source.insert("fetched_at".into(), fetched);
        sources.insert(name.to_string(), Value::Object(source));
    }
    let policy_window = domains
        .get("market")
        .and_then(|market| market.get("data"))
        .and_then(|data| data.get("policy_window"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let expected_sessions = policy_window
        .get("expected_sessions")
        .and_then(Value::as_i64);
    let observed_sessions = policy_window
        .get("observed_sessions")
        .and_then(Value::as_i64)
        .unwrap_or(count);
    let coverage_ratio = match expected_sessions {
        Some(expected) if expected > 0 => Some(python_round4(
            (observed_sessions as f64 / expected as f64).min(1.0),
        )),
        _ => None,
    };
    let forecast_eligible = gate
        .get("forecast_eligible")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let quality = if !forecast_eligible {
        "blocked"
    } else if !gate_warnings_empty(&gate)
        || coverage_ratio.map(|ratio| ratio < 1.0).unwrap_or(false)
    {
        "partial"
    } else {
        "complete"
    };
    let mut data_quality = Map::new();
    data_quality.insert(
        "schema".into(),
        Value::String("data_completeness_v2".into()),
    );
    data_quality.insert("overall".into(), Value::String(quality.into()));
    data_quality.insert("score".into(), Value::Null);
    data_quality.insert("score_deprecated".into(), Value::Bool(true));
    data_quality.insert(
        "deprecated_fields".into(),
        Value::Array(
            [
                "score",
                "confidence_score",
                "confidence_penalties",
                "overall_quality_labels",
            ]
            .iter()
            .map(|name| Value::String((*name).into()))
            .collect(),
        ),
    );
    let mut components = Map::new();
    components.insert("observed_sessions".into(), Value::from(observed_sessions));
    components.insert(
        "expected_sessions".into(),
        expected_sessions.map(Value::from).unwrap_or(Value::Null),
    );
    components.insert(
        "coverage_ratio".into(),
        coverage_ratio.map(Value::from).unwrap_or(Value::Null),
    );
    components.insert("prediction_eligible".into(), Value::Bool(forecast_eligible));
    components.insert(
        "required_domains".into(),
        Value::Array(vec![Value::String("market".into())]),
    );
    data_quality.insert("components".into(), Value::Object(components));
    data_quality.insert(
        "issues".into(),
        Value::Array(
            warnings
                .iter()
                .map(|item| Value::String(item.clone()))
                .collect(),
        ),
    );
    data_quality.insert(
        "reason_codes".into(),
        gate.get("reason_codes")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
    );

    let mut derived = Map::new();
    derived.insert("market_bar_count".into(), Value::from(count));
    derived.insert("data_domains".into(), Value::Object(statuses));
    derived.insert("sources".into(), Value::Object(sources));
    derived.insert("data_validation".into(), gate.clone());
    derived.insert(
        "latest_confirmed_close".into(),
        gate.get("latest_confirmed_close")
            .cloned()
            .unwrap_or(Value::Null),
    );
    derived.insert(
        "warnings".into(),
        Value::Array(warnings.into_iter().map(Value::String).collect()),
    );
    derived.insert("data_quality".into(), Value::Object(data_quality));
    Ok(Value::Object(derived))
}

fn gate_warnings_empty(gate: &Value) -> bool {
    gate.get("warnings")
        .and_then(Value::as_array)
        .map(Vec::is_empty)
        .unwrap_or(true)
}

fn python_round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// Freeze options.
pub struct FreezeOptions<'a> {
    /// Directory that will contain the snapshot directory.
    pub output_root: &'a Path,
    /// Verified schedule used when the collection carries no source calendar.
    pub library_calendar: Option<&'a CalendarResource>,
    /// Whether the freeze path may use [`Self::library_calendar`].
    pub allow_library_calendar: bool,
    /// Optional callback invoked with the created directory.
    pub on_snapshot_created: Option<&'a dyn Fn(&Path)>,
}

/// Freeze a sanitized-capable receipt into a validated snapshot directory.
pub fn freeze_snapshot(
    payload: &Value,
    options: &FreezeOptions<'_>,
    clock: &dyn Clock,
) -> SdResult<PathBuf> {
    let payload = sanitize_data(payload);
    let Value::Object(payload_object) = &payload else {
        return Err(SdError::validation(
            "input must be an object containing a request object",
        ));
    };
    let Some(request_value) = payload_object
        .get("request")
        .filter(|value| value.is_object())
    else {
        return Err(SdError::validation(
            "input must be an object containing a request object",
        ));
    };
    let normalized_request = normalize_request(request_value)?;
    let mut request = normalized_request.to_json();
    let Some(collection) = payload_object
        .get("domains")
        .filter(|value| value.is_object())
    else {
        return Err(SdError::validation(
            "input.domains must contain normalized stock-data domain responses",
        ));
    };
    let asof_source = request
        .get("asof")
        .filter(|value| !value.is_null())
        .cloned()
        .or_else(|| payload_object.get("captured_at_utc").cloned())
        .or_else(|| payload_object.get("asof_timestamp").cloned());
    let Some(asof_source) = asof_source.filter(|value| !value.is_null()) else {
        return Err(SdError::validation(
            "provide request.asof or captured_at_utc with an explicit timezone",
        ));
    };
    let asof_timestamp = sd_core::clock::format_utc_timestamp(parse_timestamp(&asof_source)?);
    if let Value::Object(object) = &mut request {
        object.insert("asof".into(), Value::String(asof_timestamp.clone()));
    }
    let (errors, mut warnings, mut bar_count) = validate_collection(collection, &request);
    if !errors.is_empty() {
        return Err(SdError::validation(format!(
            "snapshot rejected: {}",
            errors.join("; ")
        )));
    }

    // A daily bar dated on the as-of day may still be forming; unless the gateway
    // confirms the close, keep it in raw/ but exclude it from normalized data.
    let mut normalized_market = unwrap_domain(collection.get("market"));
    if let Some(market_object) = normalized_market.as_object().cloned() {
        let market_data = market_object
            .get("data")
            .and_then(Value::as_object)
            .cloned();
        if let Some(market_data) = market_data {
            if let Some(bars) = market_data.get("bars").and_then(Value::as_array).cloned() {
                let market_text = request
                    .get("market")
                    .and_then(Value::as_str)
                    .unwrap_or("US");
                let zone = market_zone(market_text)?;
                let asof_day = parse_timestamp(&Value::String(asof_timestamp.clone()))?
                    .with_timezone(&zone)
                    .date_naive();
                let sessions = if market_object.get("session_calendar").is_some() {
                    match CalendarResource::from_snapshot_calendar(
                        market_object
                            .get("session_calendar")
                            .unwrap_or(&Value::Null),
                        market_text,
                    ) {
                        Ok(resource) => resource.sessions,
                        Err(_) => Vec::new(),
                    }
                } else {
                    Vec::new()
                };
                let last_day = bars
                    .last()
                    .and_then(|bar| bar.get("date"))
                    .map(date_only)
                    .transpose()?;
                let close_row = last_day.and_then(|last_day| {
                    sessions
                        .iter()
                        .find(|session| session.date == policy::iso_date(last_day))
                        .cloned()
                });
                // A-CAL-01: the bar is confirmed complete at the session's sourced
                // completion boundary, not at the continuous close. A session without a
                // verified completion boundary cannot confirm the bar; the conservative
                // same-as-of fallback below then applies.
                let completion = close_row
                    .as_ref()
                    .map(|row| row.parsed_complete())
                    .transpose()?
                    .flatten();
                let exclude = if bars.is_empty() {
                    false
                } else if let (Some(complete), Some(asof)) = (
                    completion,
                    Some(parse_timestamp(&Value::String(asof_timestamp.clone()))?),
                ) {
                    complete > asof
                } else {
                    last_day == Some(asof_day)
                        && market_object.get("last_bar_closed") != Some(&Value::Bool(true))
                };
                if exclude && request.get("intent").and_then(Value::as_str) == Some("forecast") {
                    return Err(SdError::validation(
                        "snapshot rejected: the decision-session bar is not confirmed closed; fixed-policy forecasts must be created after the exchange completion boundary",
                    ));
                }
                if exclude {
                    let mut updated_market =
                        normalized_market.as_object().cloned().unwrap_or_default();
                    let mut updated_data = market_data.clone();
                    let trimmed = bars[..bars.len() - 1].to_vec();
                    updated_data.insert("bars".into(), Value::Array(trimmed.clone()));
                    if let Some(window) = updated_data
                        .get("history_window")
                        .and_then(Value::as_object)
                        .cloned()
                    {
                        if !trimmed.is_empty() {
                            let end = trimmed
                                .last()
                                .and_then(|bar| bar.get("date"))
                                .cloned()
                                .unwrap_or(Value::Null);
                            let mut updated_window = window;
                            updated_window.insert("end".into(), end);
                            updated_data
                                .insert("history_window".into(), Value::Object(updated_window));
                        }
                    }
                    updated_market.insert("data".into(), Value::Object(updated_data));
                    let mut market_warnings = updated_market
                        .get("warnings")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    market_warnings.push(Value::String(
                        "excluded same-as-of-date daily bar because the source did not confirm that the session was closed"
                            .into(),
                    ));
                    updated_market.insert("warnings".into(), Value::Array(market_warnings));
                    normalized_market = Value::Object(updated_market);
                    warnings.push(
                        "latest daily bar was excluded because the gateway did not confirm that its as-of-date session was closed"
                            .into(),
                    );
                    bar_count -= 1;
                    if bar_count <= 0 {
                        return Err(SdError::validation(
                            "snapshot rejected: no closed market bars remain after excluding an unverified same-day bar",
                        ));
                    }
                }
            }
        }
    }

    let created_at = clock.utc_now();
    if let Some(Value::String(captured)) = payload_object.get("captured_at_utc") {
        if parse_timestamp(&Value::String(captured.clone()))?
            > parse_timestamp(&Value::String(created_at.clone()))?
        {
            return Err(SdError::validation(
                "collection capture occurs after snapshot creation",
            ));
        }
    }
    let source_digest = sha256_hex(&canonical_bytes(&payload)?);
    let stamp: String = created_at
        .chars()
        .filter(|character| !matches!(character, '-' | ':' | '.'))
        .collect();
    let ticker = request
        .get("ticker")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let slug: String = ticker
        .chars()
        .map(|character| {
            if character.is_ascii_uppercase()
                || character.is_ascii_digit()
                || matches!(character, '.' | '_' | '-')
            {
                character
            } else {
                '_'
            }
        })
        .collect();
    let horizon = request
        .get("horizon")
        .and_then(Value::as_str)
        .unwrap_or("5D");

    let mut normalized: BTreeMap<String, Value> = BTreeMap::new();
    for name in DOMAIN_ORDER {
        let block = if name == "market" {
            normalized_market.clone()
        } else {
            unwrap_domain(collection.get(name))
        };
        normalized.insert(name.to_string(), block);
    }
    let captured_at = payload_object
        .get("captured_at_utc")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut derived = derived_metadata(
        &mut normalized,
        &request,
        captured_at.as_deref(),
        false,
        None,
        options.allow_library_calendar,
        options.library_calendar,
    )?;
    let resolved = derived
        .get("data_validation")
        .and_then(|value| value.get("resolved_calendar_evidence"))
        .filter(|value| !value.is_null())
        .and_then(|value| {
            CalendarResource::from_snapshot_calendar(
                value,
                request
                    .get("market")
                    .and_then(Value::as_str)
                    .unwrap_or("US"),
            )
            .ok()
        });
    if let Some(resolved) = resolved.as_ref() {
        derived = derived_metadata(
            &mut normalized,
            &request,
            captured_at.as_deref(),
            false,
            Some(resolved),
            false,
            None,
        )?;
    }

    // Python shares the `data`/`policy_window` object between the normalized market
    // block and the original collection, so `_derived_metadata` also rewrites the raw
    // market block before `raw_domain_sha256s` is computed. Mirror that aliasing so the
    // raw digest matches, without mutating the caller's payload.
    let mut raw_market = collection.get("market").cloned().unwrap_or(Value::Null);
    let normalized_policy_window = normalized
        .get("market")
        .and_then(|value| value.get("data"))
        .and_then(|value| value.get("policy_window"))
        .cloned();
    if let Some(policy_window) = normalized_policy_window {
        if let Value::Object(market_object) = &mut raw_market {
            let data = market_object
                .entry("data")
                .or_insert_with(|| Value::Object(Map::new()));
            if let Value::Object(data_object) = data {
                data_object.insert("policy_window".into(), policy_window);
            }
        }
    }
    let mut normalized_hashes = Map::new();
    let mut raw_hashes = Map::new();
    for name in DOMAIN_ORDER {
        let block = normalized.get(name).cloned().unwrap_or(Value::Null);
        normalized_hashes.insert(name.to_string(), Value::String(canonical_sha256(&block)?));
        let raw = if name == "market" {
            raw_market.clone()
        } else {
            collection.get(name).cloned().unwrap_or(Value::Null)
        };
        raw_hashes.insert(name.to_string(), Value::String(canonical_sha256(&raw)?));
    }
    let domains_value = Value::Object(
        normalized
            .iter()
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect(),
    );
    let mut content = Map::new();
    content.insert("request".into(), request.clone());
    content.insert(
        "asof_timestamp".into(),
        Value::String(asof_timestamp.clone()),
    );
    content.insert("domains".into(), domains_value);
    let content_digest = canonical_sha256(&Value::Object(content))?;
    let snapshot_id = format!("{slug}_{stamp}_{horizon}_{}", &content_digest[..10]);
    let _ = validate_path_component(&snapshot_id, "snapshot_id")?;
    let destination = safe_output_destination(options.output_root, &snapshot_id)?;
    let output_root = destination
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| options.output_root.to_path_buf());
    std::fs::create_dir_all(&output_root).map_err(SdError::from)?;
    if destination.exists() || crate::path_guard::is_link(&destination) {
        return Err(SdError::io(
            "FileExistsError",
            format!(
                "refusing to overwrite frozen snapshot: {}",
                destination.display()
            ),
        ));
    }

    let mut manifest = Map::new();
    manifest.insert(
        "schema_version".into(),
        Value::String(sd_core::versions::SNAPSHOT_SCHEMA_VERSION.into()),
    );
    manifest.insert("snapshot_id".into(), Value::String(snapshot_id.clone()));
    manifest.insert("ticker".into(), Value::String(ticker.to_string()));
    manifest.insert(
        "market".into(),
        request.get("market").cloned().unwrap_or(Value::Null),
    );
    manifest.insert("horizon".into(), Value::String(horizon.to_string()));
    manifest.insert("request".into(), request.clone());
    manifest.insert(
        "asof_timestamp".into(),
        Value::String(asof_timestamp.clone()),
    );
    manifest.insert(
        "snapshot_created_at".into(),
        Value::String(created_at.clone()),
    );
    manifest.insert(
        "collection_captured_at".into(),
        payload_object
            .get("captured_at_utc")
            .cloned()
            .unwrap_or(Value::Null),
    );
    manifest.insert("source_payload_sha256".into(), Value::String(source_digest));
    manifest.insert(
        "snapshot_content_sha256".into(),
        Value::String(content_digest),
    );
    manifest.insert("domain_sha256s".into(), Value::Object(normalized_hashes));
    manifest.insert("raw_domain_sha256s".into(), Value::Object(raw_hashes));
    if request.get("intent").and_then(Value::as_str) == Some("forecast") {
        let market_policy = normalized
            .get("market")
            .and_then(|value| value.get("data"))
            .and_then(|value| value.get("policy_window"))
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let market_object = normalized.get("market").cloned().unwrap_or(Value::Null);
        let calendar = market_object
            .get("session_calendar")
            .filter(|value| value.is_object())
            .or_else(|| {
                market_object
                    .get("data")
                    .and_then(|value| value.get("session_calendar"))
                    .filter(|value| value.is_object())
            })
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let session_dates: Vec<String> = array(calendar.get("sessions"))
            .iter()
            .filter_map(|row| row.get("date").and_then(Value::as_str).map(str::to_string))
            .collect();
        let decision = market_policy
            .get("decision_session")
            .cloned()
            .unwrap_or(Value::Null);
        let target_session = decision
            .as_str()
            .and_then(|decision| session_dates.iter().position(|date| date == decision))
            .and_then(|index| {
                session_dates
                    .get(index + policy::FORECAST_HORIZON_SESSIONS as usize)
                    .cloned()
            })
            .map(Value::String)
            .unwrap_or(Value::Null);
        let mut policy_block = request
            .get("policy")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        policy_block.insert("decision_session".into(), decision);
        policy_block.insert("target_session".into(), target_session);
        for (key, value) in [
            (
                "window_start_exclusive",
                market_policy.get("window_start_exclusive"),
            ),
            (
                "window_end_inclusive",
                market_policy.get("window_end_inclusive"),
            ),
            ("expected_sessions", market_policy.get("expected_sessions")),
            ("observed_sessions", market_policy.get("observed_sessions")),
            ("missing_sessions", market_policy.get("missing_sessions")),
            (
                "missing_session_dates",
                market_policy.get("missing_session_dates"),
            ),
            (
                "declared_coverage_status",
                market_policy.get("declared_coverage_status"),
            ),
            ("coverage_status", market_policy.get("coverage_status")),
            ("coverage_basis", market_policy.get("coverage_basis")),
            (
                "coverage_recomputed",
                market_policy.get("coverage_recomputed"),
            ),
            ("calendar_verified", market_policy.get("calendar_verified")),
            ("calendar_source", market_policy.get("calendar_source")),
            ("price_basis", market_policy.get("price_basis")),
        ] {
            policy_block.insert(key.to_string(), value.cloned().unwrap_or(Value::Null));
        }
        manifest.insert("policy".into(), Value::Object(policy_block));
    }
    if let Value::Object(derived_object) = derived {
        for (key, value) in derived_object {
            manifest.insert(key, value);
        }
    }
    let manifest_digest = canonical_sha256(&Value::Object(manifest.clone()))?;
    manifest.insert("manifest_sha256".into(), Value::String(manifest_digest));

    // Build in a scratch directory that matches the cleanup ownership pattern
    // (`.<snapshot_id>.<8 chars>`), then rename atomically.
    let scratch_name = format!(".{snapshot_id}.{}", scratch_suffix());
    let temporary = output_root.join(scratch_name);
    let result = (|| -> SdResult<()> {
        std::fs::create_dir_all(&temporary).map_err(SdError::from)?;
        atomic_write_bytes(
            &temporary.join("manifest.json"),
            &platform_payload(&Value::Object(manifest.clone()))?,
        )?;
        std::fs::create_dir_all(temporary.join("raw")).map_err(SdError::from)?;
        for name in DOMAIN_ORDER {
            let block = normalized.get(name).cloned().unwrap_or(Value::Null);
            atomic_write_bytes(
                &temporary.join(format!("{name}.json")),
                &platform_payload(&block)?,
            )?;
            let raw = if name == "market" {
                raw_market.clone()
            } else {
                collection.get(name).cloned().unwrap_or(Value::Null)
            };
            atomic_write_bytes(
                &temporary.join("raw").join(format!("{name}.json")),
                &platform_payload(&raw)?,
            )?;
        }
        std::fs::rename(&temporary, &destination).map_err(SdError::from)
    })();
    if result.is_err() {
        let _ = std::fs::remove_dir_all(&temporary);
        return result.map(|_| destination);
    }
    if let Some(callback) = options.on_snapshot_created {
        callback(&destination);
    }
    Ok(destination)
}

/// Render a readable payload with the platform newline Python's text-mode
/// `write_text()` applies (`os.linesep`), so frozen bytes match on each platform.
fn platform_payload(value: &Value) -> SdResult<Vec<u8>> {
    let text = readable_payload(value)?;
    let rendered = if cfg!(windows) {
        text.replace('\n', "\r\n")
    } else {
        text
    };
    Ok(rendered.into_bytes())
}

fn scratch_suffix() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let digest = sha256_hex(&format!("{nanos}-{}", std::process::id()).into_bytes());
    digest[..8].to_string()
}
