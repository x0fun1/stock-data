//! Artifact retention/GC policy (R10).
//!
//! Port of `quant_research/retention.py`. `cleanup` removes the JSON evidence of one
//! run after delivery but leaves the report, the response and the receipt behind, and
//! it refuses a tree that a hard-killed writer left scratch paths in. This module
//! closes both gaps without weakening the owner-scoped deletion rules: orphan scratch
//! paths are swept inside an owned run, a run whose host never reported delivery is
//! finished after a grace period, and a completed run directory is deleted once it is
//! older than the retention window and holds no JSON. Every decision is fail-closed.
//!
//! All wall-clock reads are injected (`now`) so GC reports are reproducible; the
//! generation timestamp is taken from the injected clock as well.

use std::path::{Path, PathBuf};

use chrono::{DateTime, Duration, Timelike, Utc};
use serde_json::{Map, Value};

use sd_core::clock::Clock;
use sd_core::contracts::{parse_timestamp_str, python_repr};

use crate::atomic_write::atomic_write_text;
use crate::cleanup::{
    checked_directory, entry_is_link, member_path, orphan_ignore, read_receipt, sha256_file,
    sweep_orphans, tree_entries, CleanupGate, CleanupOptions, CLEANUP_RECEIPT, PIPELINE_LOCK,
    RUN_MANIFEST,
};
use crate::jsonio::read_json_file;
use crate::locks;
use crate::path_guard::{is_link, validate_path_component};

/// Default retention window for completed run directories.
pub const DEFAULT_RETAIN_DAYS: i64 = 30;
/// Default provider-cache retention.
pub const DEFAULT_CACHE_DAYS: i64 = 7;
/// Default grace period for an abandoned (`awaiting_cleanup`) run.
pub const DEFAULT_PENDING_DAYS: i64 = 1;
/// Default GC throttle.
pub const GC_THROTTLE_HOURS: f64 = 24.0;
/// GC state filename.
pub const GC_STATE: &str = ".gc_state.json";

/// Files that survive a completed JSON cleanup.
pub const RETAINED_FILENAMES: [&str; 6] = [
    RUN_MANIFEST,
    CLEANUP_RECEIPT,
    "report.md",
    "final_response.md",
    PIPELINE_LOCK,
    "run_manifest.json",
];
/// Root directories that may survive with retained-only content.
pub const RETAINED_ROOT_DIRS: [&str; 3] = ["collections", "snapshots", "research"];

/// True when the leaf name matches `run_<YYYYMMDD>T<HHMMSS><ffffff>Z_<8 hex>`.
pub fn is_run_id(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("run_") else {
        return false;
    };
    let Some((stamp, token)) = rest.split_once('_') else {
        return false;
    };
    let bytes = stamp.as_bytes();
    let stamp_ok = bytes.len() == 22
        && bytes[..8].iter().all(u8::is_ascii_digit)
        && bytes[8] == b'T'
        && bytes[9..15].iter().all(u8::is_ascii_digit)
        && bytes[15..21].iter().all(u8::is_ascii_digit)
        && bytes[21] == b'Z';
    let token_ok = token.len() == 8
        && token
            .chars()
            .all(|character| matches!(character, '0'..='9' | 'a'..='f'));
    stamp_ok && token_ok
}

fn parse_utc(value: Option<&Value>) -> Option<DateTime<Utc>> {
    let value = value?;
    let text = value.as_str()?;
    if text.trim().is_empty() {
        return None;
    }
    parse_timestamp_str(text.trim()).ok()
}

use crate::locks::pid_is_running;

fn run_started_at(
    root: &Path,
    manifest: Option<&Value>,
    receipt: Option<&Value>,
) -> Option<DateTime<Utc>> {
    for value in [
        manifest.and_then(|value| value.get("created_at_utc")),
        receipt.and_then(|value| value.get("updated_at_utc")),
    ] {
        if let Some(parsed) = parse_utc(value) {
            return Some(parsed);
        }
    }
    std::fs::metadata(root)
        .ok()
        .and_then(|metadata| metadata.modified().ok())
        .and_then(|modified| {
            let duration = modified.duration_since(std::time::UNIX_EPOCH).ok()?;
            DateTime::<Utc>::from_timestamp(duration.as_secs() as i64, 0)
        })
}

