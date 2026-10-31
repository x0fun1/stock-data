//! Native Yahoo market collector (`yahoo_collect.py`).
//!
//! Only normalized provider data is projected. Calendar evidence is optional: a supplied
//! `session_calendar` (source-backed, timezone-correct, ordered) is validated; without one
//! the decision session falls back to bar labels and is explicitly marked unverified.
//! Optional third-party exchange-calendar libraries cannot be part of a native binary, so
//! a missing calendar is normal (never synthesized).

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Duration, NaiveDate, Timelike, Utc};
use serde_json::{Map, Value};

use sd_core::contracts::{parse_timestamp_str, python_str};
use sd_core::policy::{
    window_bounds, HISTORY_CALENDAR_DAYS, PRICE_BASIS, PRICE_BASIS_NOTE, PRICE_BASIS_SOURCE,
};
use sd_core::security::{provider_failed, sanitize_data};

use crate::transport::ProviderError;

/// Provider abstraction used by the collector (implemented by the real Yahoo gateway or a
/// scripted fake in tests).
pub trait CollectProvider {
    /// One provider capability call; the returned envelope is already provider-shaped.
    fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, ProviderError>;
}

/// Exchange timezones for the two supported markets.
pub const EXCHANGE_TIMEZONES: [(&str, &str); 2] =
    [("US", "America/New_York"), ("HK", "Asia/Hong_Kong")];

/// Extra calendar days fetched so a decision session delayed by a long holiday still has a
/// complete 364-day window; bars outside the policy window are dropped and counted.
pub const FORECAST_FETCH_BUFFER_DAYS: i64 = 30;

/// Compute the policy version label used in collector metadata.
pub fn policy_version() -> &'static str {
    sd_core::policy::POLICY_VERSION
}

fn json_safe(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), json_safe(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(json_safe).collect()),
        other => other.clone(),
    }
}

fn date(text: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").ok()
}

fn iso_date(value: NaiveDate) -> String {
    value.format("%Y-%m-%d").to_string()
}

/// `_default_history_start`: the research baseline is 52 weeks, not one calendar year.
pub fn default_history_start(day: NaiveDate) -> NaiveDate {
    day - Duration::days(7 * 52)
}

fn exchange_zone(market: &str) -> Result<chrono_tz::Tz, String> {
    match EXCHANGE_TIMEZONES
        .iter()
        .find(|(name, _)| *name == market)
        .map(|(_, tz)| *tz)
    {
        Some("America/New_York") => Ok(chrono_tz::America::New_York),
        Some("Asia/Hong_Kong") => Ok(chrono_tz::Asia::Hong_Kong),
        _ => Err(format!("unsupported market {market}")),
    }
}

/// `_valid_calendar`: validate a supplied exchange schedule (shape, not authenticity).
pub fn valid_calendar(value: &Value, market: &str) -> Option<Value> {
    let calendar = value.as_object()?;
    let source = calendar.get("source")?.as_str()?;
    if source.is_empty() {
        return None;
    }
    let zone_name = calendar.get("timezone")?.as_str()?;
    let expected = EXCHANGE_TIMEZONES
        .iter()
        .find(|(name, _)| *name == market)
        .map(|(_, tz)| *tz)?;
    if zone_name != expected {
        return None;
    }
    let start = date(calendar.get("coverage_start")?.as_str()?)?;
    let end = date(calendar.get("coverage_end")?.as_str()?)?;
    if start > end {
        return None;
    }
    let sessions = calendar.get("sessions")?.as_array()?;
    if sessions.is_empty() {
        return None;
    }
    let zone = exchange_zone(market).ok()?;
    let mut previous: Option<NaiveDate> = None;
    for row in sessions {
        let day = date(row.get("date")?.as_str()?)?;
        let close = parse_timestamp_str(row.get("close_at")?.as_str()?).ok()?;
        let local = close.with_timezone(&zone);
        if local.date_naive() != day || day < start || day > end {
            return None;
        }
        // A-CAL-01 boundaries travel with the schedule; when present they must be
        // UTC instants on the session's local date and at/after the continuous close.
        for key in ["auction_end_at", "complete_at"] {
            if let Some(value) = row.get(key) {
                match value {
                    Value::Null => {}
                    Value::String(text) => {
                        let instant = parse_timestamp_str(text).ok()?;
                        if instant.with_timezone(&zone).date_naive() != day || instant < close {
                            return None;
                        }
                    }
                    _ => return None,
                }
            }
        }
        if let Some(previous) = previous {
            if day <= previous {
                return None;
            }
        }
        previous = Some(day);
    }
    Some(value.clone())
}

fn utc_now() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
}

/// `_call`: one provider call with every failure redacted into an envelope.
pub fn call(provider: &mut dyn CollectProvider, name: &str, params: &Map<String, Value>) -> Value {
    match provider.call(name, params) {
        Ok(result) if result.is_object() => result,
        Err(error) => serde_json::json!({
            "status": "error",
            "error": {"category": error.kind.error_type(),
                      "exception_type": error.kind.error_type(),
                      "message": "provider call failed; details redacted"},
        }),
        _ => serde_json::json!({
            "status": "error",
            "error": {"category": "InvalidResponse", "message": "provider returned a non-object"},
        }),
    }
}

