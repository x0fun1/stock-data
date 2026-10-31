//! CLI contract tests: argument surface, output channels and exit codes.
//!
//! The legacy runtime has two incompatible error channels and a host may branch on
//! them, so both are asserted here against the R00 baseline:
//!
//! * gateway: JSON envelope on **stdout**, exit **1**, stderr empty;
//! * research: JSON object on **stderr**, exit **2**, stdout empty.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_stock-data")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn repo_root() -> PathBuf {
    workspace_root().join("../../..")
}

fn run(arguments: &[&str]) -> Output {
    Command::new(binary())
        .args(arguments)
        .current_dir(repo_root())
        .output()
        .unwrap_or_else(|error| panic!("cannot run the stock-data binary: {error}"))
}

fn stdout_text(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr_text(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn stdout_json(output: &Output) -> Value {
    serde_json::from_str(stdout_text(output).trim())
        .unwrap_or_else(|error| panic!("stdout is not JSON ({error}): {}", stdout_text(output)))
}

#[test]
fn gateway_list_returns_all_fifty_six_functions() {
    let output = run(&["gateway", "--list"]);
    assert_eq!(
        output.status.code(),
        Some(0),
        "stderr: {}",
        stderr_text(&output)
    );
    let envelope = stdout_json(&output);
    assert_eq!(envelope["status"], "success");
    assert_eq!(envelope["source"], "local");
    let data = envelope["data"].as_array().expect("data is an array");
    assert_eq!(
        data.len(),
        56,
        "the registry must expose exactly 56 functions"
    );

    // Membership, source and parameters are compared as a set (the legacy emitter
    // writes a dict, so enumeration order is not a contract).
    let fixture: Value = serde_json::from_str(
        &std::fs::read_to_string(workspace_root().join("tests/fixtures/gateway/registry.json"))
            .expect("fixture"),
    )
    .expect("fixture json");
    let mut expected: Vec<String> = fixture
        .as_array()
        .expect("registry fixture is an array")
        .iter()
        .map(|entry| entry["function"].as_str().unwrap().to_string())
        .collect();
    expected.sort();
    let mut actual: Vec<String> = data
        .iter()
        .map(|entry| entry["function"].as_str().unwrap().to_string())
        .collect();
    actual.sort();
    assert_eq!(actual, expected);

    // Every entry carries the legacy shape.
    for entry in data {
        assert!(entry.get("source").is_some(), "missing source: {entry}");
        assert!(
            entry["parameters"].is_array(),
            "parameters must be a list: {entry}"
        );
    }
}

#[test]
fn gateway_errors_use_stdout_and_exit_one() {
    let missing = run(&["gateway"]);
    assert_eq!(missing.status.code(), Some(1));
    assert!(
        stderr_text(&missing).is_empty(),
        "gateway errors must not use stderr"
    );
    let envelope = stdout_json(&missing);
    assert_eq!(envelope["status"], "error");
    assert_eq!(envelope["error_type"], "ValueError");
    assert_eq!(
        envelope["error_message"],
        "one of the arguments --list --function is required"
    );
    assert!(envelope["function"].is_null());

    let unknown = run(&["gateway", "--function", "__no_such_function__"]);
    assert_eq!(unknown.status.code(), Some(1));
    let envelope = stdout_json(&unknown);
    assert_eq!(envelope["error_type"], "ValueError");
    assert_eq!(
        envelope["error_message"],
        "Function is not in the allowed list; use --list."
    );
    assert_eq!(envelope["function"], "__no_such_function__");
}

#[test]
fn gateway_function_fails_closed_when_configuration_is_missing() {
    // Parameter validation runs before execution in the legacy CLI, so the function is
    // called with a valid argument and must still fail closed when its required Yahoo
    // cache configuration is absent (never a fabricated success).
    let output = run(&[
        "gateway",
        "--function",
        "yahoo_history",
        "--params-json",
        "{\"symbol\":\"AAPL\"}",
    ]);
    assert_eq!(
        output.status.code(),
        Some(1),
        "a misconfigured function must fail"
    );
    let envelope = stdout_json(&output);
    assert_eq!(envelope["status"], "error");
    assert_eq!(envelope["error_type"], "InvalidParameters");
    assert_ne!(envelope["status"], "success");
}

#[test]
fn research_errors_use_stderr_and_exit_two() {
    let output = run(&["research", "pipeline"]);
    assert_eq!(
        output.status.code(),
        Some(2),
        "argparse-compatible parameter failure exits 2"
    );
    assert!(
        stdout_text(&output).is_empty(),
        "research errors must not use stdout"
    );

    // `analyze` is implemented: a missing snapshot is reported through the same
    // stderr channel and exit code, with the legacy ValueError type.
    let missing = run(&["research", "analyze", "--snapshot-dir", "does-not-exist"]);
    assert_eq!(missing.status.code(), Some(2));
    assert!(stdout_text(&missing).is_empty());
    let payload: Value = serde_json::from_str(stderr_text(&missing).trim()).expect("stderr json");
    assert_eq!(payload["status"], "error");
    assert_eq!(payload["error_type"], "ValueError");
}

#[test]
fn schema_publishes_all_six_kinds() {
    for kind in [
        "request",
        "collection",
        "gateway-responses",
        "researcher",
        "final",
        "news",
    ] {
        let output = run(&["research", "schema", "--kind", kind]);
        assert_eq!(
            output.status.code(),
            Some(0),
            "schema {kind} failed: {}",
            stderr_text(&output)
        );
        let document: Value = serde_json::from_str(stdout_text(&output).trim())
            .unwrap_or_else(|error| panic!("schema {kind} is not JSON: {error}"));
        assert!(document.is_object(), "schema {kind} must be an object");

        // The captured legacy document is the contract.
        let captured = repo_root().join(format!(
            "runtime/refactor/baseline/commands/0{}-schema-{kind}/stdout.txt",
            schema_index(kind)
        ));
        if captured.exists() {
            let expected: Value =
                serde_json::from_str(&std::fs::read_to_string(&captured).expect("baseline"))
                    .expect("");
            assert_eq!(
                document, expected,
                "schema {kind} differs from the legacy contract"
            );
        }
    }

    let unknown = run(&["research", "schema", "--kind", "bogus"]);
    assert_eq!(unknown.status.code(), Some(2));
}

fn schema_index(kind: &str) -> u8 {
    match kind {
        "request" => 20,
        "collection" => 21,
        "gateway-responses" => 22,
        "researcher" => 23,
        "final" => 24,
        "news" => 25,
        other => panic!("unexpected kind {other}"),
    }
}

#[test]
fn every_research_command_publishes_help() {
    for command in [
        "request",
        "adapt",
        "freeze",
        "news-candidates",
        "analyze",
        "pipeline",
        "validate-response",
        "ledger-record",
        "ledger-settle",
        "ledger-audit",
        "cleanup-run",
        "gc",
        "schema",
    ] {
        let output = run(&["research", command, "--help"]);
        assert_eq!(output.status.code(), Some(0), "help for {command} failed");
        assert!(
            !stdout_text(&output).is_empty(),
            "help for {command} is empty"
        );
    }
}

#[test]
fn pipeline_keeps_the_documented_flag_surface() {
    let help = stdout_text(&run(&["research", "pipeline", "--help"]));
    for flag in [
        "--request",
        "--responses",
        "--output-root",
        "--captured-at-utc",
        "--news-policy",
        "--news-ai-assessment",
        "--ledger",
        "--artifact-profile",
        "--cleanup-json",
        "--keep-json",
        "--no-gc",
        "--retain-days",
    ] {
        assert!(
            help.contains(flag),
            "pipeline help is missing {flag}:\n{help}"
        );
    }
}

#[test]
fn experiment_and_collector_help_are_available() {
    for arguments in [
        vec!["experiments", "momentum-ab", "--help"],
        vec!["experiments", "selection-ab", "--help"],
        vec!["experiments", "research-b-ab", "--help"],
        vec!["experiments", "research-candidates", "--help"],
        vec!["collect", "yahoo", "--help"],
        vec!["selfcheck", "--help"],
    ] {
        let output = run(&arguments);
        assert_eq!(
            output.status.code(),
            Some(0),
            "help failed for {arguments:?}"
        );
    }
}

#[test]
fn selfcheck_list_is_honest_about_pending_checks() {
    let output = run(&["selfcheck", "--offline", "--list"]);
    assert_eq!(output.status.code(), Some(0));
    let document = stdout_json(&output);
    assert_eq!(document["active_checks"], 68);
    assert_eq!(document["retired_checks"], 24);
    let checks = document["checks"].as_array().expect("checks is an array");
    assert_eq!(checks.len(), 68);
    let implemented: Vec<&str> = checks
        .iter()
        .filter(|check| check["status"] == "implemented")
        .map(|check| check["id"].as_str().unwrap())
        .collect();
    // Only checks with a real assertion body may claim implementation. R02 migrated F03.
    // Only checks with a real assertion body may claim implementation: R02 migrated
    // F03 and R03 migrated the twelve offline market-data/calendar/snapshot checks.
    let mut implemented: Vec<&str> = implemented;
    implemented.sort_unstable();
    let mut expected = vec![
        "F03", "F07", "F22", "F30", "F33", "T01", "T02", "T03", "T04", "T05", "T06", "T36", "T40",
        // R05: audit, probability quality, single-path consensus (T33 is an R03 check
        // that was unblocked by the consensus module).
        "T09", "T10", "T12", "T13", "T14", "T17", "T18", "T19", "T24", "T29", "T30", "T38", "T33",
        // R04: label maturity, reproducibility, window trimming and the v2 formula.
        "T08", "T11", "T16", "F46",
        // R05 (F01 was migrated with the ML runner) plus R06: B/C paths, reliability,
        // capacity/timing, prefix invariance and the policy-disabled shadow calibration.
        "F01", "T20", "T21", "F29", "F34", "F02", "F41", "F42",
        // R07: three-state News, deterministic screening, timing/priced-in, host AI lean
        // validation, coverage reconciliation and presentation-warning severity.
        "T25", "T26", "T27", "T28", "T35", "T41", "T42", "F17",
        // R08: delivery/stage contract, non-stale reporting status, headline newline
        // injection. T45 (compact profile) stays pending until the R11 orchestrator.
        "T23", "T31", "T32",
        // R09: append-only ledger, prospective recording, prompt/version isolation.
        "T15", "T22", "F44", "T39", "F39", "F40",
        // R10: orphan scratch sweep and retention/GC.
        "F18", "F19",
        // R11: orchestrator end-to-end paths and registered experiment statistics.
        "T07", "T37", "F27", "F28", "T34", "T45",
        // R15: Yahoo provider preflight/counting, options Unix expiries, cache-dir normalization.
        "F08", "F11", "F12", // R17: registry completeness and documented versions.
        "T43", "T44",
    ];
    expected.sort_unstable();
    assert_eq!(
        implemented, expected,
        "only the migrated checks may claim implementation"
    );
    assert_eq!(document["implemented"], implemented.len());
    assert_eq!(
        document["not_implemented"].as_u64().unwrap() as usize,
        68 - implemented.len()
    );
    let retired = document["retired"].as_array().expect("retired is an array");
    assert_eq!(retired.len(), 24);
    assert!(retired
        .iter()
        .all(|entry| !entry["reason"].as_str().unwrap().is_empty()));
}

#[test]
fn selfcheck_full_run_passes_when_all_checks_are_migrated() {
    let output = run(&["selfcheck", "--offline"]);
    assert_eq!(
        output.status.code(),
        Some(0),
        "all migrated checks must pass"
    );
    let document = stdout_json(&output);
    assert_eq!(document["status"], "success");
    assert_eq!(document["executed"], 68, "the migrated checks run for real");
    assert_eq!(
        document["failed"].as_array().unwrap().len(),
        0,
        "the migrated checks must pass"
    );
    assert_eq!(document["not_implemented"].as_array().unwrap().len(), 0);
}

#[test]
fn migrated_check_can_be_selected_and_passes_on_its_own() {
    let output = run(&["selfcheck", "--offline", "--check", "F03"]);
    assert_eq!(
        output.status.code(),
        Some(0),
        "stderr: {}",
        stderr_text(&output)
    );
    let document = stdout_json(&output);
    assert_eq!(document["status"], "success");
    assert_eq!(document["executed"], 1);
    assert_eq!(document["not_implemented"].as_array().unwrap().len(), 0);
}

#[test]
fn selfcheck_unknown_check_id_is_rejected_before_execution() {
    let output = run(&[
        "selfcheck",
        "--offline",
        "--check",
        "NOT_A_REGISTERED_CHECK",
    ]);
    assert_eq!(output.status.code(), Some(1), "unknown ids must not pass");
    let document = stdout_json(&output);
    assert_eq!(document["status"], "error");
    assert_eq!(document["executed"], 0);
    assert_eq!(document["requested"], 1);
    assert_eq!(
        document["unknown"],
        serde_json::json!(["NOT_A_REGISTERED_CHECK"])
    );
    assert_eq!(document["failed"].as_array().unwrap().len(), 0);
}

#[test]
fn selfcheck_selected_run_reports_requested_and_executed_counts() {
    let output = run(&["selfcheck", "--offline", "--check", "F03"]);
    assert_eq!(output.status.code(), Some(0));
    let document = stdout_json(&output);
    assert_eq!(document["requested"], 1);
    assert_eq!(document["executed"], 1);
    assert_eq!(document["unknown"], serde_json::json!([]));
    assert_eq!(document["failed"].as_array().unwrap().len(), 0);
}