fn read_manifest(root: &Path) -> Result<Option<Value>, String> {
    let path = root.join(RUN_MANIFEST);
    if !path.exists() {
        return Ok(None);
    }
    if is_link(&path) || !path.is_file() {
        return Err("owned run manifest is linked or not a regular file".into());
    }
    let value =
        read_json_file(&path).map_err(|_| "owned run manifest is unreadable".to_string())?;
    let identity_ok = value.get("manifest_version").and_then(Value::as_i64) == Some(1)
        && value.get("mode").and_then(Value::as_str) == Some("owned_pipeline")
        && value.get("run_id").and_then(Value::as_str)
            == Some(
                root.file_name()
                    .map(|name| name.to_string_lossy().to_string())
                    .unwrap_or_default()
                    .as_str(),
            );
    if !value.is_object() || !identity_ok {
        return Err("owned run manifest identity is invalid".into());
    }
    Ok(Some(value))
}

fn prunable_tree(root: &Path) -> Result<(Vec<String>, Vec<String>), String> {
    let (files, directories) = tree_entries(root, &[])?;
    for name in &files {
        let leaf = name.rsplit('/').next().unwrap_or(name);
        if !RETAINED_FILENAMES.contains(&leaf) {
            return Err(format!(
                "run directory still holds a non-retained file: {name}"
            ));
        }
    }
    for name in &directories {
        let parts: Vec<&str> = name.split('/').collect();
        if parts.len() == 1 && RETAINED_ROOT_DIRS.contains(&parts[0]) {
            continue;
        }
        if parts.len() == 2 && parts[0] == "research" {
            validate_path_component(parts[1], "analysis_id")
                .map_err(|error| error.message().to_string())?;
            continue;
        }
        return Err(format!(
            "run directory holds an unexpected directory: {name}"
        ));
    }
    Ok((
        files.into_iter().collect(),
        directories.into_iter().collect(),
    ))
}

fn delete_run_tree(root: &Path, files: &[String], directories: &[String]) -> Result<i64, String> {
    let mut removed_bytes = 0i64;
    let mut ordered_files: Vec<String> = files.to_vec();
    ordered_files.sort_by_key(|item| (item.matches('/').count(), item.clone()));
    ordered_files.reverse();
    for name in ordered_files {
        let path = member_path(root, &name)?;
        if !path.is_file() || entry_is_link(&path) {
            return Err(format!("retained file changed before expiry: {name}"));
        }
        removed_bytes += std::fs::metadata(&path)
            .map(|metadata| metadata.len() as i64)
            .unwrap_or(0);
        std::fs::remove_file(&path).map_err(|error| error.to_string())?;
    }
    let mut ordered_dirs: Vec<String> = directories.to_vec();
    ordered_dirs.sort_by_key(|item| (item.matches('/').count(), item.clone()));
    ordered_dirs.reverse();
    for name in ordered_dirs {
        let path = member_path(root, &name)?;
        if !path.is_dir() || entry_is_link(&path) {
            return Err(format!("retained directory changed before expiry: {name}"));
        }
        std::fs::remove_dir(&path).map_err(|error| error.to_string())?;
    }
    std::fs::remove_dir(root).map_err(|error| error.to_string())?;
    Ok(removed_bytes)
}

fn drop_lock_file(lock: &Path) {
    if lock.exists() && !is_link(lock) && lock.is_file() {
        let _ = std::fs::remove_file(lock);
    }
}

fn expire_run(root: &Path, dry_run: bool) -> Result<&'static str, String> {
    let lock = root
        .parent()
        .map(|parent| {
            parent.join(format!(
                ".{}.cleanup.lock",
                root.file_name()
                    .map(|name| name.to_string_lossy())
                    .unwrap_or_default()
            ))
        })
        .unwrap_or_else(|| PathBuf::from(".cleanup.lock"));
    let _guard = locks::acquire_cleanup_lock(&lock)?;
    let (files, directories) = prunable_tree(root)?;
    if dry_run {
        return Ok("would_delete");
    }
    delete_run_tree(root, &files, &directories)?;
    drop_lock_file(&lock);
    Ok("deleted")
}

