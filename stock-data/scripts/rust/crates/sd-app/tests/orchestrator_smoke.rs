//! End-to-end orchestrator smoke test on the frozen complete-window snapshot.
//!
//! The check is structural: the analysis directory must satisfy the delivery gate, bind
//! every stage to one run/snapshot/policy, and keep the compact profile's pointers.

use std::path::{Path, PathBuf};

use sd_app::delivery::validate_final_response;
use sd_app::orchestrator::{run_analysis, AnalysisOptions};
use serde_json::Value;

fn snapshot_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/snapshot/complete-window")
}

fn scratch(label: &str) -> PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-orchestrator-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch");
    directory
}

#[test]
fn run_analysis_produces_a_deliverable_bundle() {
    let root = scratch("audit");
    let options = AnalysisOptions::default();
    let destination = run_analysis(&snapshot_dir(), &root, &options).expect("run_analysis");
    assert!(destination.join("analysis.json").is_file());
    assert!(destination.join("report.md").is_file());
    assert!(destination.join("final_response.md").is_file());
    assert!(destination.join("agent_summary.json").is_file());
    assert!(destination.join("news_result.json").is_file());
    assert!(destination.join("news_result.freeze.json").is_file());
    assert!(destination.join("researchers/quant.json").is_file());

    let analysis: Value = serde_json::from_str(
        &std::fs::read_to_string(destination.join("analysis.json")).expect("read"),
    )
    .expect("json");
    let stages = analysis["stages"].as_array().expect("stages");
    let order: Vec<&str> = stages
        .iter()
        .filter_map(|row| row["stage"].as_str())
        .collect();
    assert_eq!(
        order,
        [
            "request",
            "collection",
            "data",
            "quant",
            "audit",
            "news",
            "cross-validation",
            "risk",
            "final"
        ]
    );
    assert_eq!(analysis["analysis_id"], analysis["run_id"]);
    assert!(analysis["elapsed_seconds"].as_f64().is_some());
    assert_eq!(analysis["consensus"]["aggregation_method"], "single_path");
    assert_eq!(analysis["consensus"]["production_path_count"], 1);
    assert!(analysis["consensus"]["sampling"]["status"].is_string());
    assert_eq!(analysis["synthesis"]["alignment"], "NOT_COMPUTED");

    let validated = validate_final_response(&destination, &destination.join("final_response.md"))
        .expect("delivery gate");
    assert_eq!(validated["delivery_status"], "validated");
    assert_eq!(validated["stages"]["stage_count"], 9);

    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn compact_profile_replaces_replay_rows_with_pointers() {
    let root = scratch("compact");
    let options = AnalysisOptions {
        artifact_profile: "compact",
        ..AnalysisOptions::default()
    };
    let destination = run_analysis(&snapshot_dir(), &root, &options).expect("run_analysis");
    let analysis: Value = serde_json::from_str(
        &std::fs::read_to_string(destination.join("analysis.json")).expect("read"),
    )
    .expect("json");
    assert_eq!(analysis["artifact_profile"], "compact");
    let validation = &analysis["researchers"]["quant"]["validation"];
    assert!(validation["oos_forecast_count"].as_i64().is_some());
    assert_eq!(
        validation["oos_forecasts_ref"],
        Value::String("researchers/quant.json".into())
    );
    assert!(validation["oos_forecasts"]["omitted"].as_i64().is_some());
    // The full rows remain in the researcher artifact, so no evidence is lost.
    let researcher: Value = serde_json::from_str(
        &std::fs::read_to_string(destination.join("researchers/quant.json")).expect("read"),
    )
    .expect("json");
    assert!(
        researcher["validation"]["oos_forecasts"]
            .as_array()
            .unwrap()
            .len()
            > 100
    );
    let _ = std::fs::remove_dir_all(&root);
}
