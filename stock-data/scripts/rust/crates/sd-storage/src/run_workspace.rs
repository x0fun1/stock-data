//! Run workspace layout (R10).
//!
//! One owned run is a directory under `<output_root>/runs/run_<utc>_<token>` holding
//! `run_manifest.json`, the pipeline ownership marker and every artifact the run
//! generated. The manifest is the ownership receipt cleanup uses: `manifest_version`,
//! `mode=owned_pipeline` and `run_id == directory name` must all match before any
//! deletion or update is allowed.
//!
//! The Python `create_run_workspace` derives the run id from `datetime.now()` and
//! `secrets.token_hex(4)`; both are injected here (`clock`, optional `run_id`) so
//! tests are deterministic. The default token reuses the atomic-write scratch token
//! instead of `secrets` (the workspace id only needs uniqueness, not secrecy).

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use sd_core::clock::Clock;
use sd_core::contracts::python_str;

use crate::atomic_write::atomic_write_text;
use crate::cleanup::{CLEANUP_RECEIPT, PIPELINE_LOCK, RUN_MANIFEST};
use crate::jsonio::{read_json_file, write_json};
use crate::locks;
use crate::path_guard::{is_link, real_directory, safe_output_destination};

fn error_message(error: sd_core::error::SdError) -> String {
    error.message().to_string()
}

/// Create an isolated run directory and its ownership manifest.
///
/// `run_id` overrides the generated identifier for tests and reproducible runs.
pub fn create_run_workspace(
    output_root: &Path,
    clock: &dyn Clock,
    run_id: Option<&str>,
) -> Result<(PathBuf, Value), String> {
    let runs_root = output_root.join("runs");
    std::fs::create_dir_all(&runs_root).map_err(|error| error.to_string())?;
    if is_link(&runs_root) {
        return Err("runtime/runs must be a regular directory, not a link".into());
    }
    let run_id = match run_id {
        Some(value) => value.to_string(),
        None => {
            let stamp = clock.now().format("%Y%m%dT%H%M%S%6fZ").to_string();
            let token = unique_token();
            format!("run_{stamp}_{token}")
        }
    };
    let run_dir = safe_output_destination(&runs_root, &run_id).map_err(error_message)?;
    std::fs::create_dir(&run_dir).map_err(|error| error.to_string())?;
    let mut manifest = Map::new();
    manifest.insert("manifest_version".into(), Value::from(1));
    manifest.insert("mode".into(), Value::String("owned_pipeline".into()));
    manifest.insert("run_id".into(), Value::String(run_id.clone()));
    manifest.insert("state".into(), Value::String("running".into()));
    manifest.insert("created_at_utc".into(), Value::String(clock.utc_now()));
    manifest.insert("collection_name".into(), Value::Null);
    manifest.insert("snapshot_id".into(), Value::Null);
    manifest.insert("analysis_id".into(), Value::Null);
    let manifest = Value::Object(manifest);
    write_json(&run_dir.join(RUN_MANIFEST), &manifest, true).map_err(error_message)?;
    locks::write_pipeline_marker(&run_dir, std::process::id())?;
    Ok((run_dir, manifest))
}

/// Update an owned run manifest after re-validating its identity.
pub fn update_run_workspace(run_dir: &Path, updates: &Map<String, Value>) -> Result<Value, String> {
    let root = real_directory(run_dir, "run directory").map_err(error_message)?;
    let manifest_path = root.join(RUN_MANIFEST);
    if is_link(&manifest_path) || !manifest_path.is_file() {
        return Err("owned run manifest is missing or linked".into());
    }
    let mut manifest = read_json_file(&manifest_path).map_err(error_message)?;
    let directory_name = root
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    let identity_ok = manifest.get("manifest_version").and_then(Value::as_i64) == Some(1)
        && manifest.get("run_id").and_then(Value::as_str) == Some(directory_name.as_str())
        && manifest.get("mode").and_then(Value::as_str) == Some("owned_pipeline");
    if !manifest.is_object() || !identity_ok {
        return Err("owned run manifest identity is invalid".into());
    }
    if let Some(manifest_map) = manifest.as_object_mut() {
        for (key, value) in updates {
            manifest_map.insert(key.clone(), value.clone());
        }
    }
    let manifest = manifest.clone();
    write_json(&manifest_path, &manifest, false).map_err(error_message)?;
    Ok(manifest)
}

/// Keep a failed run's artifacts for debugging but mark explicit ownership.
///
/// A failed manifest update must not mask the original pipeline error; the legacy
/// function swallows both the update and lock-release failures.
pub fn mark_run_failed(run_dir: &Path, error_type: &str) {
    let mut updates = Map::new();
    updates.insert("state".into(), Value::String("failed".into()));
    updates.insert("failure_type".into(), Value::String(error_type.into()));
    if update_run_workspace(run_dir, &updates).is_err() {
        return;
    }
    let _ = release_pipeline_lock(run_dir);
}

/// Release the pipeline ownership marker (cleanup lock remains the OS lock).
pub fn release_pipeline_lock(run_dir: &Path) -> Result<(), String> {
    let root = real_directory(run_dir, "run directory").map_err(error_message)?;
    let marker = root.join(PIPELINE_LOCK);
    if is_link(&marker) {
        return Err("pipeline lock marker was replaced by a link".into());
    }
    match std::fs::remove_file(&marker) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

/// Mark a run as awaiting cleanup after a validated delivery.
pub fn mark_run_awaiting_cleanup(run_dir: &Path) -> Result<Value, String> {
    let mut updates = Map::new();
    updates.insert("state".into(), Value::String("awaiting_cleanup".into()));
    update_run_workspace(run_dir, &updates)
}

/// Read the cleanup receipt path inside a run directory.
pub fn run_receipt_path(run_dir: &Path) -> PathBuf {
    run_dir.join(CLEANUP_RECEIPT)
}

/// Serialize a manifest state for diagnostics.
pub fn manifest_state(manifest: &Value) -> String {
    manifest
        .get("state")
        .map(python_str)
        .unwrap_or_else(|| "unknown".into())
}

/// Write one text artifact atomically inside a run directory (manifest markers etc).
pub fn write_manifest_text(path: &Path, text: &str) -> Result<(), String> {
    atomic_write_text(path, text).map_err(error_message)
}

fn unique_token() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let mixed = nanos ^ ((std::process::id() as u128) << 64);
    crate::legacy_json::sha256_hex(&mixed.to_le_bytes())[..8].to_string()
}
