//! Development helper: dump the Rust gate output for one check-market fixture.
//!
//! Usage: `cargo run -p sd-core --example gate_dump -- <case-name>`

use std::path::{Path, PathBuf};

use sd_core::data_checks::{check_market, CheckOptions};
use serde_json::Value;

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn main() {
    let case = std::env::args().nth(1).expect("case name");
    let directory = fixtures().join("check-market").join(&case);
    let input: Value = serde_json::from_str(
        &std::fs::read_to_string(directory.join("input.json")).expect("input"),
    )
    .expect("json");
    let options = CheckOptions {
        captured_at: input["captured_at"].as_str(),
        resolved_calendar_evidence: None,
        library_calendar: None,
        allow_library_calendar: false,
        enforce_policy: input["enforce_policy"].as_bool().unwrap_or(true),
    };
    let gate = check_market(&input["block"], &input["request"], &options).expect("gate");
    println!(
        "{}",
        serde_json::to_string_pretty(&gate).expect("serialize")
    );
}
