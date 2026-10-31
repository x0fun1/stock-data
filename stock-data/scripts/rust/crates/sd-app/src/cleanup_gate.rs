//! `sd-app` wiring for the storage cleanup gate (`cleanup::CleanupGate`).
//!
//! `sd-storage` cannot depend on `sd-app`, so cleanup accepts the report/delivery surface
//! through this trait. Until R11 this implementation is the only thing that lets the
//! destructive cleanup branches re-render and re-validate a prepared response instead of
//! failing closed.

use std::path::Path;

use serde_json::Value;

use sd_storage::cleanup::CleanupGate;

use crate::delivery::validate_final_response;
use crate::delivery::validate_response_text;
use crate::report::{render_final_response, render_markdown};

/// Report/delivery surface used by the cleanup use cases.
#[derive(Debug, Clone, Copy, Default)]
pub struct AppCleanupGate;

impl CleanupGate for AppCleanupGate {
    fn validate_final_response(
        &self,
        analysis_dir: &Path,
        response_file: &Path,
    ) -> Result<Value, String> {
        validate_final_response(analysis_dir, response_file)
    }

    fn render_markdown(&self, report: &Value) -> Result<String, String> {
        Ok(render_markdown(report))
    }

    fn render_final_response(
        &self,
        report: &Value,
        _summary: Option<&Value>,
    ) -> Result<String, String> {
        Ok(render_final_response(report))
    }

    fn validate_response_text(&self, report: &Value, text: &str) -> Result<(), String> {
        validate_response_text(report, text)
    }
}