/// One GC decision per run directory, independent of the filesystem walk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunDecision {
    /// The pipeline marker names a live process.
    PipelineActive,
    /// The manifest vanished and no receipt can resume this run.
    NoManifestOrReceipt,
    /// A pending run was cleaned and remains incomplete.
    PendingIncomplete,
    /// Cleanup receipt is missing.
    NoCleanupReceipt,
    /// Cleanup receipt is not `complete`.
    CleanupIncomplete,
    /// Frozen snapshot/collection receipts are reused and outlive the run.
    RetainedSnapshotOrCollection,
    /// Old enough and prunable; would be removed by a non-dry run.
    Expire,
    /// A clean run that is still inside its retention window.
    Keep,
}

/// Decide what GC should do with one run directory.
pub fn decide_run(
    root: &Path,
    now: DateTime<Utc>,
    options: &GcOptions,
    kept_requested: bool,
) -> Result<RunDecision, String> {
    let marker = root.join(PIPELINE_LOCK);
    if marker.exists() {
        let pid = std::fs::read_to_string(&marker)
            .map_err(|_| "unreadable_pipeline_lock".to_string())?
            .trim()
            .strip_prefix("pid=")
            .and_then(|value| value.parse::<i64>().ok())
            .ok_or_else(|| "unreadable_pipeline_lock".to_string())?;
        if pid_is_running(pid)? {
            return Ok(RunDecision::PipelineActive);
        }
    }
    let manifest = read_manifest(root)?;
    let receipt = if root.join(CLEANUP_RECEIPT).is_file() {
        Some(read_receipt(&root.join(CLEANUP_RECEIPT))?)
    } else {
        None
    };
    let receipt_value = receipt.as_ref().map(|receipt| receipt.to_value());
    let started = run_started_at(root, manifest.as_ref(), receipt_value.as_ref());
    let age_days = started
        .map(|started| (now - started).num_seconds() as f64 / 86400.0)
        .unwrap_or(0.0);
    if let Some(manifest) = &manifest {
        if manifest.get("state").and_then(Value::as_str) == Some("awaiting_cleanup")
            && options.pending_days > 0
            && age_days >= options.pending_days as f64
        {
            return Ok(RunDecision::PendingIncomplete);
        }
    }
    let _ = kept_requested;
    if options.retain_days <= 0 || age_days < options.retain_days as f64 {
        return Ok(RunDecision::Keep);
    }
    let Some(receipt) = receipt else {
        return Ok(RunDecision::NoCleanupReceipt);
    };
    if receipt.get("state").and_then(Value::as_str) != Some("complete") {
        return Ok(RunDecision::CleanupIncomplete);
    }
    if root.join("snapshots").is_dir() || root.join("collections").is_dir() {
        return Ok(RunDecision::RetainedSnapshotOrCollection);
    }
    Ok(RunDecision::Expire)
}

/// GC options (mirrors the Python defaults and `--no-gc` throttle).
#[derive(Debug, Clone)]
pub struct GcOptions {
    /// Retention window in days for completed runs.
    pub retain_days: i64,
    /// Provider-cache retention in days.
    pub cache_days: i64,
    /// Grace period for `awaiting_cleanup` runs.
    pub pending_days: i64,
    /// Optional cache directory override.
    pub cache_dir: Option<PathBuf>,
    /// Report only.
    pub dry_run: bool,
    /// Throttle window; `None`/`Some(0)` disables throttling.
    pub throttle_hours: Option<f64>,
}

impl Default for GcOptions {
    fn default() -> Self {
        Self {
            retain_days: DEFAULT_RETAIN_DAYS,
            cache_days: DEFAULT_CACHE_DAYS,
            pending_days: DEFAULT_PENDING_DAYS,
            cache_dir: None,
            dry_run: false,
            throttle_hours: None,
        }
    }
}

fn gc_state_path(output_root: &Path) -> PathBuf {
    output_root.join(GC_STATE)
}

fn read_gc_state(output_root: &Path) -> Value {
    let path = gc_state_path(output_root);
    if !path.is_file() || is_link(&path) {
        return Value::Object(Map::new());
    }
    read_json_file(&path).unwrap_or_else(|_| Value::Object(Map::new()))
}

