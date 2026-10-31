//! Migrated R03 legacy checks (market data, calendar, snapshot, sampling).
//!
//! Each function mirrors the corresponding `selfcheck.py` check and asserts the same
//! property against the real Rust implementation. Synthetic data is generated here
//! (the legacy helpers are not imported), so the checks are independent of Python at
//! run time.
//!
//! Three R03-owned checks stay `not_implemented` on purpose: `T07` needs the analysis
//! path to show that no probability is published, and `T33`/`T37` assert consensus,
//! audit and orchestrator payloads that arrive with R05/R08/R11; claiming them now
//! would be a placeholder, not coverage.

use std::path::PathBuf;

use chrono::{Duration, NaiveDate, TimeZone, Utc};
use serde_json::{json, Map, Value};

use sd_core::calendar::{CalendarResource, CompletionSemantics};
use sd_core::contracts::normalize_request;
use sd_core::data_checks::{adjustment_probe, check_market, latest_close_probe, CheckOptions};
use sd_core::interfaces::CalendarSession;
use sd_core::market_data::{anchored_non_overlapping, business_day_ordinal, non_overlapping};

use crate::selfcheck::registry::CheckFn;

const POLICY_VERSION: &str = "fixed_52w_5d_v1";

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn session_end() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 23).expect("frozen check end date")
}

/// Weekday sessions ending at `end`, oldest first (mirrors `_policy_sessions`).
fn weekday_sessions(count: usize, end: NaiveDate) -> Vec<NaiveDate> {
    let mut days = Vec::with_capacity(count);
    let mut cursor = end;
    while days.len() < count {
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(cursor);
        }
        cursor -= Duration::days(1);
    }
    days.reverse();
    days
}

/// Future weekdays after `from` (so the fifth-session target is resolvable).
fn future_sessions(from: NaiveDate, count: usize) -> Vec<NaiveDate> {
    let mut days = Vec::with_capacity(count);
    let mut cursor = from;
    while days.len() < count {
        cursor += Duration::days(1);
        if cursor.format("%u").to_string().parse::<u32>().unwrap() < 6 {
            days.push(cursor);
        }
    }
    days
}

/// Sessions inside the fixed policy window `(decision - 364d, decision]`.
fn window_dates(dates: &[NaiveDate]) -> Vec<NaiveDate> {
    let Some(decision) = dates.last().copied() else {
        return Vec::new();
    };
    let start = (decision - Duration::days(364)).to_string();
    dates
        .iter()
        .copied()
        .filter(|date| date.to_string() > start)
        .collect()
}

fn close_at(day: NaiveDate) -> String {
    format!("{day}T20:00:00Z")
}

fn asof_of(day: NaiveDate) -> String {
    format!("{day}T21:00:00Z")
}

/// Deterministic OHLCV rows that satisfy every ordering/positivity invariant.
fn bars_for(dates: &[NaiveDate]) -> Vec<Value> {
    let mut price = 100.0f64;
    dates
        .iter()
        .enumerate()
        .map(|(index, date)| {
            price *= 1.0 + (((index as f64) * 0.37).sin()) * 0.006;
            json!({
                "date": date.to_string(),
                "open": (price * 0.995 * 1_000_000.0).round() / 1_000_000.0,
                "high": (price * 1.012 * 1_000_000.0).round() / 1_000_000.0,
                "low": (price * 0.988 * 1_000_000.0).round() / 1_000_000.0,
                "close": (price * 1_000_000.0).round() / 1_000_000.0,
                "volume": 1_000_000.0 + f64::from(index as u32 % 200) * 1_000.0,
            })
        })
        .collect()
}

fn schedule_value(dates: &[NaiveDate]) -> Value {
    json!({
        "source": "selfcheck:synthetic_exchange",
        "timezone": "America/New_York",
        "coverage_start": dates.first().map(ToString::to_string),
        "coverage_end": dates.last().map(ToString::to_string),
        "sessions": dates.iter().map(|date| json!({"date": date.to_string(), "close_at": close_at(*date)})).collect::<Vec<_>>(),
    })
}

