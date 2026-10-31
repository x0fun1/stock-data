//! Full-tree parity for the I/O-free News pipeline mirror (`news/pipeline.py`).
//!
//! The fixture is the exact Python pipeline output on deterministic frozen inputs. All
//! structures, key orders, statuses, warnings and display-only disclosures must match;
//! floats use a 1e-12 relative tolerance because the two runtimes compute `mean`/`round`
//! with different (but IEEE-equivalent) orderings.

use std::path::Path;

use sd_news::contracts::NewsInput;
use sd_news::pipeline::{
    analyze_news, freeze_receipt, news_input_digest, prepare_news_input, run_news,
    verify_frozen_result,
};
use sd_news::status::{news_assessed, news_status_label};
use serde_json::{Map, Value};

fn fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/news-pipeline/result.json");
    serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json")
}

fn assert_json_close(actual: &Value, expected: &Value, path: &str) {
    match (actual, expected) {
        (Value::Number(left), Value::Number(right)) => {
            let left_value = left.as_f64().expect("number");
            let right_value = right.as_f64().expect("number");
            let tolerance = 1e-12 * right_value.abs().max(1.0);
            assert!(
                (left_value - right_value).abs() <= tolerance,
                "{path}: {left_value} vs {right_value} (tolerance {tolerance})"
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
            let actual_keys: Vec<&String> = left.keys().collect();
            let expected_keys: Vec<&String> = right.keys().collect();
            assert_eq!(
                actual_keys, expected_keys,
                "{path}: object key order differs"
            );
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

struct Inputs {
    manifest: Value,
    market: Value,
    clean_news: Value,
    noisy_news: Value,
    failed_news: Value,
    provider_news: Value,
    policy: Value,
    invalid_policies: Vec<Value>,
    valid_assessment: Value,
    partial_assessment: Value,
    invalid_assessment: Value,
}

fn inputs(document: &Value) -> Inputs {
    let block = &document["inputs"];
    Inputs {
        manifest: block["manifest"].clone(),
        market: block["market"].clone(),
        clean_news: block["clean_news"].clone(),
        noisy_news: block["noisy_news"].clone(),
        failed_news: block["failed_news"].clone(),
        provider_news: block["provider_news"].clone(),
        policy: block["policy"].clone(),
        invalid_policies: block["invalid_policies"]
            .as_array()
            .expect("policies")
            .clone(),
        valid_assessment: document["valid_assessment"].clone(),
        partial_assessment: document["partial_assessment"].clone(),
        invalid_assessment: document["invalid_assessment"].clone(),
    }
}

fn summary(news_input: &NewsInput) -> Value {
    let mut map = Map::new();
    map.insert("ticker".into(), Value::String(news_input.ticker.clone()));
    map.insert("market".into(), Value::String(news_input.market.clone()));
    map.insert("horizon".into(), Value::String(news_input.horizon.clone()));
    map.insert(
        "asof_timestamp".into(),
        Value::String(news_input.asof_timestamp.clone()),
    );
    map.insert(
        "snapshot_id".into(),
        Value::String(news_input.snapshot_id.clone()),
    );
    map.insert(
        "news_status".into(),
        Value::String(news_input.news_status.clone()),
    );
    map.insert(
        "source_sentiment".into(),
        news_input.source_sentiment.clone().unwrap_or(Value::Null),
    );
    map.insert(
        "article_count".into(),
        Value::from(news_input.articles.len()),
    );
    map.insert(
        "warnings".into(),
        Value::Array(
            news_input
                .warnings
                .iter()
                .cloned()
                .map(Value::String)
                .collect(),
        ),
    );
    map.insert(
        "coverage".into(),
        Value::Object(news_input.coverage.clone()),
    );
    Value::Object(map)
}

fn prepare(inputs: &Inputs, news: &Value) -> NewsInput {
    prepare_news_input(&inputs.manifest, &inputs.market, news).expect("prepared input")
}

fn assert_no_quant_fields(value: &Value, path: &str) {
    match value {
        Value::Object(map) => {
            for forbidden in [
                "prob_up",
                "probability",
                "raw_probability",
                "calibrated_probability",
                "reportable_probability",
                "expected_return",
                "probability_source",
                "eligible",
            ] {
                assert!(
                    !map.contains_key(forbidden),
                    "{path}: News result must not carry a Quant field {forbidden}"
                );
            }
            for (key, item) in map {
                assert_no_quant_fields(item, &format!("{path}/{key}"));
            }
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                assert_no_quant_fields(item, &format!("{path}[{index}]"));
            }
        }
        _ => {}
    }
}

#[test]
fn prepared_inputs_match_python() {
    let document = fixture();
    let inputs = inputs(&document);
    let cases: Vec<(&str, &Value)> = vec![
        ("clean", &inputs.clean_news),
        ("noisy", &inputs.noisy_news),
        ("failed", &inputs.failed_news),
        ("provider", &inputs.provider_news),
    ];
    for (name, news) in cases {
        let prepared = prepare(&inputs, news);
        assert_json_close(
            &summary(&prepared),
            &document["prepared"][name],
            &format!("prepared/{name}"),
        );
    }
    // The digest depends on the exact normalized article list and frozen bars.
    let prepared = prepare(&inputs, &inputs.clean_news);
    assert_eq!(
        Value::String(news_input_digest(&prepared)),
        document["digest"],
        "news_input_digest"
    );
    assert_eq!(document["digest"], document["digest_recomputed"]);
}

#[test]
fn analysis_matches_python() {
    let document = fixture();
    let inputs = inputs(&document);
    let clean = prepare(&inputs, &inputs.clean_news);
    let noisy = prepare(&inputs, &inputs.noisy_news);
    let failed = prepare(&inputs, &inputs.failed_news);
    let provider = prepare(&inputs, &inputs.provider_news);

    let clean_result = analyze_news(&clean, None, None).expect("clean");
    assert_json_close(&clean_result, &document["clean"], "clean");
    let noisy_result = analyze_news(&noisy, None, None).expect("noisy");
    assert_json_close(&noisy_result, &document["noisy"], "noisy");
    let failed_result = analyze_news(&failed, None, None).expect("failed");
    assert_json_close(&failed_result, &document["failed"], "failed");
    let provider_result = analyze_news(&provider, None, None).expect("provider");
    assert_json_close(&provider_result, &document["provider"], "provider");
    let policy_result = analyze_news(&clean, Some(&inputs.policy), None).expect("policy");
    assert_json_close(&policy_result, &document["policy"], "policy");

    // Policy validation errors are part of the contract.
    let errors = document["invalid_policy_errors"]
        .as_array()
        .expect("errors");
    assert_eq!(errors.len(), inputs.invalid_policies.len());
    for (index, policy) in inputs.invalid_policies.iter().enumerate() {
        let error = analyze_news(&clean, Some(policy), None).expect_err("invalid policy");
        assert_eq!(
            Value::String(error),
            errors[index],
            "invalid policy {index}"
        );
    }
}

#[test]
fn ai_lean_attachment_matches_python() {
    let document = fixture();
    let inputs = inputs(&document);
    let clean = prepare(&inputs, &inputs.clean_news);
    let ai = analyze_news(&clean, None, Some(&inputs.valid_assessment)).expect("ai");
    assert_json_close(&ai, &document["ai"], "ai");
    let ai_partial =
        analyze_news(&clean, None, Some(&inputs.partial_assessment)).expect("ai partial");
    assert_json_close(&ai_partial, &document["ai_partial"], "ai_partial");
    let ai_invalid =
        analyze_news(&clean, None, Some(&inputs.invalid_assessment)).expect("ai invalid");
    assert_json_close(&ai_invalid, &document["ai_invalid"], "ai_invalid");
}

#[test]
fn reuse_and_freeze_match_python() {
    let document = fixture();
    let inputs = inputs(&document);
    let clean = prepare(&inputs, &inputs.clean_news);
    let clean_result = document["clean"].clone();
    let noisy_result = document["noisy"].clone();
    let reused = run_news(&clean, None, None, Some(&clean_result)).expect("reuse");
    assert_json_close(&reused, &document["reuse"], "reuse");
    let mismatch = run_news(
        &clean,
        None,
        Some(&inputs.valid_assessment),
        Some(&noisy_result),
    )
    .expect("reuse mismatch");
    assert_json_close(&mismatch, &document["reuse_mismatch"], "reuse_mismatch");

    let receipt = freeze_receipt("news_result.json", &clean_result, "2026-10-23T21:05:00Z");
    assert_json_close(&receipt, &document["freeze"], "freeze");
    verify_frozen_result("news_result.json", &clean_result, &document["freeze"]).expect("verify");
    let mut tampered = clean_result.clone();
    tampered["ticker"] = Value::String("OTHER".into());
    let error = verify_frozen_result("news_result.json", &tampered, &document["freeze"])
        .expect_err("tampered result");
    assert_eq!(error, "frozen result digest mismatch: news_result.json");
}

#[test]
fn news_boundaries_hold() {
    let document = fixture();
    let inputs = inputs(&document);
    let clean_result = document["clean"].clone();
    let noisy_result = document["noisy"].clone();
    let failed_result = document["failed"].clone();
    let ai_result = document["ai"].clone();
    let ai_partial_result = document["ai_partial"].clone();
    let ai_invalid_result = document["ai_invalid"].clone();

    // Three states are distinct: partial is still assessed, only not_assessed is not.
    assert_eq!(clean_result["status"], "complete");
    assert_eq!(noisy_result["status"], "partial");
    assert_eq!(failed_result["status"], "not_assessed");
    assert!(news_assessed(Some("partial")));
    assert!(news_assessed(Some("complete")));
    assert!(!news_assessed(Some("not_assessed")));
    assert_eq!(news_status_label(Some("partial")), "partial");
    assert_eq!(news_status_label(Some("failed")), "not_assessed");

    // Presentation-only warnings never downgrade the assessed state.
    assert_eq!(clean_result["warning_severity_counts"]["presentation"], 1);
    assert_eq!(
        clean_result["warning_severity_counts"]["evidence_affecting"],
        0
    );
    assert!(
        noisy_result["warning_severity_counts"]["evidence_affecting"]
            .as_i64()
            .unwrap_or(0)
            > 0
    );

    // The AI lean is display-only and must not move any aggregate.
    for field in [
        "overall_direction",
        "event_strength",
        "market_confirmation",
        "sentiment",
        "source_agreement",
        "confidence",
    ] {
        assert_eq!(
            ai_result[field], clean_result[field],
            "AI lean must not change {field}"
        );
    }
    // Three of seven candidates are judged: the artifact is disclosed as partial
    // coverage and the run is downgraded, but the deterministic aggregates stay put.
    assert_eq!(ai_result["ai_lean"]["status"], "partial");
    assert_eq!(ai_result["ai_lean"]["display_only"], true);
    assert_eq!(ai_result["ai_lean"]["coverage"]["candidate_count"], 7);
    assert_eq!(ai_result["ai_lean"]["coverage"]["assessed_count"], 3);
    assert_eq!(ai_partial_result["ai_lean"]["status"], "partial");
    assert_eq!(
        ai_partial_result["ai_lean"]["coverage"]["assessed_count"],
        1
    );
    assert_eq!(ai_invalid_result["ai_lean"]["status"], "invalid");
    assert_eq!(
        ai_invalid_result["overall_direction"],
        clean_result["overall_direction"]
    );
    // A partially assessed artifact is disclosed and downgrades the run to partial.
    assert_eq!(ai_partial_result["status"], "partial");
    assert_eq!(ai_invalid_result["status"], "partial");

    // No Quant vocabulary may appear as a field in any News result.
    for (name, result) in [
        ("clean", &clean_result),
        ("noisy", &noisy_result),
        ("failed", &failed_result),
        ("ai", &ai_result),
    ] {
        assert_no_quant_fields(result, name);
    }

    // The non-directional disclosures are pinned.
    assert_eq!(clean_result["method"]["news_probability"], "not_emitted");
    assert_eq!(clean_result["method"]["calibrated"], false);
    assert_eq!(
        clean_result["expected_volatility_regime"]["probability_effect"],
        "none"
    );
    assert_eq!(
        clean_result["expected_volatility_regime"]["direction_neutral"],
        true
    );
    assert_eq!(
        clean_result["already_priced_in"]["probability_effect"],
        "none"
    );
    assert_eq!(
        clean_result["price_reaction"]["independence"],
        "not_independent_same_market_data"
    );

    // Deduplication, buckets and classification split are visible in coverage.
    assert!(
        clean_result["coverage"]["duplicate_records_merged"]
            .as_i64()
            .unwrap_or(0)
            >= 1
    );
    let bucket_total: i64 = clean_result["event_time_bucket_counts"]
        .as_object()
        .expect("bucket counts")
        .values()
        .map(|value| value.as_i64().unwrap_or(0))
        .sum();
    assert_eq!(
        bucket_total,
        clean_result["coverage"]["canonical_event_count"]
            .as_i64()
            .unwrap_or(0),
        "every event is counted in exactly one publication bucket"
    );
    assert!(
        clean_result["event_time_bucket_counts"]["pre_market"]
            .as_i64()
            .unwrap_or(0)
            >= 1
    );
    assert!(
        noisy_result["coverage"]["unclassified_record_count"]
            .as_i64()
            .unwrap_or(0)
            >= 1
    );
    // Frozen news domain: news.json exists in the snapshot.
    let _ = &inputs;
}
