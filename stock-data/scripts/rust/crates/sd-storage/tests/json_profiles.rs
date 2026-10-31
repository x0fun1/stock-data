//! Differential tests for the four legacy JSON profiles.
//!
//! Each fixture directory holds the bytes/digests produced by the unmodified Python
//! runtime. The Rust profiles must reproduce them exactly: a mismatch is a contract
//! change, not a rounding difference, and is never "fixed" by editing the fixture.

use std::path::{Path, PathBuf};

use sd_core::json_text::format_python_float;
use sd_core::numbers::{ensure_finite_payload, FiniteFloat};
use sd_storage::legacy_json::{
    canonical_bytes, canonical_sha256, ledger_event_line, ledger_event_sha256, readable_payload,
    sha256_hex, stage_digest,
};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn read_text(path: &Path) -> String {
    std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()))
}

fn case_names() -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(fixtures().join("json"))
        .expect("json fixtures")
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| !name.starts_with('_'))
        .collect();
    names.sort();
    names
}

#[test]
fn canonical_readable_stage_and_ledger_profiles_match_python_bytes() {
    let names = case_names();
    assert_eq!(
        names.len(),
        14,
        "fixture corpus must keep all 14 JSON cases"
    );
    for name in names {
        let directory = fixtures().join("json").join(&name);
        let input: Value =
            serde_json::from_str(&read_text(&directory.join("input.json"))).expect("input json");

        let expected_canonical =
            std::fs::read(directory.join("canonical.bin")).expect("canonical reference");
        let actual_canonical = canonical_bytes(&input).expect("canonical bytes");
        assert_eq!(
            sha256_hex(&actual_canonical),
            sha256_hex(&expected_canonical),
            "canonical bytes differ for {name}: {} vs {}",
            String::from_utf8_lossy(&actual_canonical),
            String::from_utf8_lossy(&expected_canonical)
        );

        let expected_readable = read_text(&directory.join("readable.json"));
        assert_eq!(
            readable_payload(&input).expect("readable"),
            expected_readable,
            "readable payload differs for {name}"
        );

        let expected_stage = read_text(&directory.join("stage-digest.txt"))
            .trim()
            .to_string();
        assert_eq!(
            stage_digest(&input).expect("stage digest"),
            expected_stage,
            "stage digest differs for {name}"
        );

        let expected_ledger = read_text(&directory.join("ledger-sha256.txt"))
            .trim()
            .to_string();
        assert_eq!(
            ledger_event_sha256(&input).expect("ledger sha"),
            expected_ledger,
            "ledger digest differs for {name}"
        );
        // The ledger line adds exactly one trailing newline to the canonical text.
        let line = ledger_event_line(&input).expect("ledger line");
        assert_eq!(
            line.as_bytes(),
            [actual_canonical.as_slice(), b"\n"].concat()
        );

        // `canonical_sha256` is the same digest the manifests store.
        assert_eq!(
            canonical_sha256(&input).expect("canonical sha"),
            sha256_hex(&expected_canonical)
        );
    }
}

#[test]
fn non_finite_numbers_are_rejected_with_the_python_message() {
    // `serde_json` cannot hold NaN/Infinity, so the guard lives where estimators
    // produce numbers: a non-finite value must fail instead of becoming `null`.
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let error = FiniteFloat::new(value).expect_err("non-finite values are rejected");
        assert_eq!(error.error_type(), "ValueError");
        assert!(
            error
                .message()
                .starts_with("Out of range float values are not JSON compliant: "),
            "unexpected message: {}",
            error.message()
        );
    }
    assert_eq!(FiniteFloat::new(0.5).expect("finite").get(), 0.5);
    // A payload containing only representable numbers passes the defensive scan.
    let payload = serde_json::json!({"a": 1, "b": 0.5, "c": [true, null, "x"]});
    ensure_finite_payload(&payload).expect("finite payload");
}

#[test]
fn python_float_repr_matches_the_reference_formatting() {
    // Values captured from Python's `repr(float)` / `json.dumps`.
    let cases = [
        (1e20_f64, "1e+20"),
        (1e-7, "1e-07"),
        (1.5e-7, "1.5e-07"),
        (123456789012345678.0, "1.2345678901234568e+17"),
        (-0.0, "-0.0"),
        (0.5, "0.5"),
        (1.0, "1.0"),
        (1e16, "1e+16"),
        (1e22, "1e+22"),
        (2.5e-308, "2.5e-308"),
        (100.0, "100.0"),
        (0.001, "0.001"),
        (1e-4, "0.0001"),
        (12345.678, "12345.678"),
    ];
    for (value, expected) in cases {
        assert_eq!(
            format_python_float(value),
            expected,
            "float formatting differs for {value:?}"
        );
    }
}

#[test]
fn rejection_fixtures_record_the_legacy_error() {
    let rejected: Value = serde_json::from_str(&read_text(
        &fixtures().join("json/_rejections/rejected.json"),
    ))
    .expect("rejections");
    let rows = rejected.as_array().expect("rejections is an array");
    assert_eq!(rows.len(), 3);
    for row in rows {
        assert_eq!(row["error_type"], "ValueError");
        assert!(row["error"]
            .as_str()
            .unwrap()
            .starts_with("Out of range float values are not JSON compliant: "));
    }
}