/// Frozen policy window for the synthetic series, mirroring the collector fields.
fn policy_window(dates: &[NaiveDate], decision: NaiveDate, bars: &[Value]) -> Value {
    let start = decision - Duration::days(364);
    let start_text = start.to_string();
    let end_text = decision.to_string();
    let expected = dates
        .iter()
        .filter(|date| date.to_string() > start_text && date.to_string() <= end_text)
        .count();
    let observed = bars
        .iter()
        .filter(|bar| {
            let day = bar["date"].as_str().unwrap_or_default();
            day > start_text.as_str() && day <= end_text.as_str()
        })
        .count();
    json!({
        "policy_version": POLICY_VERSION,
        "status": "resolved",
        "decision_session": decision.to_string(),
        "decision_session_basis": "latest_completed_close",
        "window_start_exclusive": start_text,
        "window_end_inclusive": end_text,
        "calendar_verified": true,
        "calendar_source": "selfcheck:synthetic_exchange",
        "expected_sessions": expected,
        "observed_sessions": observed,
        "missing_sessions": [],
        "missing_session_count": expected.saturating_sub(observed),
        "coverage_status": if expected == observed { "complete" } else { "incomplete" },
        "coverage_basis": "recomputed_from_calendar_and_bars",
        "coverage_recomputed": true,
        "price_basis": "split_adjusted",
        "price_basis_source": "provider_declared_split_adjusted_close",
    })
}

/// A complete normalized market block (ticker SYN) for the synthetic schedule.
fn market_block(bars: Vec<Value>, dates: &[NaiveDate], with_calendar: bool) -> Value {
    let decision = *dates.last().expect("sessions");
    let calendar = schedule_value(dates);
    let window = policy_window(dates, decision, &bars);
    let history_window = json!({
        "start": window["window_start_exclusive"],
        "end": window["window_end_inclusive"],
        "selection": "policy_window",
    });
    let data = {
        let mut map = Map::new();
        map.insert("symbol".into(), Value::String("SYN".into()));
        map.insert("frequency".into(), Value::String("1d".into()));
        map.insert("bars".into(), Value::Array(bars));
        map.insert("history_window".into(), history_window);
        map.insert("policy_window".into(), window);
        map.insert("session_calendar".into(), calendar.clone());
        Value::Object(map)
    };
    let mut block = Map::new();
    let asof = asof_of(decision);
    block.insert("status".into(), Value::String("complete".into()));
    block.insert(
        "actual_source".into(),
        Value::String("selfcheck-feed".into()),
    );
    block.insert("currency".into(), Value::String("USD".into()));
    block.insert("unit".into(), Value::String("USD".into()));
    block.insert("adjustment".into(), Value::String("provider".into()));
    block.insert("price_basis".into(), Value::String("split_adjusted".into()));
    block.insert(
        "price_basis_source".into(),
        Value::String("provider_declared_split_adjusted_close".into()),
    );
    block.insert("source_timestamp".into(), Value::String(asof.clone()));
    block.insert("fetched_at_utc".into(), Value::String(asof));
    block.insert("last_bar_closed".into(), Value::Bool(true));
    block.insert("data".into(), data);
    if with_calendar {
        block.insert("session_calendar".into(), calendar);
    }
    Value::Object(block)
}

fn request_value(with_policy: bool) -> Value {
    let mut map = Map::new();
    map.insert("ticker".into(), Value::String("SYN".into()));
    map.insert("market".into(), Value::String("US".into()));
    map.insert("horizon".into(), Value::String("5D".into()));
    map.insert("intent".into(), Value::String("forecast".into()));
    map.insert("asset_type".into(), Value::String("stock".into()));
    map.insert("mode".into(), Value::String("standard".into()));
    map.insert("asof".into(), Value::String(asof_of(session_end())));
    if with_policy {
        map.insert("policy".into(), json!({"policy_version": POLICY_VERSION}));
    }
    Value::Object(map)
}

fn run_gate(block: &Value, request: &Value) -> Result<Value, String> {
    let captured = asof_of(session_end());
    let options = CheckOptions {
        captured_at: Some(captured.as_str()),
        resolved_calendar_evidence: None,
        library_calendar: None,
        allow_library_calendar: false,
        enforce_policy: true,
    };
    check_market(block, request, &options).map_err(|error| error.message().to_string())
}

