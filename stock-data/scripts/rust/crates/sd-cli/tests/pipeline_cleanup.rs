//! Regression for the actual pipeline -> delivery -> owned cleanup CLI handoff.
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{json, Value};

fn execute(skill: &Path, cwd: &Path, arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_stock-data"))
        .args(arguments)
        .env("STOCK_DATA_SKILL_ROOT", skill)
        .current_dir(cwd)
        .output()
        .expect("run CLI")
}

fn success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("CLI JSON")
}

#[test]
fn owned_pipeline_registers_analysis_and_preserves_failed_run_state() {
    let workspace = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let skill = workspace.join("../..");
    let fixture: Value = serde_json::from_slice(
        &std::fs::read(workspace.join("tests/fixtures/freeze/supplied-calendar/input.json"))
            .expect("collection fixture"),
    )
    .expect("fixture JSON");
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("sd-owned-cli-{}-{nonce}", std::process::id()));
    std::fs::create_dir(&root).expect("exclusive scratch directory");
    std::fs::write(
        root.join("request.json"),
        serde_json::to_vec(&fixture["request"]).unwrap(),
    )
    .unwrap();
    let responses = json!({"domains": fixture["domains"]});
    std::fs::write(
        root.join("responses.json"),
        serde_json::to_vec(&responses).unwrap(),
    )
    .unwrap();
    let result = success(execute(
        &skill,
        &root,
        &[
            "research",
            "pipeline",
            "--request",
            "request.json",
            "--responses",
            "responses.json",
            "--output-root",
            "runtime",
            "--cleanup-json",
            "--no-gc",
        ],
    ));
    let run = PathBuf::from(result["run_dir"].as_str().unwrap());
    let analysis = PathBuf::from(result["report_dir"].as_str().unwrap());
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(run.join("run_manifest.json")).unwrap()).unwrap();
    assert_eq!(manifest["state"], "awaiting_cleanup");
    assert_eq!(
        manifest["analysis_id"].as_str(),
        analysis.file_name().unwrap().to_str()
    );
    assert!(!run.join(".pipeline.lock").exists());
    let reply = analysis.join("final_response.md");
    success(execute(
        &skill,
        &root,
        &[
            "research",
            "validate-response",
            "--analysis-dir",
            analysis.to_str().unwrap(),
            "--response-file",
            reply.to_str().unwrap(),
        ],
    ));
    let snapshot = run
        .join("snapshots")
        .join(manifest["snapshot_id"].as_str().unwrap());
    let unrelated = snapshot.join("unregistered.json");
    std::fs::write(&unrelated, b"{}").unwrap();
    let rejected = execute(
        &skill,
        &root,
        &[
            "research",
            "cleanup-run",
            "--run-dir",
            run.to_str().unwrap(),
            "--analysis-dir",
            analysis.to_str().unwrap(),
            "--response-file",
            reply.to_str().unwrap(),
            "--dry-run",
        ],
    );
    assert!(!rejected.status.success());
    assert!(unrelated.is_file() && snapshot.join("news_result.json").is_file());
    std::fs::remove_file(unrelated).unwrap();
    for dry_run in [true, false, false] {
        let mut args = vec![
            "research",
            "cleanup-run",
            "--run-dir",
            run.to_str().unwrap(),
            "--analysis-dir",
            analysis.to_str().unwrap(),
            "--response-file",
            reply.to_str().unwrap(),
        ];
        if dry_run {
            args.push("--dry-run");
        }
        success(execute(&skill, &root, &args));
    }
    assert!(analysis.join("report.md").is_file() && reply.is_file());
    assert!(!analysis.join("analysis.json").exists());
    assert!(root.join("request.json").is_file() && root.join("responses.json").is_file());

    // A freeze failure after workspace creation must be marked failed, not left running.
    let mut invalid = responses;
    invalid["domains"]["market"]["data"]["bars"] = json!([]);
    std::fs::write(root.join("bad.json"), serde_json::to_vec(&invalid).unwrap()).unwrap();
    let failed = execute(
        &skill,
        &root,
        &[
            "research",
            "pipeline",
            "--request",
            "request.json",
            "--responses",
            "bad.json",
            "--output-root",
            "failed",
            "--cleanup-json",
            "--no-gc",
        ],
    );
    assert!(!failed.status.success());
    let failed_run = std::fs::read_dir(root.join("failed/runs"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let failed_manifest: Value =
        serde_json::from_slice(&std::fs::read(failed_run.join("run_manifest.json")).unwrap())
            .unwrap();
    assert_eq!(failed_manifest["state"], "failed");
    assert!(!failed_run.join(".pipeline.lock").exists());
    std::fs::remove_dir_all(root).unwrap();
}
