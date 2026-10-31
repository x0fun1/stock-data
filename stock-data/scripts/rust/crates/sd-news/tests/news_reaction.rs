//! Differential parity for News reaction validation and timing/priced-in disclosures.

use std::path::Path;

use sd_news::reaction::validate_reaction;
use sd_news::timing::{
    aggregate_priced_in, event_time_bucket, priced_in_assessment, trailing_sigma,
};
use serde_json::{Map, Value};

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/news-reaction/news.json");
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
fn reaction_matches_python() {
    let document = read();
    let inputs = &document["inputs"];
    let bars = inputs["bars"].as_array().expect("bars").clone();
    let session_closes: Map<String, Value> = inputs["session_closes"]
        .as_object()
        .expect("closes")
        .clone();
    // (case, published_at, event direction, session close mapping)
    type ReactionCase<'a> = (&'a str, &'a str, &'a str, Option<&'a Map<String, Value>>);
    let cases: Vec<ReactionCase> = vec![
        ("pre_market", "2026-01-06T12:00:00Z", "positive", None),
        (
            "intraday_after_open",
            "2026-01-06T16:00:00Z",
            "positive",
            None,
        ),
        ("after_close", "2026-01-06T22:00:00Z", "negative", None),
        (
            "session_closes",
            "2026-01-06T21:30:00Z",
            "negative",
            Some(&session_closes),
        ),
        ("hk_timezone", "2026-01-05T23:30:00Z", "positive", None),
        ("unknown_market", "2026-01-06T15:00:00Z", "positive", None),
        ("before_window", "2025-12-01T15:00:00Z", "positive", None),
        ("after_window", "2026-02-01T15:00:00Z", "positive", None),
    ];
    for (name, published, direction, closes) in cases {
        let market = if name == "hk_timezone" {
            "HK"
        } else if name == "unknown_market" {
            "JP"
        } else {
            "US"
        };
        let actual = validate_reaction(published, direction, market, &bars, closes);
        assert_json_close(&actual, &document["reactions"][name], name);
    }
    let empty = validate_reaction("2026-01-06T15:00:00Z", "positive", "US", &[], None);
    assert_json_close(&empty, &document["reactions"]["no_bars"], "no_bars");
}

#[test]
fn timing_matches_python() {
    let document = read();
    let inputs = &document["inputs"];
    let closes: Vec<f64> = inputs["closes"]
        .as_array()
        .expect("closes")
        .iter()
        .filter_map(Value::as_f64)
        .collect();
    let session_closes: Map<String, Value> = inputs["session_closes"]
        .as_object()
        .expect("closes")
        .clone();
    let sigma = &document["trailing_sigma"];
    for (name, index, expected) in [
        ("full", 25usize, &sigma["full"]),
        ("short", 3, &sigma["short"]),
    ] {
        let (value, observations) = trailing_sigma(&closes, index, 20);
        assert_json_close(
            &Value::Array(vec![
                value.map(Value::from).unwrap_or(Value::Null),
                Value::from(observations),
            ]),
            expected,
            &format!("trailing_sigma/{name}"),
        );
    }
    let (value, observations) = trailing_sigma(&[], 0, 20);
    assert_json_close(
        &serde_json::json!([value, observations]),
        &sigma["empty"],
        "trailing_sigma/empty",
    );

    let buckets = &document["buckets"];
    assert_json_close(
        &event_time_bucket(
            Some(&Value::String("2026-01-06T12:00:00Z".into())),
            Some(&Value::String("2026-01-06".into())),
            Some(&session_closes),
        ),
        &buckets["with_close"],
        "buckets/with_close",
    );
    assert_json_close(
        &event_time_bucket(
            Some(&Value::String("2026-01-06T22:00:00Z".into())),
            Some(&Value::String("2026-01-06".into())),
            Some(&session_closes),
        ),
        &buckets["after_close"],
        "buckets/after_close",
    );
    assert_json_close(
        &event_time_bucket(
            Some(&Value::String("2026-01-06T12:00:00Z".into())),
            Some(&Value::String("2026-01-09".into())),
            Some(&session_closes),
        ),
        &buckets["no_close"],
        "buckets/no_close",
    );
    assert_json_close(
        &event_time_bucket(None, None, None),
        &buckets["no_inputs"],
        "buckets/no_inputs",
    );

    let expected = &document["priced_in"];
    let card = serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.021});
    assert_json_close(
        &priced_in_assessment(&card, &closes, Some(10), "positive", 5, 1.0),
        &expected["large_no_continuation"],
        "priced_in/large_no_continuation",
    );
    let continued_closes: Vec<f64> = (0..15)
        .map(|index| 100.0 + index as f64 * 0.4)
        .chain((0..15).map(|index| 100.0 + index as f64 * 2.0))
        .collect();
    assert_json_close(
        &priced_in_assessment(&card, &continued_closes, Some(10), "positive", 5, 1.0),
        &expected["continued"],
        "priced_in/continued",
    );
    assert_json_close(
        &priced_in_assessment(
            &serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.0001}),
            &closes,
            Some(10),
            "positive",
            5,
            1.0,
        ),
        &expected["within_noise"],
        "priced_in/within_noise",
    );
    assert_json_close(
        &priced_in_assessment(
            &serde_json::json!({"status": "not_classifiable", "daily_close_return": 0.01}),
            &closes,
            Some(10),
            "positive",
            5,
            1.0,
        ),
        &expected["unclassifiable"],
        "priced_in/unclassifiable",
    );
    assert_json_close(
        &priced_in_assessment(&card, &closes, Some(28), "positive", 5, 1.0),
        &expected["unmatured"],
        "priced_in/unmatured",
    );
    assert_json_close(
        &priced_in_assessment(&card, &closes, Some(10), "mixed", 5, 1.0),
        &expected["direction_neutral"],
        "priced_in/direction_neutral",
    );

    let aggregate = &document["aggregate"];
    assert_json_close(
        &aggregate_priced_in(&[
            serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        ]),
        &aggregate["too_few"],
        "aggregate/too_few",
    );
    let mostly = vec![
        serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        serde_json::json!({"already_priced_in": {"state": "continued"}}),
    ];
    assert_json_close(
        &aggregate_priced_in(&mostly),
        &aggregate["mostly_priced_in"],
        "aggregate/mostly",
    );
    let mixed = vec![
        serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        serde_json::json!({"already_priced_in": {"state": "continued"}}),
        serde_json::json!({"already_priced_in": {"state": "continued"}}),
    ];
    assert_json_close(
        &aggregate_priced_in(&mixed),
        &aggregate["mixed"],
        "aggregate/mixed",
    );
    let ignored = vec![
        serde_json::json!({"already_priced_in": {"state": "move_within_noise"}}),
        serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        serde_json::json!({"already_priced_in": {"state": "likely_priced_in"}}),
        serde_json::json!({"already_priced_in": {"state": "continued"}}),
    ];
    assert_json_close(
        &aggregate_priced_in(&ignored),
        &aggregate["ignores_other"],
        "aggregate/ignores_other",
    );
}