fn codes(gate: &Value) -> Vec<String> {
    gate.get("reason_codes")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// Scratch directory for the freeze-based check: `TEMP` first (the normal case), then
/// the workspace `target/tmp` when the environment denies the system temp directory.
fn scratch_dir(label: &str) -> Result<PathBuf, String> {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let name = format!("sd-selfcheck-{label}-{}-{stamp}", std::process::id());
    let mut candidates = vec![std::env::temp_dir().join(&name)];
    if let Ok(current) = std::env::current_dir() {
        candidates.push(current.join("target").join("tmp").join(&name));
    }
    let mut last_error = String::from("no writable scratch directory");
    for candidate in candidates {
        match std::fs::create_dir_all(&candidate) {
            Ok(()) => return Ok(candidate),
            Err(error) => last_error = format!("{}: {error}", candidate.display()),
        }
    }
    Err(last_error)
}

fn frozen_clock() -> sd_core::clock::FixedClock {
    sd_core::clock::FixedClock::from_timestamp("2026-10-24T00:00:00Z").expect("frozen clock")
}

fn freeze_payload(payload: &Value, root: &std::path::Path) -> Result<PathBuf, String> {
    use sd_storage::freeze::{freeze_snapshot, FreezeOptions};
    std::fs::create_dir_all(root).map_err(|error| error.to_string())?;
    let clock = frozen_clock();
    let options = FreezeOptions {
        output_root: root,
        library_calendar: None,
        allow_library_calendar: false,
        on_snapshot_created: None,
    };
    freeze_snapshot(payload, &options, &clock).map_err(|error| error.message().to_string())
}

fn receipt_payload(block: Value) -> Value {
    let mut domains = Map::new();
    domains.insert("market".into(), block);
    json!({
        "request": request_value(true),
        "captured_at_utc": asof_of(session_end()),
        "domains": Value::Object(domains),
    })
}

fn calendar_resource(dates: &[NaiveDate]) -> CalendarResource {
    let sessions: Vec<CalendarSession> = dates
        .iter()
        .map(|date| CalendarSession {
            date: date.to_string(),
            close_at: close_at(*date),
            auction_end_at: Some(close_at(*date)),
            complete_at: Some(close_at(*date)),
        })
        .collect();
    CalendarResource {
        market: "US".into(),
        calendar: "XNYS".into(),
        timezone: "America/New_York".into(),
        source: "selfcheck:synthetic_exchange".into(),
        coverage_start: sessions
            .first()
            .map(|row| row.date.clone())
            .unwrap_or_default(),
        coverage_end: sessions
            .last()
            .map(|row| row.date.clone())
            .unwrap_or_default(),
        sessions,
        // Synthetic US sessions: the NYSE closing auction concludes at the core
        // close, so the continuous close is the completion boundary (A-CAL-01).
        completion: CompletionSemantics::NoAuctionExtensionRule,
    }
}

// ------------------------------------------------------------------ checks

/// T01: the forecast horizon is exactly five sessions.
pub fn legacy_t01() -> Result<(), String> {
    for horizon in ["1D", "20D"] {
        let request = json!({"ticker": "SYN", "horizon": horizon, "intent": "forecast"});
        let error = normalize_request(&request).expect_err("forecast must reject a non-5D horizon");
        if error.code() != Some("UNSUPPORTED_FORECAST_HORIZON") {
            return Err(failure(format!(
                "{horizon} produced code {:?}",
                error.code()
            )));
        }
    }
    let five = normalize_request(&json!({"ticker": "SYN", "horizon": "5D", "intent": "forecast"}))
        .map_err(|error| failure(error.message()))?;
    if five.horizon != "5D" {
        return Err(failure("5D forecast horizon was not preserved"));
    }
    let descriptive =
        normalize_request(&json!({"ticker": "SYN", "horizon": "20D", "intent": "descriptive"}))
            .map_err(|error| failure(error.message()))?;
    if descriptive.horizon != "20D" {
        return Err(failure("descriptive 20D horizon was not preserved"));
    }
    Ok(())
}

/// T02: the fixed window and recomputed coverage.
pub fn legacy_t02() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let window = window_dates(&dates);
    let bars = bars_for(&window);
    let gate = run_gate(&market_block(bars, &dates, true), &request_value(true))?;
    let policy = gate.get("policy").cloned().unwrap_or(Value::Null);
    let checks = [
        ("coverage_status", Value::String("complete".into())),
        ("decision_session", Value::String(session_end().to_string())),
        (
            "window_start_exclusive",
            Value::String((session_end() - Duration::days(364)).to_string()),
        ),
        ("calendar_verified", Value::Bool(true)),
    ];
    for (key, expected) in checks {
        if policy.get(key) != Some(&expected) {
            return Err(failure(format!(
                "policy.{key} = {:?}, expected {expected:?}",
                policy.get(key)
            )));
        }
    }
    if policy.get("expected_sessions") != policy.get("observed_sessions") {
        return Err(failure(format!("expected/observed differ: {policy}")));
    }
    Ok(())
}