const METADATA: [&str; 15] = [
    "currency",
    "unit",
    "frequency",
    "source_symbol",
    "source_timestamp",
    "source_timestamp_kind",
    "fetched_at_utc",
    "adjustment",
    "adjustment_evidence",
    "provider_library",
    "provider_version",
    "fallback_attempted",
    "fallback_used",
    "fallback_reason",
    "fallback_route",
];

/// `_block`: project provider envelope metadata into one collector block.
pub fn block(envelope: &Value) -> Map<String, Value> {
    let data = envelope
        .get("data")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let provenance = envelope
        .get("provenance")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut block = Map::new();
    for key in METADATA {
        if let Some(value) = envelope.get(key) {
            block.insert(key.to_string(), value.clone());
        } else if let Some(value) = data.get(key) {
            block.insert(key.to_string(), value.clone());
        } else if let Some(value) = provenance.get(key) {
            block.insert(key.to_string(), value.clone());
        } else if let Some(value) = envelope
            .get("history")
            .and_then(Value::as_object)
            .and_then(|history| history.get(key))
        {
            block.insert(key.to_string(), value.clone());
        } else if key == "provider_library" {
            if let Some(value) = provenance.get("library") {
                block.insert(key.to_string(), value.clone());
            }
        } else if key == "provider_version" {
            if let Some(value) = provenance.get("version") {
                block.insert(key.to_string(), value.clone());
            }
        }
    }
    block.insert(
        "actual_source".into(),
        Value::String("Yahoo Finance".into()),
    );
    block.insert("gateway_envelope".into(), envelope.clone());
    block.insert(
        "warnings".into(),
        envelope
            .get("warnings")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into(),
    );
    if !block.contains_key("fetched_at_utc") {
        block.insert("fetched_at_utc".into(), Value::String(utc_now()));
    }
    let failed = provider_failed(envelope);
    let status = envelope.get("status").and_then(Value::as_str).unwrap_or("");
    if failed {
        block.insert("status".into(), Value::String("failed".into()));
        block.insert("data".into(), Value::Null);
    } else if status.eq_ignore_ascii_case("empty") || status.eq_ignore_ascii_case("unavailable") {
        block.insert("status".into(), Value::String("unavailable".into()));
        block.insert("data".into(), Value::Null);
    } else {
        block.insert(
            "status".into(),
            Value::String(
                if status == "partial" {
                    "partial"
                } else {
                    "complete"
                }
                .into(),
            ),
        );
        block.insert("data".into(), Value::Object(data));
    }
    block
}

/// Confirmable-completion instant of one collector schedule row.
///
/// A-CAL-01: `complete_at` is authoritative when the row carries it. An explicit
/// `null` means the boundary is unverified, so the session is *not* confirmed (the
/// collector never adds an assumed offset to the continuous close). Pre-A-CAL-01
/// schedules carry only `close_at`; it remains the fallback so frozen fixtures and
/// already-written receipts replay exactly as they did.
fn session_complete_at(session: &Value) -> Option<DateTime<Utc>> {
    if let Some(value) = session.get("complete_at") {
        return match value {
            Value::Null => None,
            Value::String(text) => parse_timestamp_str(text).ok(),
            _ => None,
        };
    }
    session
        .get("close_at")
        .and_then(Value::as_str)
        .and_then(|text| parse_timestamp_str(text).ok())
}

fn closed_sessions(schedule: &Value, asof: DateTime<Utc>, local_day: &str) -> Vec<String> {
    schedule
        .get("sessions")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter(|row| {
                    row.get("date").and_then(Value::as_str).unwrap_or("") <= local_day
                        && session_complete_at(row)
                            .map(|complete| complete <= asof)
                            .unwrap_or(false)
                })
                .filter_map(|row| {
                    row.get("date")
                        .and_then(Value::as_str)
                        .map(ToString::to_string)
                })
                .collect()
        })
        .unwrap_or_default()
}

/// `_decision_session`: latest completed exchange session at/before the as-of instant.
pub fn decision_session(
    bars: &[Value],
    schedule: Option<&Value>,
    asof: DateTime<Utc>,
    market: &str,
) -> (Option<String>, &'static str) {
    let zone = match exchange_zone(market) {
        Ok(zone) => zone,
        Err(_) => return (None, "bar_labels_only"),
    };
    let local = asof.with_timezone(&zone);
    let local_day = iso_date(local.date_naive());
    if let Some(schedule) = schedule {
        let closed = closed_sessions(schedule, asof, &local_day);
        return (closed.last().cloned(), "exchange_calendar");
    }
    let mut labels: BTreeSet<String> = bars
        .iter()
        .filter_map(|row| row.get("date").and_then(Value::as_str))
        .filter(|label| *label <= local_day.as_str())
        .map(ToString::to_string)
        .collect();
    if labels.is_empty() {
        return (None, "bar_labels_only");
    }
    let latest = labels.iter().next_back().cloned().unwrap_or_default();
    if latest == local_day && (local.hour(), local.minute()) < (16, 0) {
        labels.remove(&latest);
    }
    (labels.iter().next_back().cloned(), "bar_labels_only")
}

