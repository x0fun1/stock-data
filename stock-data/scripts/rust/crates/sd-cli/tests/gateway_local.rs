//! R12 differential: the Rust gateway must reproduce the legacy CLI envelopes.
//!
//! The fixture `gateway-local/local/cases.json` is produced by running the unmodified
//! `global_stock_data.py` with the same argv. `fetched_at_utc` is the only clock seam and
//! is excluded; the payload tree, exit code and channel are compared exactly (floats with
//! a 1e-12 relative tolerance).

use std::process::{Command, Output};

use serde_json::Value;

fn fixtures() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/gateway-local")
}

fn scratch(label: &str) -> std::path::PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-gateway-r12-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch");
    directory
}

fn run(args: &[String], cwd: &std::path::Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_stock-data"))
        .args(args)
        .current_dir(cwd)
        .output()
        .expect("run stock-data")
}

fn close(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => match (left.as_f64(), right.as_f64()) {
            (Some(left), Some(right)) => {
                (left - right).abs() <= 1e-12 * left.abs().max(right.abs()).max(1.0)
            }
            _ => left == right,
        },
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(l, r)| close(l, r))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .map(|other| close(value, other))
                        .unwrap_or(false)
                })
        }
        _ => left == right,
    }
}

fn normalize(mut envelope: Value, output_placeholder: bool) -> Value {
    if let Some(object) = envelope.as_object_mut() {
        object.remove("fetched_at_utc");
        if output_placeholder && object.contains_key("envelope_file") {
            object.insert("envelope_file".into(), Value::String("<OUTPUT>".into()));
        }
    }
    envelope
}

#[test]
fn legacy_local_envelopes_match() {
    let cases: Value = serde_json::from_str(
        &std::fs::read_to_string(fixtures().join("local/cases.json")).expect("cases"),
    )
    .expect("json");
    let cases = cases["cases"].as_array().expect("cases array");
    let input = fixtures().join("local/inputs/params.json");
    let mut checked = 0usize;
    for (index, case) in cases.iter().enumerate() {
        let output_placeholder = case["argv"]
            .as_array()
            .map(|items| items.iter().any(|item| item.as_str() == Some("<OUTPUT>")))
            .unwrap_or(false);
        let work = scratch(&format!("case{index}"));
        let output_path = work.join("envelope.json");
        let mut args: Vec<String> = vec!["gateway".to_string()];
        let legacy_args: Vec<String> = case["argv"]
            .as_array()
            .expect("argv")
            .iter()
            .map(|item| match item.as_str().unwrap_or_default() {
                "<INPUT>" => input.to_string_lossy().to_string(),
                "<OUTPUT>" => output_path.to_string_lossy().to_string(),
                other => other.to_string(),
            })
            .collect();
        args.extend(legacy_args);
        let completed = run(&args, &work);
        assert_eq!(
            completed.status.code(),
            case["exit"].as_i64().map(|code| code as i32),
            "case {index} exit: {}",
            String::from_utf8_lossy(&completed.stderr)
        );
        let stdout = String::from_utf8_lossy(&completed.stdout).to_string();
        assert!(
            completed.stderr.is_empty(),
            "case {index} must not write stderr: {}",
            String::from_utf8_lossy(&completed.stderr)
        );
        let actual: Value = serde_json::from_str(stdout.trim()).expect("stdout json");
        let expected = normalize(case["envelope"].clone(), output_placeholder);
        let actual = normalize(actual, output_placeholder);
        let is_list = case["argv"]
            .as_array()
            .map(|items| items.len() == 1 && items[0].as_str() == Some("--list"))
            .unwrap_or(false);
        if is_list {
            // `YFINANCE_FUNCTIONS` is a set in the legacy module, so the tail order of
            // `--list` is nondeterministic across processes (hash randomization). The
            // deterministic prefix and the entries themselves are compared exactly.
            assert_eq!(actual["status"], expected["status"]);
            assert_eq!(actual["source"], expected["source"]);
            assert_eq!(actual["function"], expected["function"]);
            let actual_rows = actual["data"].as_array().cloned().unwrap_or_default();
            let expected_rows = expected["data"].as_array().cloned().unwrap_or_default();
            let mut actual_sorted = actual_rows.clone();
            let mut expected_sorted = expected_rows.clone();
            actual_sorted.sort_by_key(|row| row["function"].as_str().unwrap_or("").to_string());
            expected_sorted.sort_by_key(|row| row["function"].as_str().unwrap_or("").to_string());
            assert_eq!(actual_sorted.len(), 56);
            assert!(
                close(&Value::Array(actual_sorted), &Value::Array(expected_sorted)),
                "case {index} --list entries differ"
            );
            // The fixture sorts the set-derived tail; the runtime keeps the legacy
            // deterministic prefix, so only membership is asserted.
            assert!(
                actual_rows
                    .iter()
                    .all(|row| expected_rows.iter().any(|other| other == row)),
                "case {index} --list entry set differs"
            );
        } else {
            assert!(
                close(&actual, &expected),
                "case {index} {:?}\nexpected: {}\nactual: {}",
                case["argv"],
                expected,
                actual
            );
        }
        if output_placeholder {
            let full: Value = serde_json::from_str(
                &std::fs::read_to_string(&output_path).expect("envelope file"),
            )
            .expect("envelope json");
            let expected_full = case["envelope_file_value"].clone();
            let full = normalize(full, false);
            let expected_full = normalize(expected_full, false);
            assert!(
                close(&full, &expected_full),
                "case {index} full envelope file mismatch"
            );
        }
        let _ = std::fs::remove_dir_all(&work);
        checked += 1;
    }
    assert_eq!(checked, cases.len());
    assert!(checked >= 40, "expected the full R12 differential corpus");
}

#[test]
fn gateway_is_offline_and_fail_closed_for_unported_functions() {
    let work = scratch("offline");
    let output = run(
        &[
            "gateway".to_string(),
            "--function".to_string(),
            "yahoo_history".to_string(),
            "--params-json".to_string(),
            "{\"symbol\":\"AAPL\"}".to_string(),
        ],
        &work,
    );
    assert_eq!(output.status.code(), Some(1));
    let envelope: Value =
        serde_json::from_str(String::from_utf8_lossy(&output.stdout).trim()).expect("json");
    assert_eq!(envelope["status"], "error");
    assert_eq!(envelope["error_type"], "InvalidParameters");
    assert!(
        envelope["error_message"]
            .as_str()
            .unwrap_or_default()
            .contains("cache_dir"),
        "unexpected message: {envelope}"
    );
    let _ = std::fs::remove_dir_all(&work);
}
