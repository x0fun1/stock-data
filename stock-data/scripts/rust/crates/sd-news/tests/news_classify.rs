//! Differential parity for News screening, clustering text and session decay.

use std::path::Path;

use sd_news::classify::{
    direction_for, event_text, event_type_for, matched_topics, opinion_score, COVERAGE_NOTE,
    EVENT_PATTERNS, LEXICON_VERSION, SUPPORTED_LANGUAGES, TOPIC_TERMS,
};
use sd_news::decay::{half_life_for, time_decay};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/news-classify/news.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn assert_json_close(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(left), Value::Number(right)) => {
            let left_value = left.as_f64().expect("number");
            let right_value = right.as_f64().expect("number");
            let tolerance = 1e-12 * right_value.abs().max(1.0);
            assert!(
                (left_value - right_value).abs() <= tolerance,
                "{path}: {left_value} vs {right_value}"
            );
        }
        (Value::Array(left), Value::Array(right)) => {
            assert_eq!(left.len(), right.len(), "{path}: array length differs");
            for (index, (actual_item, expected_item)) in left.iter().zip(right.iter()).enumerate() {
                assert_json_close(actual_item, expected_item, &format!("{path}[{index}]"));
            }
        }
        (Value::Object(left), Value::Object(right)) => {
            assert_eq!(left.len(), right.len(), "{path}: object size differs");
            for (key, expected_item) in right.iter() {
                let actual_item = left
                    .get(key)
                    .unwrap_or_else(|| panic!("{path}: missing key {key}"));
                assert_json_close(actual_item, expected_item, &format!("{path}/{key}"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}: value differs"),
    }
}

#[test]
fn classification_matches_python() {
    let document = read();
    for (index, case) in document["classify"]
        .as_array()
        .expect("classify")
        .iter()
        .enumerate()
    {
        let text = case["text"].as_str().expect("text");
        let hint = case["hint"].as_str();
        let path = format!("classify[{index}]");
        assert_eq!(
            Value::from(event_type_for(text, hint)),
            case["event_type"],
            "{path}: event type"
        );
        assert_eq!(
            Value::from(direction_for(text)),
            case["direction"],
            "{path}: direction"
        );
        assert_json_close(
            &opinion_score(text).map(Value::from).unwrap_or(Value::Null),
            &case["opinion_score"],
            &format!("{path}: opinion"),
        );
        assert_eq!(
            Value::Array(
                matched_topics(text)
                    .into_iter()
                    .map(Value::String)
                    .collect()
            ),
            case["matched_topics"],
            "{path}: topics"
        );
    }
    assert_eq!(
        Value::from(event_text(&serde_json::json!({"articles": [
            {"title": "A", "summary": "B"},
            {"title": null, "summary": "C"},
            {"title": "D"}
        ]}))),
        document["event_text"]
    );
    let lexicon = &document["lexicon"];
    assert_eq!(Value::from(LEXICON_VERSION), lexicon["version"]);
    assert_eq!(
        Value::Array(
            SUPPORTED_LANGUAGES
                .iter()
                .map(|value| Value::from(*value))
                .collect()
        ),
        lexicon["languages"]
    );
    assert_eq!(Value::from(COVERAGE_NOTE), lexicon["coverage_note"]);
    assert_eq!(
        Value::Array(
            EVENT_PATTERNS
                .iter()
                .map(|(kind, _)| Value::from(*kind))
                .collect()
        ),
        lexicon["event_kinds"]
    );
    assert_eq!(
        Value::Array(TOPIC_TERMS.iter().map(|term| Value::from(*term)).collect()),
        lexicon["topic_terms"]
    );
}

fn dates_and_bars() -> Vec<Value> {
    [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
        "2026-01-12",
    ]
    .iter()
    .enumerate()
    .map(|(index, day)| serde_json::json!({"date": day, "close": 100.0 + index as f64}))
    .collect()
}

#[test]
fn decay_matches_python() {
    let document = read();
    let data = &document["decay"];
    let bars = dates_and_bars();
    let cases: Vec<(&str, &str, &str, &str, Value, &str)> = vec![
        (
            "us_same_day",
            "2026-01-02T15:30:00Z",
            "EARNINGS",
            "5D",
            serde_json::json!({}),
            "US",
        ),
        (
            "us_late_close",
            "2026-01-02T23:30:00Z",
            "EARNINGS",
            "5D",
            serde_json::json!({}),
            "US",
        ),
        (
            "hk_boundary",
            "2026-01-04T23:30:00Z",
            "EARNINGS",
            "5D",
            serde_json::json!({}),
            "HK",
        ),
        (
            "policy_override",
            "2026-01-02T15:30:00Z",
            "EARNINGS",
            "5D",
            serde_json::json!({"half_life_sessions_by_event_type": {"EARNINGS": 10, "default": 20}}),
            "US",
        ),
        (
            "default_override",
            "2026-01-02T15:30:00Z",
            "GUIDANCE",
            "1D",
            serde_json::json!({"half_life_sessions_by_event_type": {"default": 12}}),
            "US",
        ),
        (
            "no_override_1d",
            "2026-01-02T15:30:00Z",
            "GUIDANCE",
            "1D",
            serde_json::json!({}),
            "US",
        ),
    ];
    for (name, published, event, horizon, policy, market) in cases {
        let actual = time_decay(published, &bars, event, horizon, &policy, market).expect("decay");
        assert_json_close(&Value::Object(actual), &data[name], name);
    }
    let half_lives = &data["half_lives"];
    assert_eq!(
        Value::from(half_life_for("EARNINGS", "5D", &serde_json::json!({})).expect("default")),
        half_lives["earnings_default"]
    );
    assert_eq!(
        Value::from(
            half_life_for(
                "EARNINGS",
                "5D",
                &serde_json::json!({"half_life_sessions_by_event_type": {"EARNINGS": 10}})
            )
            .expect("override")
        ),
        half_lives["policy_earnings"]
    );
    assert_eq!(
        Value::from(
            half_life_for(
                "X",
                "5D",
                &serde_json::json!({"half_life_sessions_by_event_type": {"default": 7}})
            )
            .expect("default override")
        ),
        half_lives["policy_default"]
    );
    assert_eq!(
        Value::from(half_life_for("X", "20D", &serde_json::json!({})).expect("horizon")),
        half_lives["horizon_20d"]
    );
    let errors = data["invalid_policy_errors"]
        .as_array()
        .expect("errors")
        .clone();
    for (index, policy) in [
        serde_json::json!({"half_life_sessions_by_event_type": {"EARNINGS": 0}}),
        serde_json::json!({"half_life_sessions_by_event_type": {"EARNINGS": -3}}),
    ]
    .into_iter()
    .enumerate()
    {
        assert_eq!(
            half_life_for("EARNINGS", "5D", &policy).expect_err("invalid half-life"),
            errors[index].as_str().unwrap()
        );
    }
}
