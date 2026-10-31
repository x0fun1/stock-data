//! Differential parity for News impact bands, breadth scope and publisher tiers.

use std::path::Path;

use sd_news::impact::{
    assess, breadth_scope, magnitude_band, pattern_comparison, source_tier, trailing_sigma,
    ATTRIBUTION_CAVEATS, BAND_TOLERANCE, MAGNITUDE_BANDS, MINIMUM_USABLE_SIGMA, PATTERN_BANDS,
    SOURCE_TIERS,
};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/news-impact/news.json");
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
fn magnitude_and_patterns_match_python() {
    let document = read();
    let closes: Vec<f64> = document["inputs"]["closes"]
        .as_array()
        .expect("closes")
        .iter()
        .filter_map(Value::as_f64)
        .collect();
    for (name, value) in [
        ("severe", Some(0.11)),
        ("boundary_major", Some(0.0999999)),
        ("major", Some(0.05)),
        ("moderate", Some(0.03)),
        ("boundary_minor", Some(0.005)),
        ("negligible", Some(0.001)),
        ("none", None),
    ] {
        assert_json_close(
            &magnitude_band(value, None),
            &document["magnitude"][name],
            &format!("magnitude/{name}"),
        );
    }
    assert_json_close(
        &magnitude_band(Some(0.2), Some("intraday_daily_association")),
        &document["magnitude"]["intraday"],
        "magnitude/intraday",
    );
    assert_json_close(
        &magnitude_band(Some(-0.02), Some("not_classifiable")),
        &document["magnitude"]["unclassifiable"],
        "magnitude/unclassifiable",
    );

    let cases: Vec<(&str, Value, Vec<f64>)> = vec![
        (
            "amplified",
            serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.4}),
            closes.clone(),
        ),
        (
            "consistent",
            serde_json::json!({"status": "confirmed_negative", "daily_close_return": -0.2}),
            closes.clone(),
        ),
        (
            "dampened",
            serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.05}),
            closes.clone(),
        ),
        (
            "inverse",
            serde_json::json!({"status": "event_price_divergence_positive", "daily_close_return": -0.4}),
            closes.clone(),
        ),
        (
            "intraday",
            serde_json::json!({"status": "intraday_daily_association", "daily_close_return": 0.4}),
            closes.clone(),
        ),
        (
            "unclassifiable",
            serde_json::json!({"status": "not_classifiable", "daily_close_return": 0.4}),
            closes.clone(),
        ),
        (
            "degenerate",
            serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.4}),
            vec![100.0; 20],
        ),
    ];
    for (name, reaction, series) in cases {
        assert_json_close(
            &pattern_comparison(&reaction, &series, Some(10)),
            &document["patterns"][name],
            &format!("patterns/{name}"),
        );
    }
    assert_json_close(
        &pattern_comparison(
            &serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.4}),
            &closes,
            None,
        ),
        &document["patterns"]["no_index"],
        "patterns/no_index",
    );
    assert_json_close(
        &pattern_comparison(
            &serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.4}),
            &[],
            Some(3),
        ),
        &document["patterns"]["empty_closes"],
        "patterns/empty_closes",
    );
    let short = trailing_sigma(&[], 0, 20);
    assert_json_close(
        &serde_json::json!([short.0, short.1]),
        &document["patterns"]["trailing_sigma"]["short"],
        "trailing_sigma/short",
    );
    let tiny = trailing_sigma(&[100.0, 100.1], 1, 20);
    assert_json_close(
        &serde_json::json!([tiny.0, tiny.1]),
        &document["patterns"]["trailing_sigma"]["tiny"],
        "trailing_sigma/tiny",
    );
}

