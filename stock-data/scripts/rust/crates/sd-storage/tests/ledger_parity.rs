//! Differential parity for the append-only ledger (`ledger.py`, R09).

use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveDate, Utc};
use sd_core::json_text::canonical_text;
use sd_storage::ledger::{
    append_event, audit, prospective_records, read_events, record_prediction, settle,
    settle_pending, LedgerErrorKind, RecordOptions, BASELINE_RULE, PREDICTION, SCHEMA_VERSION,
    SETTLEMENT,
};
use serde_json::Value;

fn read_fixture() -> Value {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/ledger/ledger.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("fixture")).expect("json")
}

fn golden(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/ledger/golden")
        .join(name);
    let bytes = std::fs::read(path).expect("golden bytes");
    if cfg!(windows) {
        bytes
    } else {
        // The recorded Python ledger used Windows text-mode CRLF. Select the
        // Unix text-mode reference, preserving every canonical event/hash byte.
        String::from_utf8(bytes)
            .expect("UTF-8 ledger reference")
            .replace("\r\n", "\n")
            .into_bytes()
    }
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("sd-ledger-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch dir");
    dir
}

fn parse_time(text: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(text)
        .expect("timestamp")
        .with_timezone(&Utc)
}

fn parse_day(value: &Value) -> NaiveDate {
    NaiveDate::parse_from_str(value.as_str().expect("day"), "%Y-%m-%d").expect("date")
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
            assert_eq!(
                left.len(),
                right.len(),
                "{path}: object size differs ({} vs {})",
                left.len(),
                right.len()
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

fn normalized(value: &Value, ledger_path: &Path) -> Value {
    match value {
        Value::String(text) if Path::new(text.as_str()) == ledger_path => {
            Value::String("<ledger>".into())
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| normalized(item, ledger_path))
                .collect(),
        ),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), normalized(item, ledger_path)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn assert_file_bytes(path: &Path, name: &str) {
    let actual = std::fs::read(path).expect("ledger bytes");
    let expected = golden(name);
    assert_eq!(
        String::from_utf8_lossy(&actual),
        String::from_utf8_lossy(&expected),
        "{name}: ledger bytes differ"
    );
}

fn results_of(value: &Value) -> Vec<Value> {
    value.as_array().expect("array").clone()
}

fn options<'a>() -> RecordOptions<'a> {
    RecordOptions::default()
}

#[test]
fn basic_flow_matches_python() {
    let fixture = read_fixture();
    let directory = scratch("basic");
    let path = directory.join("basic.jsonl");
    let clock = parse_time(fixture["late_clock"].as_str().expect("late clock"));
    let settled_at = parse_time(fixture["fixed_now"].as_str().expect("now"));
    let inputs = &fixture["basic"]["inputs"];
    let first = record_prediction(&inputs["result"], &path, clock, options()).expect("first");
    let second = record_prediction(&inputs["result"], &path, clock, options()).expect("second");
    let same_decision = record_prediction(&inputs["same_decision"], &path, clock, options())
        .expect("same decision");
    assert_json_close(
        &normalized(&first, &path),
        &fixture["basic"]["first"],
        "basic/first",
    );
    assert_json_close(
        &normalized(&second, &path),
        &fixture["basic"]["second"],
        "basic/second",
    );
    assert_json_close(
        &normalized(&same_decision, &path),
        &fixture["basic"]["same_decision"],
        "basic/same_decision",
    );

    let bars = results_of(&fixture["bars"]);
    let events = read_events(&path).expect("events");
    assert_json_close(
        &Value::Array(events.clone()),
        &fixture["basic"]["events"],
        "basic/events",
    );
    let (settlements, skipped) =
        settle_pending(&events, &bars, "SYN", "US", None, None, None, settled_at).expect("settle");
    assert_json_close(
        &Value::Array(settlements.clone()),
        &fixture["basic"]["settlements"],
        "basic/settlements",
    );
    assert_json_close(
        &Value::Array(skipped),
        &fixture["basic"]["skipped"],
        "basic/skipped",
    );
    for settlement in &settlements {
        append_event(&path, settlement).expect("append settlement");
    }
    let (settled_again, skipped_again) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle again");
    assert_json_close(
        &Value::Array(settled_again),
        &fixture["basic"]["settled_again"],
        "basic/settled_again",
    );
    assert_json_close(
        &Value::Array(skipped_again),
        &fixture["basic"]["skipped_again"],
        "basic/skipped_again",
    );
    let today = parse_day(&fixture["fixed_today"]);
    assert_json_close(
        &normalized(&audit(&path, today).expect("audit"), &path),
        &fixture["basic"]["report_after_settle"],
        "basic/report_after_settle",
    );

    let abstained_outcome =
        record_prediction(&inputs["abstained"], &path, clock, options()).expect("abstained");
    assert_json_close(
        &normalized(&abstained_outcome, &path),
        &fixture["basic"]["abstained_outcome"],
        "basic/abstained_outcome",
    );
    let events_after = read_events(&path).expect("events");
    assert_json_close(
        events_after.last().expect("last event"),
        &fixture["basic"]["abstained_event"],
        "basic/abstained_event",
    );
    let (_, abstained_skips) = settle_pending(
        &events_after,
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("abstained skips");
    assert_json_close(
        &Value::Array(abstained_skips),
        &fixture["basic"]["abstained_skips"],
        "basic/abstained_skips",
    );

    let other_outcome =
        record_prediction(&inputs["other"], &path, clock, options()).expect("other");
    assert_json_close(
        &normalized(&other_outcome, &path),
        &fixture["basic"]["other_outcome"],
        "basic/other_outcome",
    );
    let (_, mixed_skips) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("mixed skips");
    assert_json_close(
        &Value::Array(mixed_skips),
        &fixture["basic"]["mixed_skips"],
        "basic/mixed_skips",
    );
    assert_json_close(
        &normalized(&audit(&path, today).expect("audit"), &path),
        &fixture["basic"]["audit"],
        "basic/audit",
    );
    assert_file_bytes(&path, "basic.jsonl");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn metrics_and_provenance_match_python() {
    let fixture = read_fixture();
    let today = parse_day(&fixture["fixed_today"]);
    let clock = parse_time(fixture["late_clock"].as_str().expect("late clock"));
    let settled_at = parse_time(fixture["fixed_now"].as_str().expect("now"));

    let metrics_dir = scratch("metrics");
    let metrics_path = metrics_dir.join("metrics.jsonl");
    let metric_bars = results_of(&fixture["metrics"]["inputs"]["bars"]);
    for payload in results_of(&fixture["metrics"]["inputs"]["results"]) {
        let options = RecordOptions {
            bars: Some(&metric_bars),
            ..RecordOptions::default()
        };
        record_prediction(&payload, &metrics_path, clock, options).expect("record metric row");
    }
    let (settlements, _) = settle_pending(
        &read_events(&metrics_path).expect("events"),
        &metric_bars,
        "SYN",
        "US",
        None,
        None,
        None,
        settled_at,
    )
    .expect("settle metrics");
    assert_json_close(
        &Value::Array(settlements.clone()),
        &fixture["metrics"]["settlements"],
        "metrics/settlements",
    );
    for settlement in &settlements {
        append_event(&metrics_path, settlement).expect("append settlement");
    }
    assert_json_close(
        &normalized(&audit(&metrics_path, today).expect("audit"), &metrics_path),
        &fixture["metrics"]["audit"],
        "metrics/audit",
    );
    assert_file_bytes(&metrics_path, "metrics.jsonl");
    let _ = std::fs::remove_dir_all(&metrics_dir);

    let provenance_dir = scratch("provenance");
    let provenance_path = provenance_dir.join("provenance.jsonl");
    let provenance_input = &fixture["provenance"]["input"];
    let outcome = record_prediction(
        provenance_input,
        &provenance_path,
        clock,
        RecordOptions {
            bars: Some(&metric_bars),
            ..RecordOptions::default()
        },
    )
    .expect("provenance record");
    assert_json_close(
        &normalized(&outcome, &provenance_path),
        &fixture["provenance"]["outcome"],
        "provenance/outcome",
    );
    let events = read_events(&provenance_path).expect("events");
    assert_json_close(
        events.last().expect("event"),
        &fixture["provenance"]["event"],
        "provenance/event",
    );
    assert_file_bytes(&provenance_path, "provenance.jsonl");
    let _ = std::fs::remove_dir_all(&provenance_dir);
}

#[test]
fn forward_eligibility_matches_python() {
    let fixture = read_fixture();
    let directory = scratch("forward");
    let path = directory.join("forward.jsonl");
    let inputs = &fixture["forward"]["inputs"];
    let prompt = record_prediction(
        &inputs["live"],
        &path,
        parse_time("2026-01-02T22:00:00Z"),
        options(),
    )
    .expect("prompt");
    let late = record_prediction(
        &inputs["late"],
        &path,
        parse_time("2026-01-06T09:00:00Z"),
        options(),
    )
    .expect("late");
    let historical = record_prediction(
        &inputs["historical"],
        &path,
        parse_time("2026-01-20T21:00:00Z"),
        options(),
    )
    .expect("historical");
    let backfill_options = RecordOptions {
        import_reason: Some("backfill"),
        ..RecordOptions::default()
    };
    let backfill = record_prediction(
        &inputs["backfill"],
        &path,
        parse_time("2026-01-20T21:00:00Z"),
        backfill_options,
    )
    .expect("backfill");
    for (name, value) in [
        ("prompt", &prompt),
        ("late", &late),
        ("historical", &historical),
        ("backfill", &backfill),
    ] {
        assert_json_close(
            &normalized(value, &path),
            &fixture["forward"][name],
            &format!("forward/{name}"),
        );
    }
    assert_json_close(
        &Value::Array(read_events(&path).expect("events")),
        &fixture["forward"]["events"],
        "forward/events",
    );
    assert_json_close(
        &Value::Array(prospective_records(&path, None, None).expect("prospective")),
        &fixture["forward"]["prospective"],
        "forward/prospective",
    );
    assert_file_bytes(&path, "forward.jsonl");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn version_isolation_matches_python() {
    let fixture = read_fixture();
    let directory = scratch("isolation");
    let path = directory.join("isolation.jsonl");
    let inputs = &fixture["version_isolation"]["inputs"];
    let prompt = record_prediction(
        &inputs["early"],
        &path,
        parse_time("2026-01-02T21:00:00Z"),
        options(),
    )
    .expect("prompt");
    let late = record_prediction(
        &inputs["late"],
        &path,
        parse_time("2026-01-06T09:00:00Z"),
        options(),
    )
    .expect("late");
    assert_json_close(
        &normalized(&prompt, &path),
        &fixture["version_isolation"]["prompt"],
        "isolation/prompt",
    );
    assert_json_close(
        &normalized(&late, &path),
        &fixture["version_isolation"]["late"],
        "isolation/late",
    );
    let bars = results_of(&fixture["bars"]);
    let (settlements, _) = settle_pending(
        &read_events(&path).expect("events"),
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        parse_time(fixture["fixed_now"].as_str().expect("now")),
    )
    .expect("settle");
    assert_json_close(
        &Value::Array(settlements.clone()),
        &fixture["version_isolation"]["settlements"],
        "isolation/settlements",
    );
    for settlement in &settlements {
        append_event(&path, settlement).expect("append settlement");
    }
    append_event(&path, &fixture["version_isolation"]["forged"]).expect("forged row");
    append_event(
        &path,
        &serde_json::json!({
            "schema_version": SCHEMA_VERSION,
            "event": "settlement",
            "prediction_id": "other-model",
            "hit": 1,
            "recorded_at": "2026-01-10T00:00:00Z",
        }),
    )
    .expect("forged settlement");
    assert_json_close(
        &Value::Array(prospective_records(&path, None, None).expect("prospective")),
        &fixture["version_isolation"]["prospective"],
        "isolation/prospective",
    );
    assert_file_bytes(&path, "isolation.jsonl");
    let _ = std::fs::remove_dir_all(&directory);

    // Tampering with a chained row and rewriting the chain link must both be rejected.
    let basic = golden("basic.jsonl");
    let text = String::from_utf8(basic).expect("utf8");
    let mut lines: Vec<Value> = text
        .lines()
        .map(|line| serde_json::from_str(line).expect("row"))
        .collect();
    let tampered_dir = scratch("tampered");
    let tampered_path = tampered_dir.join("tampered.jsonl");
    if let Some(Value::Object(row)) = lines.first_mut() {
        row.insert("prob_up".into(), Value::from(0.99));
    }
    let tampered_text: Vec<String> = lines.iter().map(canonical_text).collect();
    std::fs::write(&tampered_path, format!("{}\n", tampered_text.join("\n"))).expect("write");
    let error = read_events(&tampered_path).expect_err("tampered");
    assert_eq!(error.kind, LedgerErrorKind::Validation);
    assert_eq!(
        error.message,
        fixture["version_isolation"]["tampered_error"]["message"]
            .as_str()
            .expect("message")
    );
    let chain_dir = scratch("chain");
    let chain_path = chain_dir.join("chain.jsonl");
    let mut chain_rows: Vec<Value> = text
        .lines()
        .map(|line| serde_json::from_str(line).expect("row"))
        .collect();
    if let Some(Value::Object(row)) = chain_rows.first_mut() {
        row.insert("prev_hash".into(), Value::String("f".repeat(64)));
    }
    let chain_text: Vec<String> = chain_rows.iter().map(canonical_text).collect();
    std::fs::write(&chain_path, format!("{}\n", chain_text.join("\n"))).expect("write");
    let chain_error = read_events(&chain_path).expect_err("chain");
    assert_eq!(
        chain_error.message,
        fixture["version_isolation"]["chain_error"]["message"]
            .as_str()
            .expect("message")
    );
    let _ = std::fs::remove_dir_all(&tampered_dir);
    let _ = std::fs::remove_dir_all(&chain_dir);
}

#[test]
fn legacy_schemas_and_errors_match_python() {
    let fixture = read_fixture();
    let bars = results_of(&fixture["bars"]);
    let today = parse_day(&fixture["fixed_today"]);
    let directory = scratch("legacy");
    let path = directory.join("legacy.jsonl");
    let rows = results_of(&fixture["legacy"]["rows"]);
    let text: Vec<String> = rows.iter().map(canonical_text).collect();
    std::fs::write(&path, format!("{}\n", text.join("\n"))).expect("write legacy");
    let events = read_events(&path).expect("legacy events");
    assert_json_close(
        &Value::Array(events.clone()),
        &fixture["legacy"]["events"],
        "legacy/events",
    );
    let (settlements, _) = settle_pending(
        &events,
        &bars,
        "SYN",
        "US",
        None,
        None,
        None,
        parse_time(fixture["fixed_now"].as_str().expect("now")),
    )
    .expect("legacy settle");
    assert_json_close(
        &Value::Array(settlements.clone()),
        &fixture["legacy"]["settlements"],
        "legacy/settlements",
    );
    for settlement in &settlements {
        append_event(&path, settlement).expect("append settlement");
    }
    assert_json_close(
        &normalized(&audit(&path, today).expect("audit"), &path),
        &fixture["legacy"]["audit"],
        "legacy/audit",
    );
    assert_json_close(
        &Value::Array(prospective_records(&path, None, None).expect("prospective")),
        &fixture["legacy"]["prospective"],
        "legacy/prospective",
    );

    let cases = &fixture["legacy"]["errors"];
    let unsupported = directory.join("unsupported.jsonl");
    std::fs::write(
        &unsupported,
        format!(
            "{}\n",
            canonical_text(&serde_json::json!({
                "schema_version": "ledger-9.9",
                "event": "prediction",
            }))
        ),
    )
    .expect("write");
    let error = read_events(&unsupported).expect_err("unsupported schema");
    assert_eq!(error.kind, LedgerErrorKind::Validation);
    assert_eq!(error.message, cases["unsupported_schema"]["message"]);

    let unknown = directory.join("unknown.jsonl");
    std::fs::write(
        &unknown,
        format!(
            "{}\n",
            canonical_text(&serde_json::json!({
                "schema_version": "ledger-3.1",
                "event": "mystery",
            }))
        ),
    )
    .expect("write");
    let error = read_events(&unknown).expect_err("unknown event");
    assert_eq!(error.message, cases["unknown_event"]["message"]);

    let non_object = directory.join("non_object.jsonl");
    std::fs::write(&non_object, "[1, 2]\n").expect("write");
    let error = read_events(&non_object).expect_err("non object");
    assert_eq!(error.kind, LedgerErrorKind::Type);
    assert_eq!(error.message, cases["non_object"]["message"]);

    let malformed = directory.join("malformed.jsonl");
    std::fs::write(&malformed, "{not json}\n").expect("write");
    let error = read_events(&malformed).expect_err("malformed");
    assert_eq!(error.kind, LedgerErrorKind::Validation);
    assert!(
        error
            .message
            .starts_with("ledger line 1 is not valid JSON: "),
        "{}",
        error.message
    );

    let empty = directory.join("empty.jsonl");
    std::fs::write(&empty, "\n").expect("write");
    assert_json_close(
        &Value::Array(read_events(&empty).expect("empty")),
        &fixture["legacy"]["empty_events"],
        "legacy/empty",
    );
    assert_json_close(
        &Value::Array(read_events(&directory.join("does-not-exist.jsonl")).expect("missing")),
        &fixture["legacy"]["missing_events"],
        "legacy/missing",
    );
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn snapshot_flow_matches_python() {
    let fixture = read_fixture();
    let snapshot_dir =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/snapshot/complete-window");
    let directory = scratch("snapshot");
    let path = directory.join("snapshot.jsonl");
    let clock = parse_time(fixture["late_clock"].as_str().expect("late clock"));
    let settled_at = parse_time(fixture["fixed_now"].as_str().expect("now"));
    let input = &fixture["snapshot_flow"]["input"];
    let outcome = record_prediction(
        input,
        &path,
        clock,
        RecordOptions {
            snapshot_dir: Some(&snapshot_dir),
            ..RecordOptions::default()
        },
    )
    .expect("record against snapshot");
    assert_json_close(
        &normalized(&outcome, &path),
        &fixture["snapshot_flow"]["record"],
        "snapshot/record",
    );
    let second = record_prediction(
        input,
        &path,
        clock,
        RecordOptions {
            snapshot_dir: Some(&snapshot_dir),
            ..RecordOptions::default()
        },
    )
    .expect("duplicate against snapshot");
    assert_json_close(
        &normalized(&second, &path),
        &fixture["snapshot_flow"]["second_record"],
        "snapshot/second_record",
    );
    assert_eq!(second["status"], "duplicate");
    let settled = settle(&path, &snapshot_dir, None, None, settled_at).expect("settle snapshot");
    assert_json_close(
        &normalized(&settled, &path),
        &fixture["snapshot_flow"]["settle"],
        "snapshot/settle",
    );
    assert_json_close(
        &Value::Array(read_events(&path).expect("events")),
        &fixture["snapshot_flow"]["events"],
        "snapshot/events",
    );
    assert_file_bytes(&path, "snapshot.jsonl");
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn ledger_invariants_hold() {
    let fixture = read_fixture();
    assert_eq!(SCHEMA_VERSION, "ledger-3.1");
    assert_eq!(PREDICTION, "prediction");
    assert_eq!(SETTLEMENT, "settlement");
    assert_eq!(BASELINE_RULE, "causal_expanding_up_rate_matured_labels");
    let first = &fixture["basic"]["events"][0];
    assert_eq!(first["schema_version"], SCHEMA_VERSION);
    assert_eq!(first["event"], PREDICTION);
    // A retried analysis never double-records the forecast, even when the snapshot hash
    // changes but the decision session and group key stay the same.
    assert_eq!(fixture["basic"]["first"]["status"], "recorded");
    assert_eq!(fixture["basic"]["second"]["status"], "duplicate");
    assert_eq!(
        fixture["basic"]["same_decision"]["reason"],
        "same_group_and_decision_session"
    );
    // Legacy schemas never enter forward evidence.
    assert!(fixture["legacy"]["prospective"]
        .as_array()
        .expect("prospective")
        .is_empty());
    // Version isolation: a foreign model row and its settlement never count.
    assert_eq!(
        fixture["version_isolation"]["prospective"]
            .as_array()
            .expect("prospective")
            .len(),
        1
    );
    let forward_rows = fixture["forward"]["prospective"]
        .as_array()
        .expect("prospective");
    assert!(forward_rows
        .iter()
        .all(|row| row["evidence_scope"].is_string()));
    assert!(forward_rows.iter().all(|row| row["status"] == "matured"));
}