fn write_gc_state(output_root: &Path, moment: DateTime<Utc>, report: &Value) {
    let payload = serde_json::json!({
        "format": "stock-data-gc-v1",
        "last_run_utc": moment.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "runs_scanned": report.get("runs_scanned").cloned().unwrap_or(Value::from(0)),
        "runs_deleted": report
            .get("runs_deleted")
            .and_then(Value::as_array)
            .map(|items| items.len())
            .unwrap_or(0),
        "orphans_removed": report.get("orphans_removed").cloned().unwrap_or(Value::from(0)),
    });
    let Ok(text) = serde_json::to_string_pretty(&payload) else {
        return;
    };
    let _ = std::fs::create_dir_all(output_root);
    let _ = atomic_write_text(&gc_state_path(output_root), &(text + "\n"));
}

fn file_age_days(path: &Path, moment: DateTime<Utc>) -> Option<f64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    let duration = modified.duration_since(std::time::UNIX_EPOCH).ok()?;
    let stamp = DateTime::<Utc>::from_timestamp(duration.as_secs() as i64, 0)?;
    Some((moment - stamp).num_seconds() as f64 / 86400.0)
}

/// Sweep snapshot build directories abandoned in the shared layout.
pub fn sweep_legacy_snapshots(
    output_root: &Path,
    min_age_hours: f64,
    dry_run: bool,
    now: DateTime<Utc>,
) -> Result<Value, String> {
    let base = output_root.join("snapshots");
    let mut report = Map::new();
    report.insert("status".into(), Value::String("success".into()));
    report.insert("removed".into(), Value::Array(Vec::new()));
    report.insert("orphan_bytes".into(), Value::from(0));
    if !base.exists() {
        report.insert("status".into(), Value::String("absent".into()));
        return Ok(Value::Object(report));
    }
    if is_link(&base) || !base.is_dir() {
        report.insert("status".into(), Value::String("skipped".into()));
        report.insert(
            "reason".into(),
            Value::String("snapshots is not a regular directory".into()),
        );
        return Ok(Value::Object(report));
    }
    let mut removed: Vec<Value> = Vec::new();
    let mut removed_bytes = 0i64;
    let mut entries: Vec<PathBuf> = std::fs::read_dir(&base)
        .map_err(|error| error.to_string())?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .collect();
    entries.sort();
    for entry in entries {
        let name = entry
            .file_name()
            .map(|value| value.to_string_lossy().to_string())
            .unwrap_or_default();
        if !crate::atomic_write::is_orphan_temp_dir(&name) || is_link(&entry) || !entry.is_dir() {
            continue;
        }
        let Some(age_days) = file_age_days(&entry, now) else {
            continue;
        };
        if age_days * 24.0 < min_age_hours {
            continue;
        }
        let (files, directories) = tree_entries(&entry, &[])?;
        if dry_run {
            removed.push(Value::String(name));
            continue;
        }
        let mut size = 0i64;
        let mut ordered_files: Vec<String> = files.into_iter().collect();
        ordered_files.sort_by_key(|item| (item.matches('/').count(), item.clone()));
        ordered_files.reverse();
        for relative in ordered_files {
            let path = member_path(&entry, &relative)?;
            if !path.is_file() || entry_is_link(&path) {
                return Err(format!(
                    "snapshot scratch file changed before removal: {relative}"
                ));
            }
            size += std::fs::metadata(&path)
                .map(|metadata| metadata.len() as i64)
                .unwrap_or(0);
            std::fs::remove_file(&path).map_err(|error| error.to_string())?;
        }
        let mut ordered_dirs: Vec<String> = directories.into_iter().collect();
        ordered_dirs.sort_by_key(|item| (item.matches('/').count(), item.clone()));
        ordered_dirs.reverse();
        for relative in ordered_dirs {
            let path = member_path(&entry, &relative)?;
            std::fs::remove_dir(&path).map_err(|error| error.to_string())?;
        }
        std::fs::remove_dir(&entry).map_err(|error| error.to_string())?;
        removed.push(Value::String(name));
        removed_bytes += size;
    }
    report.insert("removed".into(), Value::Array(removed));
    report.insert("orphan_bytes".into(), Value::from(removed_bytes));
    Ok(Value::Object(report))
}