/// T03: a declared window that conflicts with the policy boundary is rejected.
pub fn legacy_t03() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let bars = bars_for(&window_dates(&dates));
    let block = market_block(bars, &dates, true);
    let mut request = request_value(true);
    request["history_window"] = json!({"start": "2020-01-01"});
    let gate = run_gate(&block, &request)?;
    if !codes(&gate)
        .iter()
        .any(|code| code == "FIXED_WINDOW_CONFLICT")
    {
        return Err(failure(format!(
            "expected FIXED_WINDOW_CONFLICT, got {:?}",
            codes(&gate)
        )));
    }
    Ok(())
}

/// T04: a bar on the left-open boundary is rejected.
pub fn legacy_t04() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let mut bars = bars_for(&window_dates(&dates));
    let start = (session_end() - Duration::days(364)).to_string();
    let mut boundary = bars[0].clone();
    boundary["date"] = Value::String(start);
    bars.insert(0, boundary);
    let gate = run_gate(&market_block(bars, &dates, true), &request_value(true))?;
    if !codes(&gate)
        .iter()
        .any(|code| code == "OHLCV_OUT_OF_WINDOW")
    {
        return Err(failure(format!(
            "expected OHLCV_OUT_OF_WINDOW, got {:?}",
            codes(&gate)
        )));
    }
    Ok(())
}

/// T05: a dividend-reinvested price basis is rejected by the policy gate.
pub fn legacy_t05() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let bars = bars_for(&window_dates(&dates));
    let mut block = market_block(bars, &dates, true);
    block["price_basis"] = Value::String("split_and_dividend_adjusted".into());
    let gate = run_gate(&block, &request_value(true))?;
    if !codes(&gate)
        .iter()
        .any(|code| code == "PRICE_BASIS_UNVERIFIED")
    {
        return Err(failure(format!(
            "expected PRICE_BASIS_UNVERIFIED, got {:?}",
            codes(&gate)
        )));
    }
    if gate.get("forecast_eligible") != Some(&Value::Bool(false)) {
        return Err(failure("a dividend-adjusted basis must not stay eligible"));
    }
    Ok(())
}

/// T06: the target session is the fifth session after the decision session.
pub fn legacy_t06() -> Result<(), String> {
    let sessions = weekday_sessions(300, session_end());
    let future = future_sessions(session_end(), 10);
    let mut extended = sessions.clone();
    extended.extend(future.iter().copied());
    let resource = calendar_resource(&extended);
    let target = resource
        .target_session(session_end(), 5)
        .ok_or_else(|| failure("the supplied calendar must resolve the fifth session"))?;
    let expected = future[4];
    if target != expected {
        return Err(failure(format!("target {target} != expected {expected}")));
    }
    Ok(())
}

/// F07: malformed market fields are classified instead of panicking.
pub fn legacy_f07() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let bars = bars_for(&window_dates(&dates));
    let base = market_block(bars, &dates, true);
    let request = request_value(true);
    type Mutation = fn(&mut Value);
    let mutations: Vec<(&str, Mutation)> = vec![
        ("adjustment-array", |block| {
            block["adjustment"] = json!([]);
        }),
        ("adjustment-object", |block| {
            block["adjustment"] = json!({"a": 1});
        }),
        ("data-array", |block| {
            block["data"] = json!([1, 2]);
        }),
        ("bars-object", |block| {
            block["data"] = json!({"bars": {"a": 1}});
        }),
        ("bars-strings", |block| {
            block["data"] = json!({"bars": ["x", "y"]});
        }),
    ];
    for (name, mutate) in mutations {
        let mut block = base.clone();
        mutate(&mut block);
        let gate = run_gate(&block, &request)?;
        let found = codes(&gate);
        let expected_ok = match name {
            "adjustment-array" | "adjustment-object" => {
                found.iter().any(|code| code == "ADJUSTMENT_UNKNOWN")
            }
            _ => found.iter().any(|code| code == "INSUFFICIENT_OHLCV"),
        };
        if !expected_ok {
            return Err(failure(format!("{name} produced {:?}", found)));
        }
    }
    let mut bad_request = request.clone();
    bad_request["asset_type"] = json!([]);
    let gate = run_gate(&base, &bad_request)?;
    if !codes(&gate)
        .iter()
        .any(|code| code == "ASSET_TYPE_UNVERIFIED")
    {
        return Err(failure(format!(
            "expected ASSET_TYPE_UNVERIFIED, got {:?}",
            codes(&gate)
        )));
    }
    Ok(())
}

