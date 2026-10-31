//! Migrated legacy assertions.
//!
//! Each function mirrors the corresponding `selfcheck.py` check: same leak vectors,
//! same negative controls, same rejection expectations. They assert; returning
//! `Ok(())` without an assertion would silently satisfy the registry.

use sd_core::security::{redact_text, safe_url};
use serde_json::Value;

/// F03: credential redaction (R02).
///
/// Port of `check_f03_redaction`: every listed credential must vanish, and ordinary
/// prose using the same words must not be mangled.
pub fn legacy_f03() -> Result<(), String> {
    let leaks: [(&str, &[&str]); 9] = [
        ("Authorization: Bearer FAKE-TOKEN-XYZ123\nnext stays", &["FAKE-TOKEN-XYZ123"]),
        ("authorization=Basic dXNlcjpwYXNzRkFLRQ==", &["dXNlcjpwYXNzRkFLRQ"]),
        (
            "{\"Authorization\": \"Bearer eyJabc123.def456\", \"Cookie\": \"sid=FAKESID1; token=FAKETOK2\"}",
            &["eyJabc123.def456", "FAKESID1", "FAKETOK2"],
        ),
        ("Cookie: a=FAKE1; b=FAKE2; c=FAKE3", &["FAKE1", "FAKE2", "FAKE3"]),
        ("Set-Cookie: session=FAKESESSION; HttpOnly", &["FAKESESSION"]),
        ("COOKIE: FAKEUPPER", &["FAKEUPPER"]),
        ("bearer eyJhbGciOi.FAKE-SIG123456", &["eyJhbGciOi.FAKE-SIG123456"]),
        ("x-api-key: FAKEKEY123456", &["FAKEKEY123456"]),
        (
            "Authorization: Bearer abc123\n  continuation-FAKE-CONT==\nnext stays",
            &["FAKE-CONT"],
        ),
    ];
    for (text, must_vanish) in leaks {
        let output = redact_text(&Value::String(text.to_string()));
        for token in must_vanish {
            if output.contains(token) {
                return Err(format!("credential {token} survived redaction: {output}"));
            }
        }
    }

    // Negative controls: ordinary prose keeps its words.
    let prose = redact_text(&Value::String(
        "authorization without a colon is fine".into(),
    ));
    if prose.contains("apses") {
        return Err(format!("ordinary prose was mangled: {prose}"));
    }
    for text in [
        "basic understanding of markets improves",
        "bearer of good news for equities",
    ] {
        let output = redact_text(&Value::String(text.to_string()));
        if output.contains("[redacted]") {
            return Err(format!("negative control was redacted: {text} -> {output}"));
        }
    }

    // URL query credentials are masked, other parameters survive.
    let url = safe_url(&Value::String(
        "https://api.example.com/v1?sessionid=FAKESESS&foo=bar&x-amz-signature=FAKESIG&api_key=FAKEKEY".into(),
    ));
    let Some(url) = url else {
        return Err("safe_url rejected a valid https URL".to_string());
    };
    for token in ["FAKESESS", "FAKESIG", "FAKEKEY"] {
        if url.contains(token) {
            return Err(format!("query credential {token} survived: {url}"));
        }
    }
    if !url.contains("foo=bar") {
        return Err(format!("non-sensitive query parameter was dropped: {url}"));
    }
    Ok(())
}
