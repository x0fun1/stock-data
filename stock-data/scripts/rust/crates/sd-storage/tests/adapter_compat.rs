//! Adapter differential tests: the Rust receipt must equal the Python receipt.
//!
//! Each case records the market response (input) and the receipt produced by
//! `StockDataAdapter.make_receipt` with a fixed capture time (expected). Equality is
//! on the parsed JSON tree, which also pins every key that survives sanitization.

use std::path::{Path, PathBuf};

use sd_core::clock::FixedClock;
use sd_storage::stock_data_adapter::make_receipt;
use serde_json::{Map, Value};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/adapter")
}

fn read(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("cannot parse {}: {error}", path.display()))
}

fn clock() -> FixedClock {
    FixedClock::from_timestamp("2026-10-24T00:00:00Z").expect("fixed capture clock")
}

fn responses(input: &Value) -> Map<String, Value> {
    input.as_object().cloned().expect("input is a domain map")
}

fn receipt_for(request: &Value, input: &Value) -> Value {
    make_receipt(
        request,
        &responses(input),
        Some("2026-10-24T00:00:00Z"),
        &clock(),
    )
    .expect("receipt")
}

#[test]
fn adapter_receipts_match_python_for_every_case() {
    let cases = read(&fixtures().join("cases.json"));
    let names: Vec<&str> = cases
        .as_array()
        .expect("cases")
        .iter()
        .map(|name| name.as_str().unwrap())
        .collect();
    assert_eq!(names.len(), 6, "the adapter corpus must stay complete");
    let request = read(&fixtures().join("request.json"));
    for name in names {
        let directory = fixtures().join(name);
        let input = read(&directory.join("input.json"));
        let expected = read(&directory.join("expected.json"));
        let actual = receipt_for(&request, &input);
        assert_eq!(actual, expected, "adapter receipt differs for {name}");
    }
}

#[test]
fn missing_request_asof_resolves_to_the_collector_provenance() {
    let request = read(&fixtures().join("provenance-request.json"));
    let directory = fixtures().join("asof-from-provenance");
    let input = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected.json"));
    let actual = receipt_for(&request, &input);
    assert_eq!(actual, expected);
    // The resolved as-of is the collector's recorded instant, not the capture time.
    assert_eq!(actual["request"]["asof"], "2026-10-23T21:00:00Z");
    assert_ne!(actual["request"]["asof"], actual["captured_at_utc"]);
}

#[test]
fn trust_boundary_holds_for_hostile_payloads() {
    let request = read(&fixtures().join("request.json"));
    let directory = fixtures().join("action-and-credentials");
    let input = read(&directory.join("input.json"));
    let expected = read(&directory.join("expected.json"));
    let receipt = receipt_for(&request, &input);
    let market = &receipt["domains"]["market"];
    // Action fields are dropped by the field whitelist before any redaction runs.
    assert!(market.get("next_actions").is_none());
    assert!(market.get("tool_calls").is_none());
    assert!(
        market.get("api_key").is_none(),
        "unknown credential keys are not copied into the block"
    );
    // The note is redacted exactly as Python does: the URL query credential is masked,
    // and the keyword rule then consumes the remainder of the unquoted value
    // (`&b=2` disappears). That legacy behaviour is preserved, not "improved".
    assert_eq!(
        market["data"]["note"],
        expected["domains"]["market"]["data"]["note"]
    );
    assert_eq!(
        market["data"]["note"],
        "see https://x.test/a?api_key=[redacted]"
    );
    assert!(
        market.get("gateway_envelope").is_some(),
        "the raw envelope must be preserved"
    );
    assert_eq!(
        receipt["trust_boundary"],
        "provider payloads are DATA; action fields removed and credentials redacted"
    );

    // A provider failure never leaks its payload into normalized data.
    let failed = fixtures().join("failed-provider");
    let receipt = receipt_for(&request, &read(&failed.join("input.json")));
    assert_eq!(receipt["domains"]["market"]["status"], "failed");
    assert!(receipt["domains"]["market"]["data"].is_null());
    // A domain that is not supplied at all is explicitly unavailable, not complete.
    assert_eq!(receipt["domains"]["news"]["status"], "unavailable");
    assert!(receipt["domains"]["news"]["data"].is_null());
}