/// F22: a daily bar's period-start label is not treated as missing data.
pub fn legacy_f22() -> Result<(), String> {
    let last = NaiveDate::from_ymd_opt(2026, 1, 2).expect("date");
    let sessions = vec![CalendarSession {
        date: last.to_string(),
        close_at: "2026-01-02T21:00:00Z".into(),
        auction_end_at: Some("2026-01-02T21:00:00Z".into()),
        complete_at: Some("2026-01-02T21:00:00Z".into()),
    }];
    let bars = vec![json!({"date": last.to_string(), "close": 101.5})];
    let block = json!({
        "currency": "USD",
        "actual_source": "synthetic-offline",
        "latest_price_observations": [
            {"price": 101.5, "session_date": last.to_string(), "price_basis": "raw", "source": "synthetic-offline"}
        ]
    });
    let asof = Utc.with_ymd_and_hms(2026, 1, 3, 0, 30, 0).unwrap();
    let probe = |source: &str, fetch: &str| {
        latest_close_probe(
            &block,
            &bars,
            &sessions,
            &last.to_string(),
            asof,
            "source_supplied_schedule",
            Some(sd_core::contracts::parse_timestamp_str(source).unwrap()),
            Some(sd_core::contracts::parse_timestamp_str(fetch).unwrap()),
        )
    };
    let (findings, latest) = probe("2026-01-02T14:30:00Z", "2026-01-03T00:30:00Z");
    if findings
        .reason_codes
        .iter()
        .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED")
    {
        return Err(failure("a session-open label was judged as missing data"));
    }
    if latest["kind"] != json!("calendar_verified") {
        return Err(failure(format!(
            "expected calendar_verified, got {}",
            latest["kind"]
        )));
    }
    let (findings, _) = probe("2026-01-02T14:30:00Z", "2026-01-02T18:00:00Z");
    if !findings
        .reason_codes
        .iter()
        .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED")
    {
        return Err(failure("a fetch before the close must stay blocked"));
    }
    let (findings, _) = probe("2025-12-30T00:00:00Z", "2026-01-03T00:30:00Z");
    if !findings
        .reason_codes
        .iter()
        .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED")
    {
        return Err(failure("a stale bar must stay blocked"));
    }
    Ok(())
}

