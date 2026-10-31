//! Differential parity for host-supplied AI lean validation and reporting.

use std::path::Path;

use sd_news::ai_assessment::{
    attach_lean_to_records, build_ai_lean_block, lean_candidates, validate_ai_assessment,
    AI_ASSESSMENT_SCHEMA_VERSION, AI_LEAN_BASIS, AI_LEAN_LAYER, EVIDENCE_GRADES, IMPORTANCE,
    LEAN_LABELS, TARGET_RELEVANCE,
};
use serde_json::Value;

fn read() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/news-ai/news.json");
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
fn validation_and_block_match_python() {
    let document = read();
    let candidates = document["candidates"]
        .as_array()
        .expect("candidates")
        .clone();
    let assessment = document["lean_id_inputs"]["assessment"].clone();
    let validated = validate_ai_assessment(&assessment, "SYN", "2026-01-05T21:00:00Z", &candidates)
        .expect("valid assessment");
    assert_json_close(&validated, &document["valid"], "valid");

    // The lean id must not depend on JSON key order.
    let reordered = document["lean_id_inputs"]["other_order"].clone();
    let second = validate_ai_assessment(&reordered, "SYN", "2026-01-05T21:00:00Z", &candidates)
        .expect("reordered assessment");
    assert_eq!(second["lean_id"], validated["lean_id"]);

    assert_json_close(
        &build_ai_lean_block(Some(&validated), &candidates, "assessed", None, None),
        &document["block"],
        "block",
    );
    let warnings = vec!["lean coverage is partial".to_string()];
    assert_json_close(
        &build_ai_lean_block(
            Some(&validated),
            &candidates,
            "partial",
            Some("some items unassessed"),
            Some(&warnings),
        ),
        &document["block_with_warnings"],
        "block_with_warnings",
    );
    assert_json_close(
        &build_ai_lean_block(
            None,
            &candidates,
            "invalid",
            Some("host artifact rejected"),
            Some(&[]),
        ),
        &document["block_invalid"],
        "block_invalid",
    );
    assert_json_close(
        &build_ai_lean_block(None, &[], "not_supplied", None, None),
        &document["block_empty"],
        "block_empty",
    );

    let constants = &document["constants"];
    assert_eq!(
        Value::from(AI_ASSESSMENT_SCHEMA_VERSION),
        constants["schema_version"]
    );
    assert_eq!(Value::from(AI_LEAN_BASIS), constants["basis"]);
    assert_eq!(Value::from(AI_LEAN_LAYER), constants["layer"]);
    assert_eq!(
        Value::Array(
            LEAN_LABELS
                .iter()
                .map(|label| Value::from(*label))
                .collect()
        ),
        constants["lean_labels"]
    );
    assert_eq!(
        Value::Array(
            EVIDENCE_GRADES
                .iter()
                .map(|grade| Value::from(*grade))
                .collect()
        ),
        constants["evidence_grades"]
    );
    assert_eq!(
        Value::Array(
            TARGET_RELEVANCE
                .iter()
                .map(|value| Value::from(*value))
                .collect()
        ),
        constants["target_relevance"]
    );
    assert_eq!(
        Value::Array(IMPORTANCE.iter().map(|value| Value::from(*value)).collect()),
        constants["importance"]
    );
}

