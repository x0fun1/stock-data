//! Differential tests for the market-data readers and sampling helpers.
//!
//! Reference values come from `quant_research/market_data.py`. These helpers sit
//! directly under the gate and the feature builders, so a drift changes which rows
//! are labelled and which sessions are sampled.

use std::path::{Path, PathBuf};

use chrono::NaiveDate;
use sd_core::market_data::{
    adjusted_close_series, anchored_non_overlapping, business_day_ordinal, evaluation_sample,
    fixed_phase_sample, forward_returns, non_overlapping, prediction_price_series, simple_return,
};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/market-data")
}

fn read(name: &str) -> Value {
    let path = fixtures().join(name);
    serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{error}")))
        .expect("json")
}

fn floats(value: &Value) -> Vec<f64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_f64().unwrap())
        .collect()
}

fn ints(value: &Value) -> Vec<i64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_i64().unwrap())
        .collect()
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn weekday_ordinals_match_python() {
    let rows = read("business-day-ordinal.json");
    for row in rows.as_array().expect("rows") {
        let day = row["date"].as_str().unwrap();
        assert_eq!(
            business_day_ordinal(day).expect("ordinal"),
            row["ordinal"].as_i64().unwrap(),
            "ordinal differs for {day}"
        );
    }
    // Consecutive weekdays differ by one; a weekend is skipped without colliding.
    assert_eq!(
        business_day_ordinal("2026-01-05").unwrap() + 1,
        business_day_ordinal("2026-01-06").unwrap()
    );
    assert_eq!(
        business_day_ordinal("2026-01-09").unwrap() + 1,
        business_day_ordinal("2026-01-12").unwrap()
    );
    assert_eq!(business_day_ordinal("1969-12-29").unwrap(), 0);
}

#[test]
fn legacy_spacing_matches_python() {
    let rows = read("non-overlapping.json");
    for row in rows.as_array().expect("rows") {
        let kept = non_overlapping(&ints(&row["indices"]), row["horizon"].as_i64().unwrap());
        assert_eq!(
            Value::from(kept),
            row["kept"],
            "spacing differs for {}",
            row["case"]
        );
    }
}

#[test]
fn anchored_sampling_matches_python() {
    let rows = read("anchored-non-overlapping.json");
    assert_eq!(rows.as_array().unwrap().len(), 10);
    let dates_file = read("business-day-ordinal.json");
    let _ = dates_file; // ordinals file only pins the ordinal function
                        // Rebuild the same calendar the fixture used.
    let dates = weekday_dates(
        "2024-01-01",
        320,
        &["2024-01-15", "2024-02-19", "2024-05-27", "2024-07-04"],
    );
    for row in rows.as_array().expect("rows") {
        let horizon = row["horizon"].as_i64().unwrap();
        let phase = row["phase"].as_i64().unwrap();
        let indices = ints(&row["indices"]);
        let (kept, provenance) = anchored_non_overlapping(&dates, &indices, horizon, phase);
        assert_eq!(
            Value::from(kept),
            row["kept"],
            "anchored sampling differs at H={horizon} phase={phase}"
        );
        assert_eq!(
            provenance, row["provenance"],
            "provenance differs at H={horizon} phase={phase}"
        );
    }
    // The fallback is deterministic and says the phase is not window independent.
    let fallback = anchored_non_overlapping(&dates[..7], &[0, 1, 2], 5, 99);
    assert_eq!(fallback.1["method"], "legacy_spacing_fallback");
    assert_eq!(fallback.1["candidate_phase_window_independent"], false);
}

#[test]
fn forward_returns_and_simple_return_match_python() {
    let payload = read("forward-returns.json");
    let closes = floats(&payload["closes"]);
    let expected: Vec<Value> = payload["returns"].as_array().unwrap().clone();
    let actual: Vec<Value> =
        forward_returns(&closes, payload["horizon"].as_u64().unwrap() as usize)
            .into_iter()
            .map(|value| value.map(Value::from).unwrap_or(Value::Null))
            .collect();
    assert_eq!(Value::Array(actual), Value::Array(expected));

    for row in read("simple-return.json").as_array().expect("returns") {
        let start = row["start"].as_f64().unwrap();
        let end = row["end"].as_f64().unwrap();
        match simple_return(start, end) {
            Ok(value) => assert_eq!(Value::from(value), row["value"], "simple return differs"),
            Err(error) => assert_eq!(error.message(), row["error"].as_str().unwrap()),
        }
    }
}