/// `_policy_window`: 364-calendar-day left-open/right-closed window from the decision session.
pub fn policy_window(
    request: &Value,
    bars: &[Value],
    schedule: Option<&Value>,
    asof: DateTime<Utc>,
) -> Value {
    let market = request
        .get("market")
        .and_then(Value::as_str)
        .unwrap_or("US");
    let (decision, basis) = decision_session(bars, schedule, asof, market);
    let Some(decision) = decision else {
        return serde_json::json!({
            "policy_version": policy_version(),
            "status": "no_completed_session",
            "decision_session": Value::Null,
            "calendar_verified": schedule.is_some(),
        });
    };
    let Some(decision_date) = date(&decision) else {
        return serde_json::json!({
            "policy_version": policy_version(),
            "status": "no_completed_session",
            "decision_session": Value::Null,
            "calendar_verified": schedule.is_some(),
        });
    };
    let (mut start, mut end) = window_bounds(decision_date);
    let declared = request
        .get("history_window")
        .cloned()
        .unwrap_or(Value::Null);
    let declared_start = declared.get("start").and_then(Value::as_str);
    let declared_end = declared.get("end").and_then(Value::as_str);
    if declared_start.is_some() || declared_end.is_some() {
        if let Ok((declared_start, declared_end)) = sd_core::policy::validate_declared_window(
            declared_start,
            declared_end,
            decision_date,
            true,
        ) {
            start = declared_start;
            end = declared_end;
        }
    }
    serde_json::json!({
        "policy_version": policy_version(),
        "status": "resolved",
        "decision_session": decision,
        "decision_session_basis": basis,
        "window_start_exclusive": start,
        "window_end_inclusive": end,
        "calendar_verified": schedule.is_some(),
        "calendar_source": schedule
            .and_then(|schedule| schedule.get("source"))
            .and_then(Value::as_str)
            .unwrap_or("unverified_no_schedule"),
        "price_basis": PRICE_BASIS,
        "price_basis_source": PRICE_BASIS_SOURCE,
        "price_basis_note": PRICE_BASIS_NOTE,
    })
}

