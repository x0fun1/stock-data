//! The sd-app cleanup gate delegates to the real report/delivery surface.

use std::path::Path;

use sd_app::cleanup_gate::AppCleanupGate;
use sd_app::report::{render_final_response, render_markdown};
use sd_storage::cleanup::CleanupGate;
use serde_json::Value;

fn fixture(path: &str) -> Value {
    let full = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/delivery")
        .join(path);
    serde_json::from_str(
        &std::fs::read_to_string(&full).unwrap_or_else(|error| panic!("{full:?}: {error}")),
    )
    .expect("json")
}

#[test]
fn gate_delegates_to_the_delivery_contract() {
    let directory =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/delivery/valid");
    let gate = AppCleanupGate;
    let validated = gate
        .validate_final_response(&directory, &directory.join("response.md"))
        .expect("the frozen fixture bundle must validate");
    assert_eq!(validated["delivery_status"], "validated");

    let analysis = fixture("valid/analysis.json");
    let markdown = gate.render_markdown(&analysis).expect("markdown");
    assert_eq!(markdown, render_markdown(&analysis));
    let response = gate
        .render_final_response(&analysis, None)
        .expect("final response");
    assert_eq!(response, render_final_response(&analysis));
    gate.validate_response_text(&analysis, &response)
        .expect("the rendered response satisfies the contract");
    assert!(gate
        .validate_response_text(&analysis, "上涨概率：99.9%")
        .is_err());
}