/// Delete provider cache files older than `days`; links and odd entries are skipped.
pub fn prune_cache(
    cache_dir: &Path,
    days: i64,
    dry_run: bool,
    now: DateTime<Utc>,
) -> Result<Value, String> {
    let mut report = Map::new();
    report.insert("status".into(), Value::String("success".into()));
    report.insert("files_removed".into(), Value::from(0));
    report.insert("bytes_removed".into(), Value::from(0));
    report.insert("errors".into(), Value::Array(Vec::new()));
    if days <= 0 {
        report.insert("status".into(), Value::String("disabled".into()));
        return Ok(Value::Object(report));
    }
    if !cache_dir.exists() {
        report.insert("status".into(), Value::String("absent".into()));
        return Ok(Value::Object(report));
    }
    if is_link(cache_dir) || !cache_dir.is_dir() {
        report.insert("status".into(), Value::String("skipped".into()));
        report.insert(
            "reason".into(),
            Value::String("cache directory is not a regular directory".into()),
        );
        return Ok(Value::Object(report));
    }
    let cutoff = now - Duration::days(days);
    let mut errors: Vec<Value> = Vec::new();
    let mut files_removed = 0i64;
    let mut bytes_removed = 0i64;
    let mut directories: Vec<PathBuf> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![cache_dir.to_path_buf()];
    let mut stale: Vec<PathBuf> = Vec::new();
    while let Some(current) = stack.pop() {
        let entries = match std::fs::read_dir(&current) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        let mut children: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .collect();
        children.sort();
        for path in children {
            let name = path
                .file_name()
                .map(|value| value.to_string_lossy().to_string())
                .unwrap_or_default();
            let metadata = match std::fs::symlink_metadata(&path) {
                Ok(metadata) => metadata,
                Err(_) => continue,
            };
            if metadata.is_dir() {
                if is_link(&path) {
                    errors.push(Value::String(format!(
                        "linked cache directory skipped: {name}"
                    )));
                    continue;
                }
                directories.push(path.clone());
                stack.push(path);
            } else if metadata.is_file() {
                if is_link(&path) {
                    errors.push(Value::String(format!(
                        "non-regular cache entry skipped: {name}"
                    )));
                    continue;
                }
                let stale_entry = metadata
                    .modified()
                    .ok()
                    .and_then(|modified| modified.duration_since(std::time::UNIX_EPOCH).ok())
                    .and_then(|duration| {
                        DateTime::<Utc>::from_timestamp(duration.as_secs() as i64, 0)
                    })
                    .map(|stamp| stamp < cutoff)
                    .unwrap_or(false);
                if stale_entry {
                    stale.push(path);
                }
            } else {
                errors.push(Value::String(format!(
                    "non-regular cache entry skipped: {name}"
                )));
            }
        }
    }
    for path in stale {
        let name = path
            .file_name()
            .map(|value| value.to_string_lossy().to_string())
            .unwrap_or_default();
        let size = std::fs::metadata(&path)
            .map(|metadata| metadata.len() as i64)
            .ok();
        let mut ok = size.is_some();
        if ok && !dry_run {
            ok = std::fs::remove_file(&path).is_ok();
        }
        let Some(size) = size.filter(|_| ok) else {
            errors.push(Value::String(format!("{name}: unreadable or locked")));
            continue;
        };
        files_removed += 1;
        bytes_removed += size;
    }
    if !dry_run {
        let mut ordered = directories;
        ordered.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        for path in ordered {
            let empty = std::fs::read_dir(&path)
                .map(|mut entries| entries.next().is_none())
                .unwrap_or(false);
            if empty {
                let _ = std::fs::remove_dir(&path);
            }
        }
    }
    report.insert("files_removed".into(), Value::from(files_removed));
    report.insert("bytes_removed".into(), Value::from(bytes_removed));
    report.insert("errors".into(), Value::Array(errors));
    Ok(Value::Object(report))
}