/// `_policy_coverage`: expected/observed/missing sessions for the resolved policy window.
pub fn policy_coverage(policy: &Value, schedule: Option<&Value>, kept: &[Value]) -> Value {
    let observed: Vec<String> = kept
        .iter()
        .filter_map(|row| {
            row.get("date")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect();
    if policy.get("status").and_then(Value::as_str) != Some("resolved") {
        return serde_json::json!({
            "coverage_status": "unavailable", "expected_sessions": Value::Null,
            "observed_sessions": observed.len(), "missing_sessions": Value::Null,
            "missing_session_count": Value::Null, "unexpected_sessions": [],
        });
    }
    let start = policy
        .get("window_start_exclusive")
        .and_then(Value::as_str)
        .unwrap_or("");
    let end = policy
        .get("window_end_inclusive")
        .and_then(Value::as_str)
        .unwrap_or("");
    let decision = policy
        .get("decision_session")
        .and_then(Value::as_str)
        .unwrap_or("");
    let Some(schedule) = schedule else {
        return serde_json::json!({
            "coverage_status": "unverified", "expected_sessions": Value::Null,
            "observed_sessions": observed.len(), "missing_sessions": Value::Null,
            "missing_session_count": Value::Null, "unexpected_sessions": [],
            "reason": "CALENDAR_UNVERIFIED",
        });
    };
    let sessions = schedule
        .get("sessions")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let expected: Vec<String> = sessions
        .iter()
        .filter_map(|row| row.get("date").and_then(Value::as_str))
        .filter(|day| start < *day && *day <= end)
        .map(ToString::to_string)
        .collect();
    let observed_set: BTreeSet<String> = observed.iter().cloned().collect();
    let expected_set: BTreeSet<String> = expected.iter().cloned().collect();
    let missing: Vec<Value> = expected
        .iter()
        .filter(|day| !observed_set.contains(*day))
        .map(|day| Value::String(day.clone()))
        .collect();
    let unexpected: Vec<Value> = observed
        .iter()
        .filter(|day| !expected_set.contains(*day))
        .map(|day| Value::String(day.clone()))
        .collect();
    let coverage_start = schedule
        .get("coverage_start")
        .and_then(Value::as_str)
        .unwrap_or("");
    let coverage_end = schedule
        .get("coverage_end")
        .and_then(Value::as_str)
        .unwrap_or("");
    let coverage_ok = missing.is_empty()
        && unexpected.is_empty()
        && observed.len() == expected.len()
        && coverage_start <= start
        && coverage_end >= end
        && observed_set.contains(decision);
    serde_json::json!({
        "coverage_status": if coverage_ok { "complete" } else { "incomplete" },
        "expected_sessions": expected.len(),
        "observed_sessions": observed.len(),
        "missing_sessions": missing,
        "missing_session_count": missing.len(),
        "unexpected_sessions": unexpected,
        "calendar_coverage_start": schedule.get("coverage_start").cloned().unwrap_or(Value::Null),
        "calendar_coverage_end": schedule.get("coverage_end").cloned().unwrap_or(Value::Null),
        "reason": if coverage_ok { Value::Null } else { Value::String("INSUFFICIENT_HISTORY_COVERAGE".into()) },
    })
}

fn parse_asof(request: &Value) -> Option<DateTime<Utc>> {
    request
        .get("asof")
        .and_then(Value::as_str)
        .and_then(|text| parse_timestamp_str(text).ok())
}

/// `_market`: one market block with policy window, coverage, exclusions and closure evidence.
pub fn market(
    envelope: &Value,
    request: &Value,
    start: &str,
    end: &str,
    supplied_schedule: Option<&Value>,
) -> Map<String, Value> {
    let mut block = block(envelope);
    let status = block.get("status").and_then(Value::as_str).unwrap_or("");
    if status == "failed" || status == "unavailable" {
        return block;
    }
    let Some(data) = block.get("data").and_then(Value::as_object).cloned() else {
        block.insert("status".into(), Value::String("failed".into()));
        block.insert("data".into(), Value::Null);
        push_warning(&mut block, "provider history lacks a normalized bars array");
        return block;
    };
    let mut data = data;
    let bars = match data.get("bars").and_then(Value::as_array).cloned() {
        Some(bars) => bars,
        None => {
            block.insert("status".into(), Value::String("failed".into()));
            block.insert("data".into(), Value::Null);
            push_warning(&mut block, "provider history lacks a normalized bars array");
            return block;
        }
    };
    let Some(asof) = parse_asof(request) else {
        block.insert("status".into(), Value::String("failed".into()));
        block.insert("data".into(), Value::Null);
        push_warning(&mut block, "request asof is invalid");
        return block;
    };
    let market_name = request
        .get("market")
        .and_then(Value::as_str)
        .unwrap_or("US");
    let schedule = supplied_schedule.cloned().or_else(|| {
        envelope
            .get("session_calendar")
            .or_else(|| data.get("session_calendar"))
            .and_then(|value| valid_calendar(value, market_name))
    });
    let sessions = schedule
        .as_ref()
        .map(|schedule| {
            schedule
                .get("sessions")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .filter_map(|row| {
                            row.get("date")
                                .and_then(Value::as_str)
                                .map(|day| (day.to_string(), row.clone()))
                        })
                        .collect::<BTreeMap<String, Value>>()
                })
                .unwrap_or_default()
        })
        .unwrap_or_default();
    if let Some(schedule) = &schedule {
        block.insert("session_calendar".into(), schedule.clone());
    }
    match block.get("adjustment").and_then(Value::as_str) {
        Some("provider") => {
            block.insert("price_basis".into(), Value::String(PRICE_BASIS.into()));
            block.insert(
                "price_basis_source".into(),
                Value::String(PRICE_BASIS_SOURCE.into()),
            );
            block.insert(
                "price_basis_note".into(),
                Value::String(PRICE_BASIS_NOTE.into()),
            );
        }
        Some("adjusted") => {
            block.insert(
                "price_basis".into(),
                Value::String("split_and_dividend_adjusted".into()),
            );
            block.insert(
                "price_basis_source".into(),
                Value::String("provider_adjusted_close_includes_dividends".into()),
            );
            block.insert(
                "price_basis_note".into(),
                Value::String(
                    "not the policy basis; dividends are reinvested in this series".into(),
                ),
            );
        }
        _ => {}
    }
    let is_forecast = request.get("intent").and_then(Value::as_str) == Some("forecast");
    let policy = if is_forecast {
        Some(policy_window(request, &bars, schedule.as_ref(), asof))
    } else {
        None
    };
    let mut start = start.to_string();
    let mut end = end.to_string();
    if let Some(policy) = &policy {
        if let Some(decision) = policy.get("decision_session").and_then(Value::as_str) {
            let _ = decision;
            start = policy
                .get("window_start_exclusive")
                .and_then(Value::as_str)
                .unwrap_or(&start)
                .to_string();
            end = policy
                .get("window_end_inclusive")
                .and_then(Value::as_str)
                .unwrap_or(&end)
                .to_string();
        }
        let mut policy_block = Map::new();
        policy_block.insert(
            "policy_version".into(),
            Value::String(policy_version().into()),
        );
        policy_block.insert(
            "decision_session".into(),
            policy
                .get("decision_session")
                .cloned()
                .unwrap_or(Value::Null),
        );
        policy_block.insert(
            "window_start_exclusive".into(),
            policy
                .get("window_start_exclusive")
                .cloned()
                .unwrap_or(Value::Null),
        );
        policy_block.insert(
            "window_end_inclusive".into(),
            policy
                .get("window_end_inclusive")
                .cloned()
                .unwrap_or(Value::Null),
        );
        policy_block.insert(
            "calendar_verified".into(),
            policy
                .get("calendar_verified")
                .cloned()
                .unwrap_or(Value::Bool(false)),
        );
        block.insert("policy".into(), Value::Object(policy_block));
    }
    let mut kept: Vec<Value> = Vec::new();
    let mut excluded: Vec<Value> = Vec::new();
    let mut invalid = false;
    for row in &bars {
        let day = match row.get("date").and_then(Value::as_str).and_then(date) {
            Some(day) => iso_date(day),
            None => {
                invalid = true;
                excluded.push(serde_json::json!({"reason": "invalid_session_date"}));
                continue;
            }
        };
        let mut reason: Option<&str> = None;
        let inside = if is_forecast {
            start < day && day <= end
        } else {
            start <= day && day <= end
        };
        if !inside {
            reason = Some("outside_requested_window");
        } else if let Some(session) = sessions.get(&day) {
            let not_closed = session_complete_at(session)
                .map(|complete| complete > asof)
                .unwrap_or(true);
            if not_closed {
                reason = Some("session_not_closed_at_asof");
            }
        }
        if reason.is_none() {
            if let Some(schedule) = &schedule {
                let within = schedule
                    .get("coverage_start")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    <= day.as_str()
                    && day.as_str()
                        <= schedule
                            .get("coverage_end")
                            .and_then(Value::as_str)
                            .unwrap_or("");
                if within && !sessions.contains_key(&day) {
                    reason = Some("not_an_exchange_session");
                    invalid = true;
                }
            }
        }
        match reason {
            Some(reason) => excluded.push(serde_json::json!({"date": day, "reason": reason})),
            None => kept.push(row.clone()),
        }
    }
    data.insert("bars".into(), Value::Array(kept.clone()));
    if let Some(end_date) = date(&end) {
        block.insert(
            "collection_provenance".into(),
            serde_json::json!({
                "requested_window": {"start": start, "end": end},
                "provider_end_exclusive": iso_date(end_date + Duration::days(1)),
                "excluded_bars": excluded,
                "asof": request.get("asof").cloned().unwrap_or(Value::Null),
                "archive_kind": "normalized provider result, not raw HTTP payload",
                "historical_vintage": "download-time revised series; not historical PIT vintage",
            }),
        );
    }
    if kept.is_empty() {
        block.insert("status".into(), Value::String("unavailable".into()));
        block.insert("data".into(), Value::Null);
        block.insert("last_bar_closed".into(), Value::Null);
        push_warning(
            &mut block,
            "no retained daily observations in requested window",
        );
        if let Some(policy) = &policy {
            let mut window = policy.as_object().cloned().unwrap_or_default();
            if let Some(coverage) = policy_coverage(policy, schedule.as_ref(), &kept).as_object() {
                for (key, value) in coverage {
                    window.insert(key.clone(), value.clone());
                }
            }
            data.insert("policy_window".into(), Value::Object(window));
            block.insert("policy_status".into(), Value::String("ineligible".into()));
        }
        return block;
    }
    let first = kept[0].get("date").cloned().unwrap_or(Value::Null);
    let last = kept
        .last()
        .and_then(|row| row.get("date"))
        .cloned()
        .unwrap_or(Value::Null);
    let last_day = last.as_str().unwrap_or("").to_string();
    if policy.is_none() {
        data.insert(
            "history_window".into(),
            serde_json::json!({
                "start": first, "end": last,
                "selection": if request.get("history_window").map(|value| !value.is_null()).unwrap_or(false) {
                    "explicit user window"
                } else {
                    "one year from latest calendar-confirmed close; calendar uncertainty declared"
                },
            }),
        );
    } else if let Some(policy) = &policy {
        let coverage = policy_coverage(policy, schedule.as_ref(), &kept);
        let mut window = policy.as_object().cloned().unwrap_or_default();
        if let Some(coverage) = coverage.as_object() {
            for (key, value) in coverage {
                window.insert(key.clone(), value.clone());
            }
        }
        let dropped = excluded
            .iter()
            .filter(|item| {
                item.get("reason").and_then(Value::as_str) == Some("outside_requested_window")
            })
            .count();
        window.insert("dropped_out_of_window_count".into(), Value::from(dropped));
        data.insert("policy_window".into(), Value::Object(window));
        data.insert(
            "history_window".into(),
            serde_json::json!({
                "start": policy.get("window_start_exclusive").cloned().unwrap_or(Value::String(start.clone())),
                "end": policy.get("window_end_inclusive").cloned().unwrap_or(Value::String(end.clone())),
                "selection": format!("{}: decision_session - {} calendar days, left-open right-closed",
                                     policy_version(), HISTORY_CALENDAR_DAYS),
                "observed_first": first,
                "observed_last": last,
            }),
        );
    }
    let frequency = data
        .get("frequency")
        .or_else(|| block.get("frequency"))
        .cloned()
        .unwrap_or(Value::String("1d".into()));
    data.insert("frequency".into(), frequency.clone());
    block.insert("frequency".into(), frequency);
    if let Some(source_symbol) = block.get("source_symbol").cloned() {
        if !source_symbol.is_null() {
            data.insert("symbol".into(), source_symbol);
        }
    } else if let Some(symbol) = data.get("symbol").cloned() {
        if !symbol.is_null() {
            block.insert("source_symbol".into(), symbol);
        }
    }
    let last_closed = sessions.get(&last_day).map(|session| {
        session_complete_at(session)
            .map(|complete| complete <= asof)
            .unwrap_or(false)
    });
    block.insert(
        "last_bar_closed".into(),
        last_closed.map(Value::Bool).unwrap_or(Value::Null),
    );
    if let Some(policy) = &policy {
        if sessions.is_empty() {
            let decision = policy.get("decision_session").and_then(Value::as_str);
            block.insert(
                "last_bar_closed".into(),
                Value::Bool(Some(last_day.as_str()) == decision),
            );
        }
    }
    if let Some(row) = kept.last() {
        let label = row
            .get("timestamp_utc")
            .or_else(|| row.get("timestamp"))
            .cloned();
        if let Some(label) = label {
            if label
                .as_str()
                .and_then(|text| parse_timestamp_str(text).ok())
                .is_some()
            {
                block.insert("source_timestamp".into(), label.clone());
                block.insert(
                    "source_timestamp_kind".into(),
                    Value::String("bar_label_not_trade_time".into()),
                );
                if let Some(provenance) = block
                    .get("collection_provenance")
                    .and_then(Value::as_object)
                    .cloned()
                {
                    let mut provenance = provenance;
                    provenance.insert(
                        "source_timestamp_kind".into(),
                        Value::String("bar_label_not_trade_time".into()),
                    );
                    block.insert("collection_provenance".into(), Value::Object(provenance));
                }
            } else {
                push_warning(&mut block, "last bar source timestamp is invalid");
            }
        } else if !excluded.is_empty() && block.get("source_timestamp").is_some() {
            let reuse = block
                .get("source_timestamp")
                .and_then(Value::as_str)
                .and_then(|text| parse_timestamp_str(text).ok());
            let zone = exchange_zone(market_name).ok();
            let keep = match (reuse, zone) {
                (Some(timestamp), Some(zone)) => {
                    let local = iso_date(timestamp.with_timezone(&zone).date_naive());
                    timestamp <= asof && local == last_day
                }
                _ => false,
            };
            if !keep {
                block.remove("source_timestamp");
            }
        }
    }
    let mut missing: Vec<String> = Vec::new();
    for key in [
        "currency",
        "unit",
        "source_symbol",
        "source_timestamp",
        "adjustment",
    ] {
        if block
            .get(key)
            .map(|value| value.is_null() || value.as_str() == Some(""))
            .unwrap_or(true)
        {
            missing.push(key.to_string());
        }
    }
    if let Some(policy) = &policy {
        let coverage = data
            .get("policy_window")
            .cloned()
            .unwrap_or_else(|| policy.clone());
        if policy.get("status").and_then(Value::as_str) != Some("resolved") {
            missing.push("completed decision session".into());
        }
        if coverage.get("coverage_status").and_then(Value::as_str) != Some("complete") {
            let reason = coverage
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("incomplete");
            missing.push(format!("complete 52-week session coverage ({reason})"));
        }
        if block.get("price_basis").and_then(Value::as_str) != Some(PRICE_BASIS) {
            missing.push("verified split-adjusted price basis".into());
        }
        if let Some(decision) = policy.get("decision_session").and_then(Value::as_str) {
            if last_day != decision {
                missing.push("decision session close bar".into());
            }
        }
        if block.get("last_bar_closed") != Some(&Value::Bool(true)) {
            missing.push("calendar-backed closure".into());
        }
        block.insert(
            "policy_status".into(),
            Value::String(
                if missing.is_empty() && !invalid {
                    "eligible"
                } else {
                    "ineligible"
                }
                .into(),
            ),
        );
    } else {
        if block
            .get("last_bar_closed")
            .map(Value::is_null)
            .unwrap_or(true)
        {
            missing.push("calendar-backed closure".into());
        }
        if let Some(schedule) = &schedule {
            let coverage_start = schedule
                .get("coverage_start")
                .and_then(Value::as_str)
                .unwrap_or("");
            let coverage_end = schedule
                .get("coverage_end")
                .and_then(Value::as_str)
                .unwrap_or("");
            if coverage_start > start.as_str() || coverage_end < end.as_str() {
                missing.push("calendar coverage of requested window".into());
            }
        }
        if !sessions.is_empty() {
            let expected: Vec<String> = sessions
                .iter()
                .filter(|(day, session)| {
                    start.as_str() <= day.as_str()
                        && day.as_str() <= end.as_str()
                        && session_complete_at(session)
                            .map(|complete| complete <= asof)
                            .unwrap_or(false)
                })
                .map(|(day, _)| day.clone())
                .collect();
            let observed: Vec<String> = kept
                .iter()
                .filter_map(|row| {
                    row.get("date")
                        .and_then(Value::as_str)
                        .map(ToString::to_string)
                })
                .collect();
            if expected != observed {
                missing.push("requested exchange session coverage".into());
            }
        }
    }
    if !missing.is_empty() || invalid {
        block.insert("status".into(), Value::String("partial".into()));
        let detail = if missing.is_empty() {
            "invalid session dates".to_string()
        } else {
            missing.join(", ")
        };
        push_warning(&mut block, &format!("Unverified/incomplete: {detail}"));
    }
    if data.get("bars").is_some() {
        block.insert("data".into(), Value::Object(data));
    }
    block
}