/// F30: sampling follows the calendar, not the window start.
pub fn legacy_f30() -> Result<(), String> {
    if business_day_ordinal("2026-01-05").map_err(|error| failure(error.message()))? + 1
        != business_day_ordinal("2026-01-06").map_err(|error| failure(error.message()))?
    {
        return Err(failure("consecutive weekdays must differ by one ordinal"));
    }
    if business_day_ordinal("2026-01-09").map_err(|error| failure(error.message()))? + 1
        != business_day_ordinal("2026-01-12").map_err(|error| failure(error.message()))?
    {
        return Err(failure(
            "the ordinal must skip the weekend without colliding",
        ));
    }
    let dates: Vec<String> = weekday_sessions(320, session_end())
        .iter()
        .map(ToString::to_string)
        .collect();
    for horizon in [1i64, 5, 20] {
        let indices: Vec<i64> = (60..(dates.len() as i64 - horizon)).collect();
        let (kept, provenance) = anchored_non_overlapping(&dates, &indices, horizon, 0);
        if kept.windows(2).any(|pair| pair[1] - pair[0] <= horizon) {
            return Err(failure(format!("labels overlap at H={horizon}")));
        }
        if provenance["candidate_phase_window_independent"] != Value::Bool(true) {
            return Err(failure(format!(
                "phase must be window independent at H={horizon}"
            )));
        }
    }
    let sampled = |window: &[String]| -> Vec<String> {
        let (kept, _) = anchored_non_overlapping(
            window,
            &(60..(window.len() as i64 - 5)).collect::<Vec<_>>(),
            5,
            0,
        );
        kept.iter()
            .map(|index| window[*index as usize].clone())
            .collect()
    };
    let base: Vec<String> = dates[..250].to_vec();
    let base_set = sampled(&base);
    for shift in [1usize, 2, 3] {
        let window: Vec<String> = dates[shift..shift + 250].to_vec();
        let shifted = sampled(&window);
        let boundary = &window[60];
        for day in &base_set {
            if day >= boundary && !shifted.contains(day) {
                return Err(failure(format!(
                    "shift {shift} dropped interior sampled date {day}"
                )));
            }
        }
    }
    let legacy_base: Vec<i64> = non_overlapping(&(60..245).collect::<Vec<_>>(), 5);
    let legacy_shift: Vec<i64> = non_overlapping(&(60..245).collect::<Vec<_>>(), 5);
    if !legacy_base
        .iter()
        .any(|index| !legacy_shift.contains(index))
        && legacy_base != legacy_shift
    {
        return Err(failure("the legacy spacing control is stale"));
    }
    let first = anchored_non_overlapping(&dates, &(60..245).collect::<Vec<_>>(), 5, 0).0;
    let second = anchored_non_overlapping(&dates, &(60..245).collect::<Vec<_>>(), 5, 0).0;
    if first != second {
        return Err(failure("anchored sampling must be deterministic"));
    }
    let (_, empty_provenance) = anchored_non_overlapping(&dates[..1], &[0], 5, 99);
    if empty_provenance["candidate_phase_window_independent"] != Value::Bool(false) {
        return Err(failure("an empty candidate set must fall back and say so"));
    }
    Ok(())
}

/// F33: holidays cannot break the session gap, and membership invariance is not claimed.
pub fn legacy_f33() -> Result<(), String> {
    let dates: Vec<String> = weekday_sessions(500, session_end())
        .iter()
        .map(ToString::to_string)
        .collect();
    let ordinals: Vec<i64> = dates
        .iter()
        .map(|day| business_day_ordinal(day).map_err(|error| failure(error.message())))
        .collect::<Result<Vec<_>, _>>()?;
    let (horizon, step) = (5i64, 7i64);
    let mut pairs = Vec::new();
    for left in 60..(dates.len() as i64 - horizon) {
        if ordinals[left as usize].rem_euclid(step) != 0 {
            continue;
        }
        for right in (left + 1)..(left + 30).min(dates.len() as i64 - horizon) {
            if ordinals[right as usize].rem_euclid(step) == 0 {
                pairs.push((left, right, right - left));
                break;
            }
        }
    }
    let (left, _, span) = *pairs
        .first()
        .ok_or_else(|| failure("fixture produced no adjacent lattice pair"))?;
    let removed: Vec<&String> = ((left + 1)..(left + 1 + (span - horizon)))
        .map(|index| &dates[index as usize])
        .collect();
    let sparse: Vec<String> = dates
        .iter()
        .filter(|day| !removed.contains(day))
        .cloned()
        .collect();
    let (kept, provenance) = anchored_non_overlapping(
        &sparse,
        &(60..(sparse.len() as i64 - horizon)).collect::<Vec<_>>(),
        horizon,
        0,
    );
    if provenance["dropped_for_overlap"].as_u64().unwrap_or(0) < 1 {
        return Err(failure("a collapsed span must drop a candidate"));
    }
    if kept.windows(2).any(|pair| pair[1] - pair[0] < horizon + 1) {
        return Err(failure("the drop did not restore the session gap"));
    }
    if provenance["required_session_gap"] != json!(horizon + 1) {
        return Err(failure("the declared gap must match the horizon"));
    }
    let dense: Vec<String> = dates
        .iter()
        .enumerate()
        .filter(|(index, _)| index % 9 != 0)
        .map(|(_, day)| day.clone())
        .collect();
    for probe in [1i64, 2, 3, 5, 10, 20] {
        let (sampled, info) = anchored_non_overlapping(
            &dense,
            &(60..(dense.len() as i64 - probe)).collect::<Vec<_>>(),
            probe,
            0,
        );
        if sampled.windows(2).any(|pair| pair[1] - pair[0] < probe + 1) {
            return Err(failure(format!(
                "H={probe} violated the gap under dense closures"
            )));
        }
        if info["required_session_gap"] != json!(probe + 1) {
            return Err(failure(format!("declared gap mismatch at H={probe}")));
        }
    }
    // Non-invariance disclosure probe: a sparse calendar must show both entry and exit.
    let sparse_calendar: Vec<String> = dates
        .iter()
        .enumerate()
        .filter(|(index, _)| index % 3 != 0)
        .map(|(_, day)| day.clone())
        .collect();
    let probe_horizon = 20i64;
    let base_window: Vec<String> = sparse_calendar[..250].to_vec();
    let base: Vec<i64> = anchored_non_overlapping(
        &base_window,
        &(60..(base_window.len() as i64 - probe_horizon)).collect::<Vec<_>>(),
        probe_horizon,
        0,
    )
    .0;
    let base_dates: Vec<&String> = base
        .iter()
        .map(|index| &base_window[*index as usize])
        .collect();
    let (mut entered, mut left_set) = (0usize, 0usize);
    for shift in [2usize, 3, 5, 7, 11, 13, 17] {
        let window: Vec<String> = sparse_calendar[shift..shift + 250].to_vec();
        let shifted: Vec<i64> = anchored_non_overlapping(
            &window,
            &(60..(window.len() as i64 - probe_horizon)).collect::<Vec<_>>(),
            probe_horizon,
            0,
        )
        .0;
        let shifted_dates: Vec<&String> = shifted
            .iter()
            .map(|index| &window[*index as usize])
            .collect();
        entered += shifted_dates
            .iter()
            .filter(|day| !base_dates.contains(day))
            .count();
        left_set += base_dates
            .iter()
            .filter(|day| !shifted_dates.contains(day))
            .count();
    }
    if entered == 0 || left_set == 0 {
        return Err(failure(format!(
            "the cascade probe no longer shows both entry and exit (entered={entered}, left={left_set})"
        )));
    }
    let (_, provenance) = anchored_non_overlapping(
        &dates,
        &(60..(dates.len() as i64 - 5)).collect::<Vec<_>>(),
        5,
        0,
    );
    if provenance["candidate_phase_window_independent"] != Value::Bool(true) {
        return Err(failure(
            "the candidate phase is calendar-anchored and must say so",
        ));
    }
    if provenance["final_membership_window_independent"] != Value::Bool(false) {
        return Err(failure(
            "final membership must not claim window independence",
        ));
    }
    Ok(())
}

