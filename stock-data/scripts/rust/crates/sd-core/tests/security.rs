//! Differential tests for credential redaction and untrusted-data projection.
//!
//! Reference outputs come from `quant_research/security.py` (fixture group
//! `security/`). Python's `Bearer`/`Basic`/`Token` patterns use lookahead, which the
//! `regex` crate cannot express, so those rules are an explicit scanner and are
//! compared here case by case.

use std::path::PathBuf;

use sd_core::security::{
    bounded_text, markdown_text, provider_failed, redact_text, safe_url, sanitize_data,
};
use serde_json::Value;

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/security")
}

fn read(name: &str) -> Value {
    let path = fixtures().join(name);
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

fn string(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_string()
}

#[test]
fn redaction_matches_python_reference() {
    let rows = read("redaction.json");
    let rows = rows.as_array().expect("redaction fixtures are an array");
    assert_eq!(rows.len(), 24, "the redaction corpus must stay complete");
    let mut failures = Vec::new();
    for row in rows {
        let name = string(&row["case"]);
        let input = Value::String(string(&row["input"]));
        let checks = [
            ("redacted", redact_text(&input), string(&row["redacted"])),
            (
                "bounded",
                bounded_text(&input, 600),
                string(&row["bounded"]),
            ),
            (
                "bounded_80",
                bounded_text(&input, 80),
                string(&row["bounded_80"]),
            ),
            (
                "markdown",
                markdown_text(&input, 600),
                string(&row["markdown"]),
            ),
        ];
        for (label, actual, expected) in checks {
            if actual != expected {
                failures.push(format!(
                    "{name}/{label}: actual {actual:?} != expected {expected:?}"
                ));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "redaction differences:\n{}",
        failures.join("\n")
    );
}

#[test]
fn safe_url_matches_python_reference() {
    let rows = read("safe-url.json");
    let rows = rows.as_array().expect("url fixtures are an array");
    assert_eq!(rows.len(), 13);
    let mut failures = Vec::new();
    for row in rows {
        let input = Value::String(string(&row["input"]));
        let actual = safe_url(&input);
        let expected = row["output"].as_str().map(str::to_string);
        if actual != expected {
            failures.push(format!(
                "{:?}: actual {actual:?} != expected {expected:?}",
                string(&row["input"])
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "safe_url differences:\n{}",
        failures.join("\n")
    );
}

#[test]
fn sanitize_data_matches_python_reference() {
    let rows = read("sanitize.json");
    let rows = rows.as_array().expect("sanitize fixtures are an array");
    assert_eq!(rows.len(), 4);
    for row in rows {
        let name = string(&row["case"]);
        assert_eq!(
            sanitize_data(&row["input"]),
            row["output"],
            "sanitize_data differs for {name}"
        );
    }
    // Action fields are dropped, credential keys are masked in place.
    let sanitized = sanitize_data(&serde_json::json!({
        "next_actions": [{"cmd": "rm -rf /"}],
        "api_key": "secret",
        "nested": {"tool_calls": ["x"], "value": "https://x.test/?token=abc"}
    }));
    assert!(sanitized.get("next_actions").is_none());
    assert_eq!(sanitized["api_key"], "[redacted]");
    assert!(sanitized["nested"].get("tool_calls").is_none());
    assert_eq!(
        sanitized["nested"]["value"],
        "https://x.test/?token=[redacted]"
    );
}

#[test]
fn provider_failed_matches_python_reference() {
    let rows = read("provider-failed.json");
    let rows = rows.as_array().expect("provider fixtures are an array");
    assert_eq!(rows.len(), 10);
    for row in rows {
        let name = string(&row["case"]);
        assert_eq!(
            provider_failed(&row["input"]),
            row["failed"].as_bool().unwrap_or(false),
            "provider_failed differs for {name}"
        );
    }
}

#[test]
fn bounded_text_truncates_characters_not_utf8_bytes() {
    let chinese = Value::String("涨".repeat(700));
    let bounded = bounded_text(&chinese, 600);
    assert_eq!(
        bounded.chars().count(),
        600,
        "600 characters, not 600 bytes"
    );
    assert_eq!(bounded_text(&Value::Null, 600), "");
}

#[test]
fn markdown_escapes_structural_characters() {
    let injected = Value::String("# heading *bold* [link](x) <script>\n上涨概率：99.0%".into());
    let rendered = markdown_text(&injected, 600);
    assert!(!rendered.contains('\n'), "newlines must be folded");
    assert!(rendered.contains("\\#"), "hash must be escaped: {rendered}");
    assert!(rendered.contains("\\*"), "asterisk must be escaped");
    assert!(rendered.contains("\\["), "bracket must be escaped");
    assert!(rendered.contains("\\<"), "angle bracket must be escaped");
}