fn push_warning(block: &mut Map<String, Value>, warning: &str) {
    let mut warnings = block
        .get("warnings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    warnings.push(Value::String(warning.to_string()));
    block.insert("warnings".into(), Value::Array(warnings));
}

/// `_benchmark`: benchmark collection never fails the target; no symbol is substituted.
pub fn benchmark(
    provider: &mut dyn CollectProvider,
    symbol: &str,
    start: &str,
    end: &str,
) -> Value {
    let mut block = serde_json::json!({
        "symbol": symbol, "selection": "declared", "status": "unavailable",
        "bars": [], "adjustment": Value::Null, "source": Value::Null,
    });
    let end_date = match date(end) {
        Some(day) => day,
        None => return block,
    };
    let mut params = Map::new();
    params.insert("symbol".into(), Value::String(symbol.to_string()));
    params.insert("start".into(), Value::String(start.to_string()));
    params.insert(
        "end".into(),
        Value::String(iso_date(end_date + Duration::days(1))),
    );
    params.insert("interval".into(), Value::String("1d".into()));
    params.insert("price_basis".into(), Value::String("provider".into()));
    let envelope = call(provider, "yahoo_history", &params);
    if provider_failed(&envelope) {
        if let Some(target) = block.as_object_mut() {
            target.insert(
                "reason".into(),
                Value::String("provider_call_failed".into()),
            );
            target.insert(
                "error".into(),
                envelope.get("error").cloned().unwrap_or(Value::Null),
            );
        }
        return block;
    }
    let data = envelope.get("data").cloned().unwrap_or(Value::Null);
    let Some(raw) = data.get("bars").and_then(Value::as_array) else {
        if let Some(target) = block.as_object_mut() {
            target.insert("reason".into(), Value::String("missing_bars".into()));
        }
        return block;
    };
    let bars: Vec<Value> = raw
        .iter()
        .filter_map(|row| {
            let day_text = row.get("date").and_then(Value::as_str)?;
            let day = date(day_text.get(0..10).unwrap_or(day_text))?;
            let day = iso_date(day);
            if start <= day.as_str() && day.as_str() <= end {
                Some(row.clone())
            } else {
                None
            }
        })
        .collect();
    if bars.is_empty() {
        if let Some(target) = block.as_object_mut() {
            target.insert("reason".into(), Value::String("no_bars_in_window".into()));
        }
        return block;
    }
    let adjustment = data
        .get("adjustment")
        .or_else(|| envelope.get("adjustment"))
        .cloned()
        .unwrap_or(Value::Null);
    let status = if envelope.get("status").and_then(Value::as_str) == Some("partial") {
        "partial"
    } else {
        "complete"
    };
    if let Some(target) = block.as_object_mut() {
        target.insert("bars".into(), Value::Array(bars));
        target.insert("adjustment".into(), adjustment.clone());
        target.insert(
            "price_basis".into(),
            Value::String(
                if adjustment == Value::String("provider".into()) {
                    PRICE_BASIS
                } else {
                    "split_and_dividend_adjusted"
                }
                .into(),
            ),
        );
        target.insert(
            "source".into(),
            envelope
                .get("source")
                .or_else(|| data.get("source"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        target.insert("status".into(), Value::String(status.into()));
    }
    block
}

fn normalized_request(request: &Value) -> Result<Value, String> {
    let normalized =
        sd_core::contracts::normalize_request(request).map_err(|error| error.to_string())?;
    let mut map = Map::new();
    map.insert("ticker".into(), Value::String(normalized.ticker.clone()));
    map.insert("market".into(), Value::String(normalized.market.clone()));
    map.insert("horizon".into(), Value::String(normalized.horizon.clone()));
    map.insert("mode".into(), Value::String(normalized.mode.clone()));
    map.insert("intent".into(), Value::String(normalized.intent.clone()));
    map.insert(
        "asset_type".into(),
        Value::String(normalized.asset_type.clone()),
    );
    map.insert(
        "asof".into(),
        normalized
            .asof
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    map.insert(
        "history_window".into(),
        normalized.history_window.clone().unwrap_or(Value::Null),
    );
    map.insert(
        "instrument".into(),
        normalized.instrument.clone().unwrap_or(Value::Null),
    );
    Ok(Value::Object(map))
}

/// `collect`: collect the selected domains independently (news is never collected here).
#[allow(clippy::too_many_arguments)]
pub fn collect(
    request: &Value,
    domains: &[String],
    provider: &mut dyn CollectProvider,
    schedule_input: Option<&Value>,
    now: Option<DateTime<Utc>>,
    timeout: f64,
    deadline: f64,
    request_budget: u32,
) -> Result<Value, String> {
    if timeout <= 0.0 || deadline <= 0.0 || request_budget < 1 {
        return Err("network limits must be positive".into());
    }
    let mut normalized = normalized_request(request)?;
    if domains.is_empty() || domains.iter().any(|domain| domain != "market") {
        return Err("domains must select market".into());
    }
    let asof_text = normalized
        .get("asof")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| {
            let now = now.unwrap_or_else(Utc::now);
            now.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
        });
    if let Some(object) = normalized.as_object_mut() {
        object.insert("asof".into(), Value::String(asof_text.clone()));
    }
    let asof = parse_timestamp_str(&asof_text).map_err(|error| error.to_string())?;
    let market_name = normalized
        .get("market")
        .and_then(Value::as_str)
        .unwrap_or("US")
        .to_string();
    let zone = exchange_zone(&market_name)?;
    let local_day = asof.with_timezone(&zone).date_naive();
    let is_forecast = normalized.get("intent").and_then(Value::as_str) == Some("forecast");
    let window = normalized
        .get("history_window")
        .cloned()
        .unwrap_or(Value::Null);
    let mut end = window
        .get("end")
        .and_then(Value::as_str)
        .and_then(date)
        .map(|day| day.min(local_day))
        .unwrap_or(local_day);
    // A supplied schedule (from an optional exchange-calendar source) is validated exactly
    // like the legacy collector; without one the bar-label path declares itself unverified.
    let schedule: Option<Value> =
        schedule_input.and_then(|value| valid_calendar(value, &market_name));
    if window.get("end").and_then(Value::as_str).is_none() {
        if let Some(schedule_value) = &schedule {
            let mut closed: Vec<NaiveDate> = schedule_value
                .get("sessions")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .filter_map(|row| {
                            let day = date(row.get("date").and_then(Value::as_str)?)?;
                            let complete = session_complete_at(row)?;
                            (complete <= asof && day <= end).then_some(day)
                        })
                        .collect()
                })
                .unwrap_or_default();
            closed.sort();
            if let Some(last) = closed.last() {
                end = *last;
            }
        }
    }
    let start = match window.get("start").and_then(Value::as_str) {
        Some(start) => {
            date(start).ok_or_else(|| "requested history start is invalid".to_string())?
        }
        None if is_forecast => {
            end - Duration::days(HISTORY_CALENDAR_DAYS + FORECAST_FETCH_BUFFER_DAYS)
        }
        None => default_history_start(end),
    };
    if start > end {
        return Err("requested history starts after effective as-of end".into());
    }
    let aliases = normalized
        .get("instrument")
        .and_then(|instrument| instrument.get("provider_symbols"))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let ticker = normalized
        .get("ticker")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let symbol = aliases
        .get("Yahoo Finance")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or(ticker);
    let mut params = Map::new();
    params.insert("symbol".into(), Value::String(symbol));
    params.insert("start".into(), Value::String(iso_date(start)));
    params.insert(
        "end".into(),
        Value::String(iso_date(end + Duration::days(1))),
    );
    params.insert("interval".into(), Value::String("1d".into()));
    params.insert("price_basis".into(), Value::String("provider".into()));
    let envelope = call(provider, "yahoo_history", &params);
    let mut market_block = market(
        &envelope,
        &normalized,
        &iso_date(start),
        &iso_date(end),
        schedule.as_ref(),
    );
    let benchmark_symbol = normalized
        .get("instrument")
        .and_then(|instrument| instrument.get("benchmark"))
        .and_then(Value::as_str)
        .map(ToString::to_string);
    if let Some(benchmark_symbol) = &benchmark_symbol {
        let benchmark_block =
            benchmark(provider, benchmark_symbol, &iso_date(start), &iso_date(end));
        let mut data = market_block
            .get("data")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let mut benchmark_block = benchmark_block;
        if is_forecast {
            let policy_window = data
                .get("policy_window")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            if policy_window.get("status").and_then(Value::as_str) == Some("resolved") {
                let low = policy_window
                    .get("window_start_exclusive")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let high = policy_window
                    .get("window_end_inclusive")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if let Some(bars) = benchmark_block
                    .get("bars")
                    .and_then(Value::as_array)
                    .cloned()
                {
                    let kept: Vec<Value> = bars
                        .into_iter()
                        .filter(|row| {
                            row.get("date")
                                .and_then(Value::as_str)
                                .map(|day| low < day && day <= high)
                                .unwrap_or(false)
                        })
                        .collect();
                    benchmark_block["bars"] = Value::Array(kept);
                }
                benchmark_block["window_start_exclusive"] = Value::String(low.to_string());
                benchmark_block["window_end_inclusive"] = Value::String(high.to_string());
            }
        }
        data.insert("benchmark".into(), benchmark_block);
        market_block.insert("data".into(), Value::Object(data));
    }
    let mut result = Map::new();
    result.insert("market".into(), Value::Object(market_block));
    Ok(sanitize_data(&json_safe(&Value::Object(result))))
}

/// `write_responses`: serialize first, then create the destination exclusively.
pub fn write_responses(
    responses: &Value,
    path: &std::path::Path,
) -> Result<std::path::PathBuf, String> {
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&sanitize_data(&json_safe(responses)))
            .map_err(|error| error.to_string())?
    );
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::AlreadyExists {
                "FileExistsError: output already exists".to_string()
            } else {
                error.to_string()
            }
        })?;
    file.write_all(text.as_bytes())
        .map_err(|error| error.to_string())?;
    Ok(path.to_path_buf())
}

#[allow(dead_code)]
fn _keep(value: &Value) -> String {
    let mut keys: Vec<String> = value
        .as_object()
        .map(|map| map.keys().cloned().collect())
        .unwrap_or_default();
    keys.sort();
    python_str(value)
}
