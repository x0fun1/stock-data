//! Differential tests for the market-data gate (`data_checks.check_market`).
//!
//! The reference blocks come from the frozen Python snapshot plus recorded
//! mutations. Tree equality is the contract: a changed reason code, blocker text or
//! eligibility flag changes whether a probability may be published.
//!
//! A-CAL-01 boundary regressions additionally run the real generated calendar pack
//! through `check_market` at the HK CAS completion boundaries (16:00/16:05/16:09:59/
//! 16:10 and the half-day equivalents) and the US DST/early-close boundaries.

use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveDate, Utc};
use sd_core::calendar::CalendarResource;
use sd_core::data_checks::{check_market, CheckOptions};
use sd_core::policy;
use serde_json::{json, Value};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/check-market")
}

fn assets() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../..")
        .join("assets/calendars")
}

fn read(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

fn pack_resource(market: &str) -> CalendarResource {
    let path = assets().join(format!("{market}.sessions.json"));
    let value = read(&path);
    CalendarResource::from_json(&value)
        .unwrap_or_else(|error| panic!("{market} pack parses: {error}"))
}

fn instant(text: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(text)
        .expect("timestamp")
        .with_timezone(&Utc)
}

fn day(text: &str) -> NaiveDate {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").expect("date")
}

fn codes(gate: &Value) -> Vec<String> {
    gate["reason_codes"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Build a complete normalized market block whose policy window ends at `decision`
/// and whose bars end at the same session date (the evidence the CAS boundary must
/// qualify). The block carries no `session_calendar`, so the pack is the only calendar
/// evidence and the `library_verified` path is exercised.
fn market_block(
    resource: &CalendarResource,
    ticker: &str,
    currency: &str,
    asof: DateTime<Utc>,
    decision: NaiveDate,
) -> Value {
    let (start, end) = policy::window_bounds(decision);
    let sessions: Vec<String> = resource
        .sessions_in_window(&start, &end)
        .into_iter()
        .map(|session| session.date.clone())
        .collect();
    let bars: Vec<Value> = sessions
        .iter()
        .enumerate()
        .map(|(index, date)| {
            let price = 100.0 + f64::from(index as u32) * 0.05;
            json!({
                "date": date,
                "open": price - 0.2,
                "high": price + 0.4,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000.0,
            })
        })
        .collect();
    let asof_text = asof.format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let policy_window = json!({
        "policy_version": policy::POLICY_VERSION,
        "status": "resolved",
        "decision_session": decision.to_string(),
        "decision_session_basis": "exchange_calendar",
        "window_start_exclusive": start,
        "window_end_inclusive": end,
        "calendar_verified": true,
        "calendar_source": "exchange_calendars 4.13.2",
        "price_basis": policy::PRICE_BASIS,
        "price_basis_source": "provider_declared_split_adjusted_close",
        "expected_sessions": sessions.len(),
        "observed_sessions": bars.len(),
        "missing_sessions": [],
        "missing_session_count": 0,
        "coverage_status": "complete",
        "coverage_basis": "recomputed_from_calendar_and_bars",
    });
    let data = json!({
        "symbol": ticker,
        "frequency": "1d",
        "bars": bars,
        "history_window": {"start": start, "end": end, "selection": "policy_window"},
        "policy_window": policy_window,
    });
    json!({
        "status": "complete",
        "actual_source": "Yahoo Finance",
        "currency": currency,
        "unit": currency,
        "adjustment": "provider",
        "price_basis": policy::PRICE_BASIS,
        "price_basis_source": "provider_declared_split_adjusted_close",
        "source_timestamp": asof_text,
        "fetched_at_utc": asof_text,
        "last_bar_closed": true,
        "data": data,
    })
}

fn request(ticker: &str, market: &str, asof: DateTime<Utc>) -> Value {
    json!({
        "ticker": ticker,
        "market": market,
        "horizon": "5D",
        "intent": "forecast",
        "asset_type": "stock",
        "mode": "standard",
        "asof": asof.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "policy": {"policy_version": policy::POLICY_VERSION},
    })
}

fn run_gate(
    resource: &CalendarResource,
    ticker: &str,
    market: &str,
    currency: &str,
    asof: DateTime<Utc>,
    decision: NaiveDate,
) -> Value {
    let block = market_block(resource, ticker, currency, asof, decision);
    let request = request(ticker, market, asof);
    check_market(
        &block,
        &request,
        &CheckOptions {
            captured_at: Some(request["asof"].as_str().unwrap_or_default()),
            resolved_calendar_evidence: None,
            library_calendar: Some(resource),
            allow_library_calendar: true,
            enforce_policy: true,
        },
    )
    .expect("gate runs")
}

/// A-CAL-01: HK normal-day CAS completion boundary through the real gate + pack.
#[test]
fn hk_cas_normal_day_boundary_through_the_gate() {
    let hk = pack_resource("HK");
    // 2026-10-30 is a normal HK session: continuous close 08:00Z, CAS latest end 08:10Z.
    for (asof_text, expected_decision) in [
        ("2026-10-30T08:00:00Z", "2026-10-29"),
        ("2026-10-30T08:05:00Z", "2026-10-29"),
        ("2026-10-30T08:09:59Z", "2026-10-29"),
        ("2026-10-30T08:10:00Z", "2026-10-30"),
    ] {
        let asof = instant(asof_text);
        assert_eq!(
            hk.decision_session(asof).map(|date| date.to_string()),
            Some(expected_decision.to_string()),
            "{asof_text} must resolve the confirmed completion boundary"
        );
        let gate = run_gate(&hk, "0700.HK", "HK", "HKD", asof, day(expected_decision));
        assert!(
            !codes(&gate)
                .iter()
                .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED"),
            "{asof_text}: the gate must accept the complete decision session: {:?}",
            codes(&gate)
        );
        assert!(
            !codes(&gate)
                .iter()
                .any(|code| code == "SEVERE_MISSING_BARS"),
            "{asof_text}: axis must match: {:?}",
            codes(&gate)
        );
        assert_eq!(
            gate["calendar_verification"], "library_verified",
            "{asof_text}: {gate}"
        );
        assert_eq!(gate["forecast_eligible"], true, "{asof_text}: {gate}");
    }

    // Before 16:10 the current day is not confirmable: claiming it as the decision
    // session is a fatal observation mismatch, not a warning.
    let premature = run_gate(
        &hk,
        "0700.HK",
        "HK",
        "HKD",
        instant("2026-10-30T08:05:00Z"),
        day("2026-10-30"),
    );
    assert!(
        codes(&premature)
            .iter()
            .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED"),
        "a pre-completion claim must be unresolvable: {:?}",
        codes(&premature)
    );
    assert_eq!(premature["forecast_eligible"], false);
    // At/after 16:10 the same claim is accepted (conditional on the OHLCV evidence).
    let at_completion = run_gate(
        &hk,
        "0700.HK",
        "HK",
        "HKD",
        instant("2026-10-30T08:10:00Z"),
        day("2026-10-30"),
    );
    assert!(
        !codes(&at_completion)
            .iter()
            .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED"),
        "{:?}",
        codes(&at_completion)
    );
    assert_eq!(at_completion["forecast_eligible"], true);
}

/// A-CAL-01: HK half-day CAS completion boundary (12:00/12:05/12:09:59/12:10 HKT).
#[test]
fn hk_cas_half_day_boundary_through_the_gate() {
    let hk = pack_resource("HK");
    // 2026-12-24 is a half day: continuous close 04:00Z, CAS latest end 04:10Z.
    for (asof_text, expected_decision) in [
        ("2026-12-24T04:00:00Z", "2026-12-23"),
        ("2026-12-24T04:05:00Z", "2026-12-23"),
        ("2026-12-24T04:09:59Z", "2026-12-23"),
        ("2026-12-24T04:10:00Z", "2026-12-24"),
    ] {
        let asof = instant(asof_text);
        assert_eq!(
            hk.decision_session(asof).map(|date| date.to_string()),
            Some(expected_decision.to_string()),
            "{asof_text}"
        );
        let gate = run_gate(&hk, "0700.HK", "HK", "HKD", asof, day(expected_decision));
        assert_eq!(
            gate["calendar_verification"], "library_verified",
            "{asof_text}: {gate}"
        );
        assert_eq!(gate["forecast_eligible"], true, "{asof_text}: {gate}");
    }
    let premature = run_gate(
        &hk,
        "0700.HK",
        "HK",
        "HKD",
        instant("2026-12-24T04:09:59Z"),
        day("2026-12-24"),
    );
    assert!(
        codes(&premature)
            .iter()
            .any(|code| code == "LATEST_OBSERVATION_UNRESOLVED"),
        "{:?}",
        codes(&premature)
    );
}

/// US regression: DST close shift, the early-close half day, and no CAS extension.
#[test]
fn us_dst_and_half_day_boundaries_are_unchanged() {
    let us = pack_resource("US");
    for (asof_text, expected_decision) in [
        // DST ends 2026-11-01: the close moves from 20:00Z to 21:00Z.
        ("2026-11-02T20:30:00Z", "2026-10-30"),
        ("2026-11-02T21:00:00Z", "2026-11-02"),
        // Thanksgiving Friday early close: 13:00 ET = 18:00Z.
        ("2026-11-27T17:59:59Z", "2026-11-25"),
        ("2026-11-27T18:00:00Z", "2026-11-27"),
        ("2026-12-24T18:00:00Z", "2026-12-24"),
    ] {
        assert_eq!(
            us.decision_session(instant(asof_text))
                .map(|date| date.to_string()),
            Some(expected_decision.to_string()),
            "{asof_text}"
        );
    }
    let gate = run_gate(
        &us,
        "AAPL",
        "US",
        "USD",
        instant("2026-11-27T18:00:00Z"),
        day("2026-11-27"),
    );
    assert_eq!(gate["calendar_verification"], "library_verified");
    assert_eq!(gate["forecast_eligible"], true, "{gate}");
}

fn cases() -> Vec<PathBuf> {
    let mut directories: Vec<PathBuf> = std::fs::read_dir(fixtures())
        .expect("fixture directory")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    directories.sort();
    directories
}

#[test]
fn gate_output_matches_python_for_every_case() {
    let directories = cases();
    assert_eq!(directories.len(), 15, "the gate corpus must stay complete");
    let mut failures: Vec<String> = Vec::new();
    for directory in directories {
        let name = directory
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let input = read(&directory.join("input.json"));
        let expected = read(&directory.join("expected.json"));
        let options = CheckOptions {
            captured_at: input["captured_at"].as_str(),
            resolved_calendar_evidence: None,
            library_calendar: None,
            allow_library_calendar: false,
            enforce_policy: input["enforce_policy"].as_bool().unwrap_or(true),
        };
        match check_market(&input["block"], &input["request"], &options) {
            Ok(actual) => {
                if actual != expected {
                    failures.push(format!(
                        "{name} differs\n  actual:   {}\n  expected: {}",
                        serde_json::to_string(&actual).unwrap(),
                        serde_json::to_string(&expected).unwrap()
                    ));
                }
            }
            Err(error) => failures.push(format!("{name} returned an error: {error}")),
        }
    }
    assert!(
        failures.is_empty(),
        "check_market differences:\n{}",
        failures.join("\n\n")
    );
}

#[test]
fn a_vetoed_block_cannot_publish_a_probability() {
    let directory = fixtures().join("not-closed");
    let input = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected.json"));
    let options = CheckOptions {
        captured_at: input["captured_at"].as_str(),
        enforce_policy: true,
        ..CheckOptions::default()
    };
    let gate = check_market(&input["block"], &input["request"], &options).expect("gate");
    assert_eq!(gate["forecast_eligible"], expected["forecast_eligible"]);
    assert_eq!(
        gate["forecast_eligible"], false,
        "an unconfirmed close must veto"
    );
    assert_eq!(gate["reporting_status"], "veto");
    assert_eq!(gate["prediction_eligibility"]["status"], "blocked");
    assert!(gate["blockers"]
        .as_array()
        .unwrap()
        .iter()
        .any(|value| value
            .as_str()
            .unwrap()
            .contains("provider marked the latest OHLCV bar as not closed")));
}

#[test]
fn legacy_snapshot_skips_the_fixed_policy_checks() {
    let directory = fixtures().join("complete-window-enforce-false");
    let input = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected.json"));
    let options = CheckOptions {
        captured_at: input["captured_at"].as_str(),
        enforce_policy: false,
        ..CheckOptions::default()
    };
    let gate = check_market(&input["block"], &input["request"], &options).expect("gate");
    assert_eq!(gate["policy"]["applicable"], false);
    assert_eq!(gate["policy"]["status"], "legacy_not_enforced");
    assert_eq!(gate, expected);
}