#[test]
fn breadth_tiers_and_assess_match_python() {
    let document = read();
    for event_type in [
        "MACRO",
        "EARNINGS",
        "GUIDANCE",
        "ANALYST_REVISION",
        "SUPPLY_CHAIN",
        "COMPETITION",
        "REGULATORY",
        "PRODUCT",
    ] {
        assert_json_close(
            &breadth_scope(Some(event_type)),
            &document["breadth"][event_type],
            &format!("breadth/{event_type}"),
        );
    }
    assert_json_close(
        &breadth_scope(None),
        &document["breadth"]["None"],
        "breadth/null",
    );

    let tier_cases: Vec<(&str, Vec<String>)> = vec![
        ("empty", Vec::new()),
        ("reuters", vec!["Reuters".into()]),
        ("blog_contains_sec", vec!["Security Finance Blog".into()]),
        ("wsj_with_article", vec!["The Wall Street Journal".into()]),
        ("domain_suffix", vec!["Reuters.com".into()]),
        ("relay_suffix", vec!["Reuters - Yahoo Finance".into()]),
        (
            "some_blog_via_reuters",
            vec!["Some Blog via Reuters".into()],
        ),
        (
            "reuters_via_some_blog",
            vec!["Reuters via Some Blog".into()],
        ),
        ("symbol_authority", vec!["AP News".into()]),
        ("yahoo_domain", vec!["finance.yahoo.com".into()]),
        (
            "mixed_cluster",
            vec!["Reuters".into(), "Some Blog".into(), "AP News".into()],
        ),
        (
            "duplicates",
            vec![" Reuters ".into(), "Reuters".into(), "".into()],
        ),
    ];
    for (name, publishers) in tier_cases {
        assert_json_close(
            &source_tier(&publishers),
            &document["tiers"][name],
            &format!("tiers/{name}"),
        );
    }
    // Exact-alias only: a publisher containing "sec"/"ap" must not be upgraded.
    assert_eq!(
        document["tiers"]["blog_contains_sec"]["tier"],
        "unclassified_publisher"
    );
    assert_eq!(
        document["tiers"]["mixed_cluster"]["tier"],
        "unclassified_publisher"
    );
    let mut meanings = serde_json::Map::new();
    for (name, meaning) in SOURCE_TIERS {
        meanings.insert((*name).to_string(), Value::String((*meaning).to_string()));
    }
    assert_json_close(
        &Value::Object(meanings),
        &document["tier_meanings"],
        "tier_meanings",
    );
    assert_eq!(
        Value::Array(
            ATTRIBUTION_CAVEATS
                .iter()
                .map(|caveat| Value::from(*caveat))
                .collect()
        ),
        document["caveats"]
    );
    let bands: Vec<Value> = MAGNITUDE_BANDS
        .iter()
        .map(|(name, threshold)| serde_json::json!([name, threshold]))
        .collect();
    assert_eq!(Value::Array(bands), document["constants"]["bands"]);
    let patterns: Vec<Value> = PATTERN_BANDS
        .iter()
        .map(|(name, threshold)| serde_json::json!([name, threshold]))
        .collect();
    assert_eq!(Value::Array(patterns), document["constants"]["patterns"]);
    assert_json_close(
        &Value::from(BAND_TOLERANCE),
        &document["constants"]["tolerance"],
        "constants/tolerance",
    );
    assert_json_close(
        &Value::from(MINIMUM_USABLE_SIGMA),
        &document["constants"]["minimum_sigma"],
        "constants/minimum_sigma",
    );

    let closes: Vec<f64> = document["inputs"]["closes"]
        .as_array()
        .expect("closes")
        .iter()
        .filter_map(Value::as_f64)
        .collect();
    let positive = assess(
        Some("EARNINGS"),
        &serde_json::json!({"status": "confirmed_positive", "daily_close_return": 0.4}),
        &["Reuters".to_string(), "Some Blog".to_string()],
        &closes,
        Some(10),
    );
    assert_json_close(
        &positive,
        &document["assess"]["positive_card"],
        "assess/positive_card",
    );
    let macro_card = assess(
        Some("MACRO"),
        &serde_json::json!({"status": "intraday_daily_association", "daily_close_return": 0.12}),
        &["Federal Reserve".to_string()],
        &closes,
        Some(10),
    );
    assert_json_close(
        &macro_card,
        &document["assess"]["intraday_macro"],
        "assess/intraday_macro",
    );
    assert_eq!(positive["composite_impact_score"], "not_implemented");
}