#[test]
fn price_series_readers_match_python() {
    let payload = read("prediction-price-series.json");
    let block = snapshot_market();
    let (dates, values, provenance) = prediction_price_series(&block).expect("prediction series");
    assert_eq!(Value::from(strings(&payload["dates"])), payload["dates"]);
    assert_eq!(Value::from(dates), payload["dates"]);
    assert_eq!(Value::from(values), payload["values"]);
    assert_eq!(provenance, payload["provenance"]);

    let adjusted = read("adjusted-close-series.json");
    let (plain_dates, plain_values, plain_provenance) =
        adjusted_close_series(&block).expect("plain series");
    assert_eq!(Value::from(plain_dates), adjusted["plain"]["dates"]);
    assert_eq!(Value::from(plain_values), adjusted["plain"]["values"]);
    assert_eq!(plain_provenance, adjusted["plain"]["provenance"]);
}

#[test]
fn sampling_phase_sampling_matches_python() {
    let payload = read("fixed-phase-sample.json");
    let block = snapshot_market();
    let sessions: Vec<NaiveDate> = block["session_calendar"]["sessions"]
        .as_array()
        .expect("sessions")
        .iter()
        .map(|row| NaiveDate::parse_from_str(row["date"].as_str().unwrap(), "%Y-%m-%d").unwrap())
        .collect();
    let dates: Vec<String> = sessions
        .iter()
        .take(120)
        .map(|date| date.to_string())
        .collect();
    let (sampled, provenance) =
        fixed_phase_sample(&dates, &sessions, sd_core::policy::SAMPLING_MODULUS);
    assert_eq!(Value::from(sampled), payload["anchored"]["sampled"]);
    assert_eq!(provenance, payload["anchored"]["provenance"]);

    let unanchored_sessions = &sessions[10..];
    let (fallback, fallback_provenance) = fixed_phase_sample(
        &dates,
        unanchored_sessions,
        sd_core::policy::SAMPLING_MODULUS,
    );
    assert_eq!(Value::from(fallback), payload["unanchored"]["sampled"]);
    assert_eq!(fallback_provenance, payload["unanchored"]["provenance"]);
}
fn snapshot_market() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/snapshot/complete-window/market.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("snapshot market")).expect("json")
}

#[test]
fn evaluation_sample_matches_python() {
    let payload = read("evaluation-sample.json");
    let predictions = payload["predictions"].as_array().expect("predictions");
    let block = snapshot_market();
    let sessions: Vec<NaiveDate> = block["session_calendar"]["sessions"]
        .as_array()
        .expect("sessions")
        .iter()
        .map(|row| NaiveDate::parse_from_str(row["date"].as_str().unwrap(), "%Y-%m-%d").unwrap())
        .collect();
    let anchored = evaluation_sample(predictions, Some(&sessions));
    assert_eq!(anchored, payload["anchored"]);
    let unanchored = evaluation_sample(predictions, Some(&sessions[10..]));
    assert_eq!(unanchored, payload["unanchored"]);
    let none = evaluation_sample(predictions, None);
    assert_eq!(none, payload["no-sessions"]);
}

fn weekday_dates(start: &str, count: usize, holidays: &[&str]) -> Vec<String> {
    let mut current = NaiveDate::parse_from_str(start, "%Y-%m-%d").expect("start date");
    let mut days = Vec::new();
    while days.len() < count {
        let weekend = current.format("%u").to_string().parse::<u32>().unwrap() >= 6;
        let text = current.to_string();
        if !weekend && !holidays.contains(&text.as_str()) {
            days.push(text);
        }
        current = current.succ_opt().expect("next day");
    }
    days
}