/// Sweep orphans, finish abandoned runs and expire run directories past retention.
pub fn gc_runtime(
    output_root: &Path,
    options: &GcOptions,
    clock: &dyn Clock,
    gate: &dyn CleanupGate,
) -> Result<Value, String> {
    let moment = clock.now();
    let mut report = Map::new();
    report.insert("status".into(), Value::String("success".into()));
    report.insert("dry_run".into(), Value::Bool(options.dry_run));
    report.insert("runs_scanned".into(), Value::from(0));
    report.insert("orphans_removed".into(), Value::from(0));
    report.insert("orphans_detected".into(), Value::from(0));
    report.insert("orphan_bytes".into(), Value::from(0));
    report.insert("pending_cleaned".into(), Value::Array(Vec::new()));
    report.insert("runs_deleted".into(), Value::Array(Vec::new()));
    report.insert("runs_would_delete".into(), Value::Array(Vec::new()));
    report.insert("runs_skipped".into(), Value::Array(Vec::new()));
    report.insert("errors".into(), Value::Array(Vec::new()));
    report.insert(
        "legacy_snapshots".into(),
        serde_json::json!({"status": "absent"}),
    );
    report.insert("cache".into(), serde_json::json!({"status": "absent"}));
    if let Some(throttle_hours) = options.throttle_hours {
        if throttle_hours > 0.0 {
            let state = read_gc_state(output_root);
            let last = parse_utc(state.get("last_run_utc"));
            if let Some(last) = last {
                if last <= moment && (moment - last).num_seconds() as f64 / 3600.0 < throttle_hours
                {
                    let mut throttled = Map::new();
                    throttled.insert("status".into(), Value::String("throttled".into()));
                    throttled.insert("dry_run".into(), Value::Bool(options.dry_run));
                    throttled.insert(
                        "last_run_utc".into(),
                        state.get("last_run_utc").cloned().unwrap_or(Value::Null),
                    );
                    throttled.insert("throttle_hours".into(), Value::from(throttle_hours));
                    return Ok(Value::Object(throttled));
                }
            }
        }
    }
    let runs_root = output_root.join("runs");
    let mut runs_scanned = 0i64;
    let mut pending_cleaned: Vec<Value> = Vec::new();
    let mut runs_deleted: Vec<Value> = Vec::new();
    let mut runs_would_delete: Vec<Value> = Vec::new();
    let mut runs_skipped: Vec<Value> = Vec::new();
    let mut orphans_detected = 0i64;
    let mut orphans_removed = 0i64;
    let mut orphan_bytes = 0i64;
    if runs_root.exists() && !is_link(&runs_root) && runs_root.is_dir() {
        let mut entries: Vec<PathBuf> = std::fs::read_dir(&runs_root)
            .map_err(|error| error.to_string())?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .collect();
        entries.sort();
        for entry in entries {
            let name = entry
                .file_name()
                .map(|value| value.to_string_lossy().to_string())
                .unwrap_or_default();
            if !is_run_id(&name) || is_link(&entry) || !entry.is_dir() {
                continue;
            }
            runs_scanned += 1;
            let run_name = name.clone();
            let outcome = (|| -> Result<(), String> {
                let orphans = sweep_orphans(&entry, options.dry_run)?;
                let total = ["orphan_files", "orphan_directories"]
                    .iter()
                    .map(|key| {
                        orphans
                            .get(*key)
                            .and_then(Value::as_array)
                            .map(|items| items.len() as i64)
                            .unwrap_or(0)
                    })
                    .sum::<i64>();
                orphans_detected += total;
                if !options.dry_run {
                    orphans_removed += total;
                }
                orphan_bytes += orphans
                    .get("orphan_bytes")
                    .and_then(Value::as_i64)
                    .unwrap_or(0);
                let decision = decide_run(&entry, moment, options, false)?;
                match decision {
                    RunDecision::PipelineActive => {
                        runs_skipped.push(serde_json::json!({
                            "run_id": run_name.clone(), "reason": "pipeline_active"
                        }));
                        Ok(())
                    }
                    RunDecision::PendingIncomplete => {
                        let status = finish_pending(&entry, options.dry_run, clock, gate)?;
                        pending_cleaned.push(serde_json::json!({
                            "run_id": run_name.clone(), "cleanup_status": status
                        }));
                        let receipt_path = entry.join(CLEANUP_RECEIPT);
                        let complete = if receipt_path.is_file() {
                            read_receipt(&receipt_path)?
                                .get("state")
                                .and_then(Value::as_str)
                                == Some("complete")
                        } else {
                            false
                        };
                        if !complete {
                            runs_skipped.push(serde_json::json!({
                                "run_id": run_name.clone(), "reason": "cleanup_incomplete"
                            }));
                        }
                        Ok(())
                    }
                    RunDecision::NoManifestOrReceipt => {
                        runs_skipped.push(serde_json::json!({
                            "run_id": run_name.clone(), "reason": "no_manifest_or_receipt"
                        }));
                        Ok(())
                    }
                    RunDecision::NoCleanupReceipt => {
                        runs_skipped.push(serde_json::json!({
                            "run_id": run_name.clone(), "reason": "no_cleanup_receipt"
                        }));
                        Ok(())
                    }
                    RunDecision::CleanupIncomplete => {
                        runs_skipped.push(serde_json::json!({
                            "run_id": run_name.clone(), "reason": "cleanup_incomplete"
                        }));
                        Ok(())
                    }
                    RunDecision::RetainedSnapshotOrCollection => {
                        runs_skipped.push(serde_json::json!({
                            "run_id": run_name.clone(), "reason": "retained_snapshot_or_collection"
                        }));
                        Ok(())
                    }
                    RunDecision::Keep => Ok(()),
                    RunDecision::Expire => {
                        let outcome = expire_run(&entry, options.dry_run)?;
                        if outcome == "deleted" {
                            runs_deleted.push(Value::String(run_name.clone()));
                        } else {
                            runs_would_delete.push(Value::String(run_name.clone()));
                        }
                        Ok(())
                    }
                }
            })();
            if let Err(message) = outcome {
                let truncated: String = message.chars().take(160).collect();
                runs_skipped.push(serde_json::json!({
                    "run_id": run_name.clone(), "reason": format!("CleanupError: {truncated}")
                }));
            }
        }
    }
    let legacy = sweep_legacy_snapshots(output_root, 1.0, options.dry_run, moment)
        .unwrap_or_else(|message| {
            serde_json::json!({"status": "error", "reason": format!("CleanupError: {message}")})
        });
    let cache_target = options
        .cache_dir
        .clone()
        .unwrap_or_else(|| output_root.join("yfinance-cache"));
    let cache = prune_cache(&cache_target, options.cache_days, options.dry_run, moment)?;
    report.insert("runs_scanned".into(), Value::from(runs_scanned));
    report.insert("orphans_removed".into(), Value::from(orphans_removed));
    report.insert("orphans_detected".into(), Value::from(orphans_detected));
    report.insert("orphan_bytes".into(), Value::from(orphan_bytes));
    report.insert("pending_cleaned".into(), Value::Array(pending_cleaned));
    report.insert("runs_deleted".into(), Value::Array(runs_deleted));
    report.insert("runs_would_delete".into(), Value::Array(runs_would_delete));
    report.insert("runs_skipped".into(), Value::Array(runs_skipped));
    report.insert("legacy_snapshots".into(), legacy);
    report.insert("cache".into(), cache);
    if !options.dry_run {
        write_gc_state(output_root, moment, &Value::Object(report.clone()));
    }
    report.insert("generated_at_utc".into(), Value::String(clock.utc_now()));
    Ok(Value::Object(report))
}

