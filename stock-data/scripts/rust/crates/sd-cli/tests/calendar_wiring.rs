//! S03: freeze consumes the verified Skill calendar pack and records the evidence in the
//! snapshot; a corrupt pack is rejected instead of silently degrading.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_stock-data")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn skill_root() -> PathBuf {
    workspace_root().join("../..")
}

fn fixture(case: &str) -> PathBuf {
    workspace_root()
        .join("tests/fixtures/freeze")
        .join(case)
        .join("input.json")
}

fn scratch(label: &str) -> PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-calendar-wiring-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch");
    directory
}

fn run_freeze(input: &Path, output_root: &Path, extra: &[&str], env: &[(&str, &str)]) -> Output {
    let mut command = Command::new(binary());
    command
        .args([
            "research",
            "freeze",
            "--input",
            input.to_str().expect("input"),
            "--output-root",
            output_root.to_str().expect("root"),
        ])
        .args(extra)
        .current_dir(std::env::temp_dir());
    command.env_remove("STOCK_DATA_SKILL_ROOT");
    command.env_remove("STOCK_DATA_CALENDAR_DIR");
    for (key, value) in env {
        command.env(key, value);
    }
    command.output().expect("run freeze")
}

fn freeze_snapshot_dir(output: &Output) -> PathBuf {
    let text = String::from_utf8_lossy(&output.stdout);
    let document: Value = serde_json::from_str(text.trim())
        .unwrap_or_else(|error| panic!("stdout is not JSON ({error}): {text}"));
    PathBuf::from(document["snapshot_dir"].as_str().expect("snapshot_dir"))
}

#[test]
fn explicit_calendar_pack_lands_in_the_frozen_snapshot() {
    let root = scratch("explicit");
    let pack = skill_root().join("assets/calendars");
    let output = run_freeze(
        &fixture("missing-calendar"),
        &root,
        &["--calendar-dir", pack.to_str().expect("pack")],
        &[],
    );
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let snapshot = freeze_snapshot_dir(&output);
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(snapshot.join("manifest.json")).expect("manifest"),
    )
    .expect("manifest json");
    // Python contract (`snapshot._derived_metadata`): the optional library schedule is
    // persisted as data_validation.resolved_calendar_evidence with
    // calendar_verification=library_verified; the producer's `sources.market.session_calendar`
    // stays absent because the collection itself supplied no calendar.
    let validation = &manifest["data_validation"];
    assert_eq!(
        validation["calendar_verification"], "library_verified",
        "library calendar was not used: {validation}"
    );
    let evidence = &validation["resolved_calendar_evidence"];
    assert!(
        evidence["source"]
            .as_str()
            .unwrap_or_default()
            .starts_with("exchange_calendars"),
        "calendar evidence missing from the snapshot: {evidence}"
    );
    assert!(
        evidence["sessions"]
            .as_array()
            .map(|sessions| sessions.len())
            .unwrap_or(0)
            > 100,
        "unexpected session count: {evidence}"
    );
    assert!(
        manifest["sources"]["market"]["session_calendar"].is_null(),
        "the supplied collection had no calendar; provenance must say so"
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn mounted_skill_root_resolves_the_pack_without_a_flag() {
    let root = scratch("auto");
    let output = run_freeze(
        &fixture("missing-calendar"),
        &root,
        &[],
        &[(
            "STOCK_DATA_SKILL_ROOT",
            skill_root().to_str().expect("skill"),
        )],
    );
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let snapshot = freeze_snapshot_dir(&output);
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(snapshot.join("manifest.json")).expect("manifest"),
    )
    .expect("json");
    let validation = &manifest["data_validation"];
    assert_eq!(validation["calendar_verification"], "library_verified");
    assert!(validation["resolved_calendar_evidence"]["source"]
        .as_str()
        .unwrap_or_default()
        .starts_with("exchange_calendars"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn tampered_calendar_pack_is_rejected_with_a_research_error() {
    let root = scratch("tampered");
    let pack = root.join("pack");
    std::fs::create_dir_all(&pack).expect("pack");
    let source = skill_root().join("assets/calendars");
    for name in ["manifest.json", "US.sessions.json", "HK.sessions.json"] {
        std::fs::copy(source.join(name), pack.join(name)).expect("copy");
    }
    // Flip a byte in the US resource: the digest must fail loudly.
    let path = pack.join("US.sessions.json");
    let mut bytes = std::fs::read(&path).expect("read");
    let position = bytes.len() / 2;
    bytes[position] = if bytes[position] == b'0' { b'1' } else { b'0' };
    std::fs::write(&path, &bytes).expect("write");
    let output = run_freeze(
        &fixture("missing-calendar"),
        &root.join("out"),
        &["--calendar-dir", pack.to_str().expect("pack")],
        &[],
    );
    assert_eq!(output.status.code(), Some(2), "tampered pack must abort");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("failed its digest check"),
        "unexpected stderr: {stderr}"
    );
    let _ = std::fs::remove_dir_all(&root);
}