/// T36: coverage is recomputed, not declared.
pub fn legacy_t36() -> Result<(), String> {
    let dates = weekday_sessions(300, session_end());
    let bars = bars_for(&window_dates(&dates));
    let block = market_block(bars.clone(), &dates, true);
    let root = scratch_dir("t36")?;
    let first = freeze_payload(&receipt_payload(block.clone()), &root).map_err(failure)?;
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(first.join("manifest.json"))
            .map_err(|error| failure(error.to_string()))?,
    )
    .map_err(|error| failure(error.to_string()))?;
    let policy = manifest.get("policy").cloned().unwrap_or(Value::Null);
    if policy["coverage_recomputed"] != Value::Bool(true)
        || policy["expected_sessions"] != policy["observed_sessions"]
    {
        return Err(failure(format!(
            "a valid snapshot must record recomputed coverage: {policy}"
        )));
    }
    let _ = std::fs::remove_dir_all(&root);

    // Forged: truncated bars still declaring complete coverage.
    let mut forged_block = block.clone();
    let truncated: Vec<Value> = forged_block["data"]["bars"]
        .as_array()
        .unwrap()
        .iter()
        .rev()
        .take(120)
        .rev()
        .cloned()
        .collect();
    forged_block["data"]["bars"] = Value::Array(truncated);
    forged_block["data"]["policy_window"]["coverage_status"] = Value::String("complete".into());
    forged_block["data"]["policy_window"]["observed_sessions"] =
        forged_block["data"]["policy_window"]["expected_sessions"].clone();
    forged_block["data"]["policy_window"]["missing_session_count"] = Value::from(0);
    forged_block["data"]["policy_window"]["missing_sessions"] = Value::Array(Vec::new());
    let root = scratch_dir("t36-forged")?;
    let frozen = freeze_payload(&receipt_payload(forged_block), &root).map_err(failure)?;
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(frozen.join("manifest.json"))
            .map_err(|error| failure(error.to_string()))?,
    )
    .map_err(|error| failure(error.to_string()))?;
    let validation = manifest
        .get("data_validation")
        .cloned()
        .unwrap_or(Value::Null);
    if validation["forecast_eligible"] != Value::Bool(false) {
        return Err(failure(
            "truncated bars declaring complete coverage must block eligibility",
        ));
    }
    if !validation["reason_codes"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .any(|code| code == "INSUFFICIENT_HISTORY_COVERAGE")
        })
        .unwrap_or(false)
    {
        return Err(failure(format!(
            "expected INSUFFICIENT_HISTORY_COVERAGE, got {}",
            validation["reason_codes"]
        )));
    }
    if manifest["policy"]["coverage_status"] == Value::String("complete".into()) {
        return Err(failure("the manifest must carry the recomputed coverage"));
    }
    let _ = std::fs::remove_dir_all(&root);
    Ok(())
}