fn finish_pending(
    root: &Path,
    dry_run: bool,
    clock: &dyn Clock,
    gate: &dyn CleanupGate,
) -> Result<String, String> {
    let options = CleanupOptions {
        dry_run,
        force_incomplete: false,
        delivery_unverified: true,
    };
    let value = crate::cleanup::cleanup_owned_run(root, None, None, &options, clock, gate)?;
    Ok(value
        .get("cleanup_status")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string())
}

/// True when `name` is a retained-only root (used by GC decision callers).
pub fn retained_root(name: &str) -> bool {
    RETAINED_ROOT_DIRS.contains(&name)
}

/// Render a GC report's run lists deterministically (helper for tests/logs).
pub fn report_runs(report: &Value, key: &str) -> Vec<String> {
    let mut names: Vec<String> = report
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    item.as_str().map(ToString::to_string).or_else(|| {
                        item.get("run_id")
                            .and_then(Value::as_str)
                            .map(ToString::to_string)
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    names
}

#[allow(dead_code)]
fn _unused(value: &Value) -> String {
    python_repr(value)
}

#[allow(dead_code)]
fn _checked(path: &Path) -> Result<PathBuf, String> {
    checked_directory(path, "path")
}

#[allow(dead_code)]
fn _orphan_ignore(value: &Value) -> Vec<String> {
    orphan_ignore(value)
}

#[allow(dead_code)]
fn _sha(path: &Path) -> Result<String, String> {
    sha256_file(path)
}

#[allow(dead_code)]
fn _clock_marker(clock: &dyn Clock) -> u32 {
    clock.now().hour()
}