#[test]
fn rejection_messages_match_python() {
    let document = read();
    let candidates = document["candidates"]
        .as_array()
        .expect("candidates")
        .clone();
    let valid = &document["lean_id_inputs"]["assessment"];
    let mutations: Vec<(&str, Value)> = vec![
        ("schema", serde_json::json!({"schema_version": "2.0"})),
        ("schema_unknown_field", serde_json::json!({"extra": 1})),
        ("ticker", serde_json::json!({"ticker": "OTHER"})),
        (
            "asof",
            serde_json::json!({"asof_timestamp": "2026-01-05T20:00:00Z"}),
        ),
        ("bad_basis", serde_json::json!({"basis": "other"})),
        ("model_not_object", serde_json::json!({"model": "x"})),
        (
            "model_unknown",
            serde_json::json!({"model": {"id": "m", "extra": "x"}}),
        ),
        ("items_empty", serde_json::json!({"items": []})),
        ("items_not_array", serde_json::json!({"items": "x"})),
        (
            "unknown_id",
            serde_json::json!({"items": [{"news_id": "ghost", "lean": "bullish", "reason": "r"}]}),
        ),
        (
            "duplicate",
            serde_json::json!({"items": [
                {"news_id": "event-1", "lean": "bullish", "reason": "r"},
                {"news_id": "event-1", "lean": "bearish", "reason": "r"}
            ]}),
        ),
        (
            "bad_lean",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "strong", "reason": "r"}]}),
        ),
        (
            "bad_grade",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "evidence_grade": "vibes"}]}),
        ),
        (
            "bad_relevance",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "target_relevance": "maybe"}]}),
        ),
        (
            "bad_importance",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "importance": "huge"}]}),
        ),
        (
            "forbidden_reason",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "probability rises"}]}),
        ),
        (
            "forbidden_headline",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "headline": "consensus up"}]}),
        ),
        (
            "headline_mismatch",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "headline": "invented headline"}]}),
        ),
        (
            "source_mismatch",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "source": "Bloomberg"}]}),
        ),
        (
            "url_mismatch",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "url": "https://example.com/other"}]}),
        ),
        (
            "published_mismatch",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish", "reason": "r", "published_at": "2026-01-05T13:00:00Z"}]}),
        ),
        (
            "missing_reason",
            serde_json::json!({"items": [{"news_id": "event-1", "lean": "bullish"}]}),
        ),
    ];
    for (name, mutation) in mutations {
        let mut merged = valid.clone();
        for (key, value) in mutation.as_object().expect("object") {
            merged[key] = value.clone();
        }
        let expected = document["errors"][name]
            .as_str()
            .unwrap_or_else(|| panic!("fixture error for {name} missing"));
        let actual = validate_ai_assessment(&merged, "SYN", "2026-01-05T21:00:00Z", &candidates)
            .expect_err("should be rejected");
        assert_eq!(actual, expected, "error message for {name}");
    }
    let not_object = validate_ai_assessment(
        &Value::String("nope".into()),
        "SYN",
        "2026-01-05T21:00:00Z",
        &candidates,
    )
    .expect_err("not object");
    assert_eq!(
        not_object,
        document["errors"]["not_object"]
            .as_str()
            .expect("fixture message")
    );
}

#[test]
fn candidates_and_attachment_match_python() {
    let document = read();
    let events = vec![
        serde_json::json!({"event_id": "event-1", "headline": "Company beats estimates", "summary": "s",
            "sources": ["Reuters", "AP"], "first_published_at": "2026-01-05T12:00:00Z",
            "url": "https://example.com/a", "direction": "positive", "event_type": "EARNINGS",
            "ticker_relevance": "direct", "eligible_evidence": true, "exclusion_reasons": []}),
        serde_json::json!({"event_id": "", "headline": "no id", "first_published_at": "2026-01-05T10:00:00Z"}),
    ];
    let opinions = vec![
        serde_json::json!({"opinion_id": "opinion-1", "headline": "Strong",
        "summary": "s", "sources": ["StockTwits"], "published_at": "2026-01-05T14:00:00Z",
        "url": "https://example.com/o", "sentiment": "positive", "eligible_evidence": true}),
    ];
    let unclassified = vec![
        serde_json::json!({"news_id": "unclassified-1", "headline": "Mystery",
        "summary": "s", "sources": ["Blog"], "published_at": "2026-01-04T09:00:00Z", "url": null,
        "ticker_relevance": "indirect", "reason": "unclassified"}),
    ];
    assert_json_close(
        &Value::Array(lean_candidates(
            &events,
            &opinions,
            Some(&unclassified),
            500,
        )),
        &document["candidates_from_records"],
        "candidates",
    );
    assert_json_close(
        &Value::Array(lean_candidates(&events, &opinions, Some(&unclassified), 2)),
        &document["candidates_limited"],
        "candidates_limited",
    );

    let candidates = document["candidates"]
        .as_array()
        .expect("candidates")
        .clone();
    let assessment = document["lean_id_inputs"]["assessment"].clone();
    let validated = validate_ai_assessment(&assessment, "SYN", "2026-01-05T21:00:00Z", &candidates)
        .expect("valid assessment");
    let block = build_ai_lean_block(Some(&validated), &candidates, "assessed", None, None);
    let mut result = serde_json::json!({
        "major_events": [
            {"event_id": "event-1", "headline": "Company beats estimates"},
            {"event_id": "missing", "headline": "no lean"}
        ],
        "opinion_records": [{"opinion_id": "opinion-1", "headline": "Strong"}],
        "background_records": [{"event_id": "unclassified-1"}],
    });
    attach_lean_to_records(&mut result, &block);
    assert_json_close(&result, &document["attached"], "attached");
}
