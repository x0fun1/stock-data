//! Differential parity for News point-in-time normalization and clustering.

use std::path::Path;

use sd_news::deduplicate::{clean_url, deduplicate_articles, title_key};
use sd_news::normalize::{
    is_opinion_record, normalize_articles, normalize_source_sentiment, record_role,
};
use serde_json::Value;

fn read() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/news-normalize/news.json");
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

fn strings(value: Vec<String>) -> Value {
    Value::Array(value.into_iter().map(Value::String).collect())
}

#[test]
fn normalization_matches_python() {
    let document = read();
    let inputs = &document["inputs"];
    let asof = inputs["asof"].as_str().expect("asof");
    let payload = inputs["articles_payload"].clone();
    let normalized = normalize_articles(&payload, asof);
    assert_json_close(
        &Value::Array(normalized.articles.clone()),
        &document["normalized"],
        "normalized",
    );
    assert_json_close(
        &Value::Object(normalized.excluded.clone()),
        &document["excluded"],
        "excluded",
    );
    assert_json_close(
        &strings(normalized.warnings.clone()),
        &document["warnings"],
        "warnings",
    );

    let (sentiment, warnings) = normalize_source_sentiment(&payload, None, asof);
    assert_json_close(
        &sentiment.unwrap_or(Value::Null),
        &document["sentiment"],
        "sentiment",
    );
    assert_json_close(
        &strings(warnings),
        &document["sentiment_warnings"],
        "sentiment_warnings",
    );

    for (name, key) in [
        ("undeclared_scale", "undeclared"),
        ("percent_exceed", "percent_exceed"),
        ("late", "late"),
        ("missing_timestamp", "missing"),
    ] {
        let block = serde_json::json!({"source_sentiment": inputs[key].clone()});
        let (result, case_warnings) = normalize_source_sentiment(&block, None, asof);
        let expected = document["sentiment_cases"][name]
            .as_array()
            .expect("case tuple");
        assert_json_close(
            &result.clone().unwrap_or(Value::Null),
            &expected[0],
            &format!("{name}/result"),
        );
        assert_json_close(
            &strings(case_warnings),
            &expected[1],
            &format!("{name}/warnings"),
        );
    }
    let (result, warnings) =
        normalize_source_sentiment(&serde_json::json!({"source_sentiment": "nope"}), None, asof);
    assert!(result.is_none());
    assert!(warnings.is_empty());

    let roles: Vec<Value> = normalized
        .articles
        .iter()
        .map(|article| serde_json::json!({"role": record_role(article), "opinion": is_opinion_record(article)}))
        .collect();
    assert_json_close(&Value::Array(roles), &document["roles"], "roles");

    let bad = normalize_articles(&Value::String("nope".into()), asof);
    assert!(bad.articles.is_empty());
    assert_eq!(
        bad.warnings,
        vec!["news.data must be an array or an object with an articles array".to_string()]
    );
    let mixed = normalize_articles(
        &serde_json::json!([{"title": "ok", "published_at": "2026-01-05T00:00:00Z"}, 7, "x"]),
        asof,
    );
    assert_eq!(mixed.articles.len(), 1);
    assert_eq!(mixed.excluded["invalid_record"], 2);
}

#[test]
fn deduplication_matches_python() {
    let document = read();
    let inputs = &document["inputs"];
    let records = inputs["dedupe_records"]
        .as_array()
        .expect("records")
        .clone();
    let result = deduplicate_articles(&records);
    let expected = &document["dedup"];
    assert_eq!(
        Value::from(result.duplicate_count),
        expected["duplicate_count"]
    );
    assert_eq!(
        Value::from(result.clusters.len()),
        expected["cluster_count"]
    );
    assert_json_close(
        &Value::Array(result.clusters),
        &expected["clusters"],
        "clusters",
    );
    let urls: Vec<Value> = [
        "https://Example.com/a/b/?utm_medium=x&fbclid=1&q=2#frag",
        "https://example.com/a/b",
    ]
    .iter()
    .map(|url| {
        clean_url(Some(url))
            .map(Value::String)
            .unwrap_or(Value::Null)
    })
    .collect();
    let expected_urls = expected["clean_urls"].as_array().expect("urls");
    assert_eq!(urls[0], expected_urls[0]);
    assert_eq!(urls[1], expected_urls[1]);
    let keys = vec![
        Value::String(title_key("Company Beats Estimates!")),
        Value::String(title_key("")),
    ];
    assert_eq!(Value::Array(keys), expected["title_keys"]);
}