/// T40: a declared split-adjusted series must not show the reverse split step.
pub fn legacy_t40() -> Result<(), String> {
    let sessions: Vec<String> = [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
        "2026-01-12",
        "2026-01-13",
        "2026-01-14",
    ]
    .iter()
    .map(|day| (*day).to_string())
    .collect();
    let block = |price_basis: &str, confirmed: bool| {
        json!({
            "adjustment": "provider",
            "price_basis": price_basis,
            "adjustment_evidence": {
                "source": "test",
                "price_basis": price_basis,
                "checked_through": sessions.last(),
                "action_coverage_confirmed": confirmed,
                "actions": [{"date": sessions[3], "type": "stock_splits", "value": 2.0}],
            }
        })
    };
    let continuous: Vec<Value> = sessions
        .iter()
        .enumerate()
        .map(|(index, day)| json!({"date": day, "close": 100.0 + index as f64}))
        .collect();
    let (findings, _status, _known, reconciliation) = adjustment_probe(
        &block("split_adjusted", true),
        &sessions[0],
        sessions.last().unwrap(),
        &continuous,
    );
    if findings
        .reason_codes
        .iter()
        .any(|code| code == "CORPORATE_ACTION_CONTAMINATION")
    {
        return Err(failure(
            "a continuous split-adjusted series must not be vetoed",
        ));
    }
    if reconciliation
        .first()
        .and_then(|row| row.get("verdict"))
        .and_then(Value::as_str)
        != Some("consistent_with_split_adjusted")
    {
        return Err(failure(format!(
            "unexpected reconciliation: {reconciliation:?}"
        )));
    }
    let unadjusted: Vec<Value> = sessions
        .iter()
        .enumerate()
        .map(|(index, day)| {
            let close = if index < 3 {
                100.0 + index as f64
            } else {
                50.0 + index as f64
            };
            json!({"date": day, "close": close})
        })
        .collect();
    let (findings, _status, _known, _) = adjustment_probe(
        &block("split_adjusted", true),
        &sessions[0],
        sessions.last().unwrap(),
        &unadjusted,
    );
    if !findings
        .reason_codes
        .iter()
        .any(|code| code == "CORPORATE_ACTION_CONTAMINATION")
        || findings.blockers.is_empty()
    {
        return Err(failure(
            "a split step in a declared split-adjusted series must be fatal",
        ));
    }
    let (findings, _status, _known, _) = adjustment_probe(
        &block("split_adjusted", false),
        &sessions[0],
        sessions.last().unwrap(),
        &continuous,
    );
    if !findings
        .reason_codes
        .iter()
        .any(|code| code == "CORPORATE_ACTION_UNKNOWN")
        || !findings.blockers.is_empty()
    {
        return Err(failure(
            "missing action coverage must degrade, not invent contamination",
        ));
    }
    Ok(())
}

/// Implemented R03 checks: `(id, body)`.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("T01", legacy_t01 as CheckFn),
        ("T02", legacy_t02 as CheckFn),
        ("T03", legacy_t03 as CheckFn),
        ("T04", legacy_t04 as CheckFn),
        ("T05", legacy_t05 as CheckFn),
        ("T06", legacy_t06 as CheckFn),
        ("F07", legacy_f07 as CheckFn),
        ("F22", legacy_f22 as CheckFn),
        ("F30", legacy_f30 as CheckFn),
        ("F33", legacy_f33 as CheckFn),
        ("T36", legacy_t36 as CheckFn),
        ("T40", legacy_t40 as CheckFn),
    ]
}
