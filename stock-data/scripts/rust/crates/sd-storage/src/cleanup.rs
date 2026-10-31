//! Owner-scoped JSON cleanup after a validated research response (R10).
//!
//! Port of `quant_research/cleanup.py`. Deletion is always allow-list based: a
//! registered run manifest names the collection/snapshot/analysis and the exact JSON
//! inventory; the tree must contain no unknown file or directory, every target's
//! SHA-256 and size must still match the inventory, and the surviving Markdown must
//! still reproduce from the frozen `analysis.json`. Anything ambiguous blocks the
//! cleanup with an explicit error instead of guessing.
//!
//! Layering note: the legacy module imports `report`/`delivery` from `sd-app` to
//! re-render and validate the retained artifacts. `sd-storage` must not depend on
//! `sd-app`, so those two steps are expressed as the [`CleanupGate`] trait. The app
//! layer supplies the implementation in R11; [`UnwiredGate`] fails closed until then.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use sd_core::clock::Clock;
use sd_core::contracts::python_repr;

use crate::atomic_write::atomic_write_text;
use crate::jsonio::{read_json_file, write_json};
use crate::legacy_json::sha256_hex;
use crate::locks;
use crate::path_guard::{is_link, real_directory, validate_path_component};

/// Owned run manifest filename.
pub const RUN_MANIFEST: &str = "run_manifest.json";
/// Cleanup receipt filename.
pub const CLEANUP_RECEIPT: &str = "cleanup_receipt.txt";
/// Pipeline ownership marker filename.
pub const PIPELINE_LOCK: &str = ".pipeline.lock";
/// JSON artifacts an analysis directory may hold.
pub const ANALYSIS_JSON: [&str; 8] = [
    "analysis.json",
    "agent_summary.json",
    "news_result.json",
    "news_result.freeze.json",
    "audit.json",
    "data.json",
    "request.json",
    "snapshot.json",
];
/// Markdown artifacts that survive cleanup.
pub const ANALYSIS_MARKDOWN: [&str; 2] = ["report.md", "final_response.md"];
/// Prefixes of artifacts that outlive a run directory.
pub const RETAINED_PREFIXES: [&str; 2] = ["snapshots/", "collections/"];
/// Researcher identifiers whose JSON survives in an analysis directory.
pub const RESEARCHER_IDS: [&str; 4] = ["quant", "factor", "ml", "factor_backtest"];
/// Snapshot domains (mirrors `snapshot.DOMAINS`).
pub use crate::snapshot::DOMAINS;
/// Receipt format token.
pub const RECEIPT_FORMAT: &str = "stock-data-cleanup-v1";

/// Error type carrying the exact legacy message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CleanupError(pub String);

impl std::fmt::Display for CleanupError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for CleanupError {}

impl CleanupError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// Result alias used by this module.
pub type CleanupResult<T> = Result<T, String>;

fn fail<T>(message: impl Into<String>) -> CleanupResult<T> {
    Err(message.into())
}

/// Steps that need the app-layer report/delivery implementation.
pub trait CleanupGate {
    /// Validate a prepared response against the analysis bundle (`delivery.validate_final_response`).
    fn validate_final_response(
        &self,
        analysis_dir: &Path,
        response_file: &Path,
    ) -> Result<Value, String>;
    /// Render the fixed Markdown report (`report.render_markdown`).
    fn render_markdown(&self, report: &Value) -> Result<String, String>;
    /// Render the fixed final response (`report.render_final_response`).
    fn render_final_response(
        &self,
        report: &Value,
        summary: Option<&Value>,
    ) -> Result<String, String>;
    /// Validate response text against the fixed lines (`delivery.validate_response_text`).
    fn validate_response_text(&self, report: &Value, text: &str) -> Result<(), String>;
}

/// Fail-closed gate used until `sd-app` wires the real implementation (R11).
#[derive(Debug, Clone, Copy, Default)]
pub struct UnwiredGate;

impl CleanupGate for UnwiredGate {
    fn validate_final_response(
        &self,
        _analysis_dir: &Path,
        _response_file: &Path,
    ) -> Result<Value, String> {
        fail("cleanup delivery validation is not wired in sd-storage; use --force-incomplete or wire the sd-app gate")
    }

    fn render_markdown(&self, _report: &Value) -> Result<String, String> {
        fail("cleanup report rendering is not wired in sd-storage; wire the sd-app gate")
    }

    fn render_final_response(
        &self,
        _report: &Value,
        _summary: Option<&Value>,
    ) -> Result<String, String> {
        fail("cleanup report rendering is not wired in sd-storage; wire the sd-app gate")
    }

    fn validate_response_text(&self, _report: &Value, _text: &str) -> Result<(), String> {
        fail("cleanup delivery validation is not wired in sd-storage; wire the sd-app gate")
    }
}

/// Options shared by the two cleanup entry points.
#[derive(Debug, Clone, Default)]
pub struct CleanupOptions {
    /// Validate and report only; never delete or write a receipt.
    pub dry_run: bool,
    /// Allow cleaning a failed/interrupted run (missing files tolerated).
    pub force_incomplete: bool,
    /// Delivery is explicitly unverified (`--assume-delivered`).
    pub delivery_unverified: bool,
}

/// `utc_now()` formatting, injected so receipts are reproducible.
fn now_text(clock: &dyn Clock) -> String {
    clock.utc_now()
}

/// True when the entry is a symlink, junction or reparse point (delegates to the guard).
pub fn entry_is_link(path: &Path) -> bool {
    is_link(path)
}

/// Resolve a directory that must exist, be a real directory and not a link.
pub fn checked_directory(path: &Path, label: &str) -> CleanupResult<PathBuf> {
    real_directory(path, label).map_err(|error| CleanupError::new(error.message().to_string()).0)
}

/// Write a readable JSON artifact (atomic; exclusive maps to Python `open("x")`).
pub fn write_json_artifact(path: &Path, value: &Value, exclusive: bool) -> CleanupResult<()> {
    write_json(path, value, exclusive)
        .map(|_| ())
        .map_err(|error| error.message().to_string())
}

/// SHA-256 of a file's bytes (lowercase hex).
pub fn sha256_file(path: &Path) -> CleanupResult<String> {
    let bytes = std::fs::read(path).map_err(|error| error.to_string())?;
    Ok(sha256_hex(&bytes))
}

/// Resolve a registered relative member without following a linked segment.
pub fn member_path(root: &Path, relative: &str) -> CleanupResult<PathBuf> {
    if relative.is_empty()
        || relative.starts_with('/')
        || relative.starts_with('\\')
        || relative.contains('\\')
        || relative.contains(':')
    {
        return fail(format!(
            "cleanup member must be a safe relative path: {relative:?}"
        ));
    }
    let parts: Vec<&str> = relative.split('/').collect();
    if parts
        .iter()
        .any(|part| part.is_empty() || *part == "." || *part == "..")
    {
        return fail(format!(
            "cleanup member must be a safe relative path: {relative:?}"
        ));
    }
    let mut candidate = root.to_path_buf();
    for part in &parts {
        candidate = candidate.join(part);
        if (candidate.exists() || is_link(&candidate)) && is_link(&candidate) {
            return fail(format!(
                "refusing to follow linked cleanup member: {relative}"
            ));
        }
    }
    let resolved_root = root.canonicalize().map_err(|error| error.to_string())?;
    let resolved_candidate = candidate
        .canonicalize()
        .unwrap_or_else(|_| candidate.clone());
    if !resolved_candidate.starts_with(&resolved_root) {
        return fail(format!("cleanup member escapes its owned root: {relative}"));
    }
    Ok(candidate)
}

/// List every regular file and directory under `root`, refusing links.
pub fn tree_entries(
    root: &Path,
    ignore: &[String],
) -> CleanupResult<(BTreeSet<String>, BTreeSet<String>)> {
    fn excluded(relative: &str, ignored: &[String]) -> bool {
        ignored
            .iter()
            .any(|item| relative == item || relative.starts_with(&format!("{item}/")))
    }
    fn walk(
        root: &Path,
        current: &Path,
        ignored: &[String],
        files: &mut BTreeSet<String>,
        directories: &mut BTreeSet<String>,
    ) -> CleanupResult<()> {
        let entries = std::fs::read_dir(current).map_err(|error| error.to_string())?;
        let mut children: Vec<PathBuf> = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|error| error.to_string())?;
            children.push(entry.path());
        }
        for path in children {
            let name = path
                .file_name()
                .map(|value| value.to_string_lossy().into_owned())
                .unwrap_or_default();
            let relative = path
                .strip_prefix(root)
                .map(|value| value.to_string_lossy().replace('\\', "/"))
                .unwrap_or_default();
            if excluded(&relative, ignored) {
                continue;
            }
            let metadata = std::fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
            if metadata.is_dir() {
                if is_link(&path) {
                    return fail(format!(
                        "linked or non-directory path in cleanup tree: {name}"
                    ));
                }
                directories.insert(relative);
                walk(root, &path, ignored, files, directories)?;
            } else if metadata.is_file() {
                if is_link(&path) {
                    return fail(format!(
                        "linked or non-regular file in cleanup tree: {name}"
                    ));
                }
                files.insert(relative);
            } else {
                return fail(format!(
                    "linked or non-regular file in cleanup tree: {name}"
                ));
            }
        }
        Ok(())
    }
    let mut files = BTreeSet::new();
    let mut directories = BTreeSet::new();
    walk(root, root, ignore, &mut files, &mut directories)?;
    Ok((files, directories))
}

/// Remove scratch paths a hard-killed writer left inside an owned tree.
pub fn sweep_orphans(root: &Path, dry_run: bool) -> CleanupResult<Value> {
    let base = checked_directory(root, "run directory")?;
    let (files, directories) = tree_entries(&base, &[])?;
    let mut temp_dirs: Vec<String> = directories
        .iter()
        .filter(|name| {
            let leaf = name.rsplit('/').next().unwrap_or(name);
            crate::atomic_write::is_orphan_temp_dir(leaf)
        })
        .cloned()
        .collect();
    temp_dirs.sort();
    let snapshot = temp_dirs.clone();
    temp_dirs.retain(|name| {
        !snapshot
            .iter()
            .any(|other| other != name && name.starts_with(&format!("{other}/")))
    });
    let mut temp_files: Vec<String> = files
        .iter()
        .filter(|name| {
            let leaf = name.rsplit('/').next().unwrap_or(name);
            crate::atomic_write::is_orphan_temp_file(leaf)
                && !temp_dirs
                    .iter()
                    .any(|directory| name.starts_with(&format!("{directory}/")))
        })
        .cloned()
        .collect();
    temp_files.sort();
    let mut report = Map::new();
    report.insert(
        "orphan_files".into(),
        Value::Array(temp_files.iter().cloned().map(Value::String).collect()),
    );
    report.insert(
        "orphan_directories".into(),
        Value::Array(temp_dirs.iter().cloned().map(Value::String).collect()),
    );
    report.insert("orphan_bytes".into(), Value::from(0));
    report.insert("removed".into(), Value::Bool(!dry_run));
    if dry_run {
        let mut total = 0i64;
        for name in &temp_files {
            total += std::fs::metadata(base.join(name))
                .map(|metadata| metadata.len() as i64)
                .unwrap_or(0);
        }
        for directory in &temp_dirs {
            for name in files
                .iter()
                .filter(|name| name.starts_with(&format!("{directory}/")))
            {
                total += std::fs::metadata(base.join(name))
                    .map(|metadata| metadata.len() as i64)
                    .unwrap_or(0);
            }
        }
        report.insert("orphan_bytes".into(), Value::from(total));
        return Ok(Value::Object(report));
    }
    let mut removed_bytes = 0i64;
    for relative in &temp_files {
        let path = member_path(&base, relative)?;
        if !path.is_file() || is_link(&path) {
            return fail(format!(
                "orphan scratch file changed before removal: {relative}"
            ));
        }
        removed_bytes += std::fs::metadata(&path)
            .map(|metadata| metadata.len() as i64)
            .unwrap_or(0);
        std::fs::remove_file(&path).map_err(|error| error.to_string())?;
    }
    for relative in &temp_dirs {
        let path = member_path(&base, relative)?;
        if !path.is_dir() || is_link(&path) {
            return fail(format!(
                "orphan scratch directory changed before removal: {relative}"
            ));
        }
        let mut nested: Vec<String> = files
            .iter()
            .filter(|name| name.starts_with(&format!("{relative}/")))
            .cloned()
            .collect();
        nested.sort_by_key(|name| (name.matches('/').count(), name.clone()));
        nested.reverse();
        for name in nested {
            let child = member_path(&base, &name)?;
            if !child.is_file() || is_link(&child) {
                return fail(format!(
                    "orphan scratch file changed before removal: {name}"
                ));
            }
            removed_bytes += std::fs::metadata(&child)
                .map(|metadata| metadata.len() as i64)
                .unwrap_or(0);
            std::fs::remove_file(&child).map_err(|error| error.to_string())?;
        }
        let mut nested_dirs: Vec<String> = directories
            .iter()
            .filter(|name| name.starts_with(&format!("{relative}/")))
            .cloned()
            .collect();
        nested_dirs.sort_by_key(|name| (name.matches('/').count(), name.clone()));
        nested_dirs.reverse();
        for name in nested_dirs {
            let child = member_path(&base, &name)?;
            std::fs::remove_dir(&child).map_err(|error| error.to_string())?;
        }
        std::fs::remove_dir(&path).map_err(|error| error.to_string())?;
    }
    report.insert("orphan_bytes".into(), Value::from(removed_bytes));
    Ok(Value::Object(report))
}

/// Paths a dry run would have swept; validation must ignore them as if gone.
pub fn orphan_ignore(report: &Value) -> Vec<String> {
    let mut names: BTreeSet<String> = BTreeSet::new();
    for key in ["orphan_files", "orphan_directories"] {
        if let Some(items) = report.get(key).and_then(Value::as_array) {
            for item in items {
                if let Some(name) = item.as_str() {
                    names.insert(name.to_string());
                }
            }
        }
    }
    names.into_iter().collect()
}

/// Separate what was found from what was actually deleted.
pub fn orphan_counts(report: &Value, dry_run: bool) -> (i64, i64) {
    let total = ["orphan_files", "orphan_directories"]
        .iter()
        .map(|key| {
            report
                .get(*key)
                .and_then(Value::as_array)
                .map(|items| items.len() as i64)
                .unwrap_or(0)
        })
        .sum::<i64>();
    (total, if dry_run { 0 } else { total })
}

fn analysis_targets(analysis_id: &str) -> CleanupResult<BTreeSet<String>> {
    validate_path_component(analysis_id, "analysis_id")
        .map_err(|error| error.message().to_string())?;
    let mut names: BTreeSet<String> = RESEARCHER_IDS
        .iter()
        .map(|name| format!("research/{analysis_id}/researchers/{name}.json"))
        .collect();
    for name in ANALYSIS_JSON {
        names.insert(format!("research/{analysis_id}/{name}"));
    }
    Ok(names)
}

fn snapshot_targets(snapshot_id: &str) -> CleanupResult<BTreeSet<String>> {
    validate_path_component(snapshot_id, "snapshot_id")
        .map_err(|error| error.message().to_string())?;
    let base = format!("snapshots/{snapshot_id}");
    let mut names: BTreeSet<String> = BTreeSet::new();
    names.insert(format!("{base}/manifest.json"));
    for domain in DOMAINS {
        names.insert(format!("{base}/{domain}.json"));
        names.insert(format!("{base}/raw/{domain}.json"));
    }
    Ok(names)
}

// The news stages create these files after the immutable domain snapshot is
// frozen. They are optional (including after an interrupted stage), but only
// these exact producer filenames belong to an owned snapshot.
fn optional_snapshot_targets(snapshot_id: &str) -> CleanupResult<BTreeSet<String>> {
    validate_path_component(snapshot_id, "snapshot_id")
        .map_err(|error| error.message().to_string())?;
    Ok([
        "news_result.json",
        "news_result.freeze.json",
        "news_preview.json",
        "news_preview.freeze.json",
    ]
    .into_iter()
    .map(|name| format!("snapshots/{snapshot_id}/{name}"))
    .collect())
}

fn allowed_directories(
    analysis_id: Option<&str>,
    snapshot_id: Option<&str>,
    full_run: bool,
) -> BTreeSet<String> {
    let mut names: BTreeSet<String> = BTreeSet::new();
    if full_run {
        for name in ["collections", "snapshots", "research"] {
            names.insert(name.to_string());
        }
    }
    if let Some(snapshot_id) = snapshot_id {
        names.insert(format!("snapshots/{snapshot_id}"));
        names.insert(format!("snapshots/{snapshot_id}/raw"));
    }
    if let Some(analysis_id) = analysis_id {
        names.insert(format!("research/{analysis_id}"));
        names.insert(format!("research/{analysis_id}/researchers"));
    }
    names
}

fn allowed_markdown(analysis_id: Option<&str>, full_run: bool) -> BTreeSet<String> {
    match analysis_id {
        None => BTreeSet::new(),
        Some(analysis_id) => {
            let base = if full_run {
                format!("research/{analysis_id}")
            } else {
                String::new()
            };
            ANALYSIS_MARKDOWN
                .iter()
                .map(|name| {
                    if base.is_empty() {
                        (*name).to_string()
                    } else {
                        format!("{base}/{name}")
                    }
                })
                .collect()
        }
    }
}

fn expected_full_targets(manifest: &Value) -> CleanupResult<BTreeSet<String>> {
    let mut targets: BTreeSet<String> = BTreeSet::new();
    targets.insert(RUN_MANIFEST.to_string());
    if let Some(collection_name) = manifest.get("collection_name").and_then(Value::as_str) {
        validate_path_component(collection_name, "collection filename")
            .map_err(|error| error.message().to_string())?;
        if !collection_name.starts_with("collection_") || !collection_name.ends_with(".json") {
            return fail("owned collection filename is invalid");
        }
        targets.insert(format!("collections/{collection_name}"));
    }
    if let Some(snapshot_id) = manifest.get("snapshot_id").and_then(Value::as_str) {
        targets.extend(snapshot_targets(snapshot_id)?);
    }
    if let Some(analysis_id) = manifest.get("analysis_id").and_then(Value::as_str) {
        targets.extend(analysis_targets(analysis_id)?);
    }
    Ok(targets)
}

fn python_str_value(value: Option<&Value>) -> String {
    match value {
        None => "None".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => python_repr(other),
    }
}

/// Receipt text (`_receipt_data` / `_format_receipt`).
#[allow(clippy::too_many_arguments)]
pub fn receipt_data(
    mode: &str,
    run_id: &str,
    analysis_id: Option<&str>,
    snapshot_id: Option<&str>,
    collection_name: Option<&str>,
    state: &str,
    response_hash: Option<&str>,
    report_hash: Option<&str>,
    final_response_hash: Option<&str>,
    analysis_status: Option<&str>,
    reporting_status: Option<&str>,
    targets: &[(String, String, i64)],
    removed_file_count: i64,
    removed_bytes: i64,
    errors: &[String],
    delivery: &str,
    now_utc: &str,
) -> String {
    let mut lines: Vec<String> = vec![
        format!("format={RECEIPT_FORMAT}"),
        format!("mode={mode}"),
        format!("run_id={run_id}"),
        format!("analysis_id={}", analysis_id.unwrap_or("")),
        format!("snapshot_id={}", snapshot_id.unwrap_or("")),
        format!("collection_name={}", collection_name.unwrap_or("")),
        format!("state={state}"),
        format!("delivery={delivery}"),
        format!("response_sha256={}", response_hash.unwrap_or("")),
        format!("report_sha256={}", report_hash.unwrap_or("")),
        format!(
            "final_response_sha256={}",
            final_response_hash.unwrap_or("")
        ),
        format!("analysis_status={}", analysis_status.unwrap_or("")),
        format!("reporting_status={}", reporting_status.unwrap_or("")),
        format!("target_file_count={}", targets.len()),
        format!(
            "target_json_bytes={}",
            targets.iter().map(|(_, _, size)| *size).sum::<i64>()
        ),
        format!("removed_file_count={removed_file_count}"),
        format!("removed_json_bytes={removed_bytes}"),
        format!("updated_at_utc={now_utc}"),
    ];
    for (name, digest, size) in targets {
        lines.push(format!("target={name}\t{digest}\t{size}"));
    }
    for item in errors {
        let flattened = item.replace('\n', " ");
        let truncated: String = flattened.chars().take(200).collect();
        lines.push(format!("error={truncated}"));
    }
    let mut text = lines.join("\n");
    text.push('\n');
    text
}

fn metadata_str(metadata: &Value, key: &str, default: &str) -> String {
    metadata
        .get(key)
        .map(|value| python_str_value(Some(value)))
        .unwrap_or_else(|| default.to_string())
}

/// Format a receipt from cleanup metadata.
pub fn format_receipt(
    metadata: &Value,
    state: &str,
    targets: &[(String, String, i64)],
    errors: &[String],
    now_utc: &str,
) -> String {
    let removed_file_count = metadata
        .get("removed_file_count")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let removed_bytes = metadata
        .get("removed_bytes")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    receipt_data(
        &metadata_str(metadata, "mode", ""),
        &metadata_str(metadata, "run_id", ""),
        metadata.get("analysis_id").and_then(Value::as_str),
        metadata.get("snapshot_id").and_then(Value::as_str),
        metadata.get("collection_name").and_then(Value::as_str),
        state,
        metadata.get("response_hash").and_then(Value::as_str),
        metadata.get("report_hash").and_then(Value::as_str),
        metadata.get("final_response_hash").and_then(Value::as_str),
        metadata.get("analysis_status").and_then(Value::as_str),
        metadata.get("reporting_status").and_then(Value::as_str),
        targets,
        removed_file_count,
        removed_bytes,
        errors,
        &metadata_str(metadata, "delivery", ""),
        now_utc,
    )
}

/// Parsed cleanup receipt.
#[derive(Debug, Clone)]
pub struct Receipt {
    /// Scalar values in their first-seen order.
    pub values: Map<String, Value>,
    /// Registered targets `(name, digest, size)`.
    pub targets: Vec<(String, String, i64)>,
    /// Recorded errors.
    pub errors: Vec<String>,
}

impl Receipt {
    /// Python dict shape (`{**values, "targets": [...], "errors": [...]}`).
    pub fn to_value(&self) -> Value {
        let mut map = self.values.clone();
        map.insert(
            "targets".into(),
            Value::Array(
                self.targets
                    .iter()
                    .map(|(name, digest, size)| {
                        Value::Array(vec![
                            Value::String(name.clone()),
                            Value::String(digest.clone()),
                            Value::from(*size),
                        ])
                    })
                    .collect(),
            ),
        );
        map.insert(
            "errors".into(),
            Value::Array(self.errors.iter().cloned().map(Value::String).collect()),
        );
        Value::Object(map)
    }

    /// Scalar field access.
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.values.get(key)
    }
}

fn is_lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .chars()
            .all(|character| matches!(character, '0'..='9' | 'a'..='f'))
}

/// Validate a safe relative JSON target name.
pub fn validate_relative_target(value: &str) -> CleanupResult<()> {
    let relative = Path::new(value);
    let normalized = value.replace('\\', "/");
    let valid = !value.is_empty()
        && !value.contains('\\')
        && !value.contains(':')
        && normalized == value
        && relative
            .components()
            .all(|component| matches!(component, std::path::Component::Normal(_)))
        && value.to_lowercase().ends_with(".json");
    if !valid {
        return fail("cleanup target must be a safe relative JSON filename");
    }
    Ok(())
}

/// Read and validate a cleanup receipt.
pub fn read_receipt(path: &Path) -> CleanupResult<Receipt> {
    if is_link(path) || !path.is_file() {
        return fail("cleanup receipt is missing or linked");
    }
    let text = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    let mut values: Map<String, Value> = Map::new();
    let mut targets: Vec<(String, String, i64)> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("target=") {
            let fields: Vec<&str> = rest.split('\t').collect();
            if fields.len() != 3 {
                return fail("cleanup receipt contains a malformed target");
            }
            let Ok(size) = fields[2].parse::<i64>() else {
                return fail("cleanup receipt contains an invalid target size");
            };
            if size < 0 {
                return fail("cleanup receipt contains an invalid target size");
            }
            targets.push((fields[0].to_string(), fields[1].to_string(), size));
        } else if let Some(rest) = line.strip_prefix("error=") {
            errors.push(rest.to_string());
        } else if let Some((key, value)) = line.split_once('=') {
            values.insert(key.to_string(), Value::String(value.to_string()));
        }
    }
    if values.get("format").and_then(Value::as_str) != Some(RECEIPT_FORMAT) {
        return fail("cleanup receipt format or target list is invalid");
    }
    let state = values.get("state").and_then(Value::as_str).unwrap_or("");
    if !matches!(state, "in_progress" | "partial" | "complete") {
        return fail("cleanup receipt state is invalid");
    }
    let delivery = values.get("delivery").and_then(Value::as_str).unwrap_or("");
    if !matches!(delivery, "" | "verified" | "unverified") {
        return fail("cleanup receipt delivery mode is invalid");
    }
    let unique: BTreeSet<&str> = targets.iter().map(|(name, _, _)| name.as_str()).collect();
    if unique.len() != targets.len() {
        return fail("cleanup receipt contains duplicate targets");
    }
    for (name, digest, _) in &targets {
        validate_relative_target(name)?;
        if !is_lower_hex_64(digest) {
            return fail("cleanup receipt contains an invalid file digest");
        }
    }
    for key in ["response_sha256", "report_sha256", "final_response_sha256"] {
        let digest = values.get(key).and_then(Value::as_str).unwrap_or("");
        if !digest.is_empty() && !is_lower_hex_64(digest) {
            return fail(format!("cleanup receipt contains an invalid {key}"));
        }
    }
    let parse_i64 = |key: &str| -> CleanupResult<i64> {
        values
            .get(key)
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<i64>().ok())
            .ok_or_else(|| CleanupError::new("cleanup receipt contains invalid file/byte totals").0)
    };
    let target_count = parse_i64("target_file_count")?;
    let target_bytes = parse_i64("target_json_bytes")?;
    let removed_count = parse_i64("removed_file_count")?;
    let removed_bytes = parse_i64("removed_json_bytes")?;
    if target_count != targets.len() as i64
        || target_bytes != targets.iter().map(|(_, _, size)| *size).sum::<i64>()
        || removed_count < 0
        || removed_bytes < 0
        || removed_count > target_count
        || removed_bytes > target_bytes
    {
        return fail("cleanup receipt file/byte totals do not match its inventory");
    }
    if state == "complete" && (removed_count != target_count || removed_bytes != target_bytes) {
        return fail("complete cleanup receipt does not account for its full inventory");
    }
    Ok(Receipt {
        values,
        targets,
        errors,
    })
}

fn target_path(root: &Path, relative: &str) -> CleanupResult<PathBuf> {
    validate_relative_target(relative)?;
    member_path(root, relative)
}

fn validate_tree(
    root: &Path,
    expected_files: &BTreeSet<String>,
    expected_dirs: &BTreeSet<String>,
    allowed_markdown: &BTreeSet<String>,
    allow_missing: bool,
    ignore: &[String],
) -> CleanupResult<BTreeSet<String>> {
    let (files, directories) = tree_entries(root, ignore)?;
    let mut allowed_files = expected_files.clone();
    allowed_files.extend(allowed_markdown.iter().cloned());
    let unknown_files: Vec<String> = files.difference(&allowed_files).cloned().collect();
    let unknown_dirs: Vec<String> = directories.difference(expected_dirs).cloned().collect();
    if !unknown_files.is_empty() || !unknown_dirs.is_empty() {
        let mut names: Vec<String> = unknown_files;
        names.extend(unknown_dirs);
        names.sort();
        names.truncate(8);
        return fail(format!(
            "unknown files or directories prevent cleanup: {}",
            names.join(", ")
        ));
    }
    if !allow_missing {
        let missing: Vec<String> = expected_files.difference(&files).cloned().collect();
        if !missing.is_empty() {
            let mut names = missing;
            names.sort();
            names.truncate(8);
            return fail(format!(
                "registered cleanup files are missing: {}",
                names.join(", ")
            ));
        }
    }
    Ok(files)
}

fn verify_targets(
    root: &Path,
    targets: &[(String, String, i64)],
) -> CleanupResult<(i64, Vec<String>)> {
    let mut size = 0i64;
    let mut existing: Vec<String> = Vec::new();
    for (relative, expected_hash, expected_size) in targets {
        let path = target_path(root, relative)?;
        if !path.exists() {
            continue;
        }
        if !path.is_file() {
            return fail(format!("cleanup target is not a regular file: {relative}"));
        }
        let actual_size = std::fs::metadata(&path)
            .map(|metadata| metadata.len() as i64)
            .unwrap_or(0);
        if sha256_file(&path)? != *expected_hash || actual_size != *expected_size {
            return fail(format!(
                "cleanup target changed after inventory: {relative}"
            ));
        }
        size += actual_size;
        existing.push(relative.clone());
    }
    Ok((size, existing))
}

fn remove_empty_directories(root: &Path, names: &[String]) -> CleanupResult<Vec<String>> {
    let mut ordered: Vec<String> = names.to_vec();
    ordered.sort_by_key(|item| (item.matches('/').count(), item.clone()));
    ordered.reverse();
    let mut removed: Vec<String> = Vec::new();
    for relative in ordered {
        let parts: Vec<&str> = relative.split('/').collect();
        let mut path = root.to_path_buf();
        for part in parts {
            path.push(part);
        }
        if !path.exists() {
            continue;
        }
        if is_link(&path) || !path.is_dir() {
            return fail(format!("refusing to remove changed directory: {relative}"));
        }
        std::fs::remove_dir(&path).map_err(|error| error.to_string())?;
        removed.push(relative);
    }
    Ok(removed)
}

fn value_i64(metadata: &Value, key: &str, default: i64) -> i64 {
    metadata.get(key).and_then(Value::as_i64).unwrap_or(default)
}

fn execute_cleanup(
    root: &Path,
    receipt_path: &Path,
    metadata: &Value,
    targets: &[(String, String, i64)],
    dry_run: bool,
    now_utc: &str,
) -> CleanupResult<Value> {
    let (size, existing) = verify_targets(root, targets)?;
    let target_bytes: i64 = targets.iter().map(|(_, _, size)| *size).sum();
    if dry_run {
        let mut result = Map::new();
        result.insert("cleanup_status".into(), Value::String("dry_run".into()));
        result.insert(
            "mode".into(),
            Value::String(metadata_str(metadata, "mode", "")),
        );
        result.insert("json_file_count".into(), Value::from(existing.len()));
        result.insert("json_bytes".into(), Value::from(size));
        result.insert(
            "files".into(),
            Value::Array(existing.into_iter().map(Value::String).collect()),
        );
        result.insert(
            "receipt_file".into(),
            Value::String(receipt_path.to_string_lossy().to_string()),
        );
        result.insert(
            "orphans_removed".into(),
            Value::from(value_i64(metadata, "orphans_removed", 0)),
        );
        result.insert(
            "orphans_detected".into(),
            Value::from(value_i64(metadata, "orphans_detected", 0)),
        );
        return Ok(Value::Object(result));
    }
    if let Some(parent) = receipt_path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    if !receipt_path.exists() {
        let text = format_receipt(metadata, "in_progress", targets, &[], now_utc);
        atomic_write_text(receipt_path, &text).map_err(|error| error.message().to_string())?;
    }
    let mut removed: Vec<String> = Vec::new();
    let mut removed_bytes = 0i64;
    let mut errors: Vec<String> = Vec::new();
    let mut ordered: Vec<&(String, String, i64)> = targets.iter().collect();
    ordered.sort_by_key(|item| item.0 == RUN_MANIFEST);
    for (relative, expected_hash, expected_size) in ordered {
        let path = target_path(root, relative)?;
        if !path.exists() {
            continue;
        }
        let check = || -> CleanupResult<i64> {
            let path = target_path(root, relative)?;
            let actual_size = std::fs::metadata(&path)
                .map(|metadata| metadata.len() as i64)
                .unwrap_or(0);
            if !path.is_file()
                || sha256_file(&path)? != *expected_hash
                || actual_size != *expected_size
            {
                return fail(format!(
                    "cleanup target changed after inventory: {relative}"
                ));
            }
            Ok(actual_size)
        };
        match check() {
            Ok(file_bytes) => match std::fs::remove_file(&path) {
                Ok(()) => {
                    removed.push(relative.clone());
                    removed_bytes += file_bytes;
                }
                Err(error) => {
                    errors.push(format!("{relative}: {}", error.kind()));
                    break;
                }
            },
            Err(message) => {
                errors.push(message);
                break;
            }
        }
    }
    if errors.is_empty() {
        let dirs: Vec<String> = metadata
            .get("empty_directories")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default();
        if let Err(message) = remove_empty_directories(root, &dirs) {
            let truncated: String = message.chars().take(160).collect();
            errors.push(format!("directory_cleanup: CleanupError: {truncated}"));
        }
    }
    let state = if errors.is_empty() {
        "complete"
    } else {
        "partial"
    };
    let cleanup_status = if errors.is_empty() {
        "complete"
    } else {
        "partial_cleanup"
    };
    let mut remaining: Vec<String> = Vec::new();
    for (relative, _, _) in targets {
        if target_path(root, relative)?.exists() {
            remaining.push(relative.clone());
        }
    }
    let mut receipt_metadata = metadata.clone();
    if let Some(map) = receipt_metadata.as_object_mut() {
        map.insert(
            "removed_file_count".into(),
            Value::from((targets.len() - remaining.len()) as i64),
        );
        map.insert(
            "removed_bytes".into(),
            Value::from(
                target_bytes
                    - targets
                        .iter()
                        .filter(|(relative, _, _)| remaining.contains(relative))
                        .map(|(_, _, size)| *size)
                        .sum::<i64>(),
            ),
        );
    }
    let text = format_receipt(&receipt_metadata, state, targets, &errors, now_utc);
    atomic_write_text(receipt_path, &text).map_err(|error| error.message().to_string())?;
    let retained_files: Vec<String> = metadata
        .get("retained_paths")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .filter(|name| root.join(name).is_file())
                .map(|name| root.join(name).to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default();
    let mut result = Map::new();
    result.insert(
        "cleanup_status".into(),
        Value::String(cleanup_status.into()),
    );
    result.insert(
        "mode".into(),
        Value::String(metadata_str(metadata, "mode", "")),
    );
    result.insert(
        "analysis_id".into(),
        metadata.get("analysis_id").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "run_id".into(),
        metadata.get("run_id").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "analysis_status".into(),
        metadata
            .get("analysis_status")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert(
        "reporting_status".into(),
        metadata
            .get("reporting_status")
            .cloned()
            .unwrap_or(Value::Null),
    );
    result.insert("json_file_count".into(), Value::from(removed.len()));
    result.insert("json_bytes".into(), Value::from(removed_bytes));
    result.insert("planned_json_bytes".into(), Value::from(target_bytes));
    result.insert(
        "remaining_json_files".into(),
        Value::Array(remaining.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "errors".into(),
        Value::Array(errors.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "retained_files".into(),
        Value::Array(retained_files.into_iter().map(Value::String).collect()),
    );
    result.insert(
        "orphans_removed".into(),
        Value::from(value_i64(metadata, "orphans_removed", 0)),
    );
    result.insert(
        "orphans_detected".into(),
        Value::from(value_i64(metadata, "orphans_detected", 0)),
    );
    result.insert(
        "receipt_file".into(),
        Value::String(receipt_path.to_string_lossy().to_string()),
    );
    Ok(Value::Object(result))
}

fn read_analysis_identity(analysis_dir: &Path) -> CleanupResult<(PathBuf, String)> {
    let analysis = checked_directory(analysis_dir, "analysis directory")?;
    let name = analysis
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_default();
    validate_path_component(&name, "analysis directory name")
        .map_err(|error| error.message().to_string())?;
    Ok((analysis, name))
}

fn retained_hashes(analysis_dir: &Path, required: bool) -> CleanupResult<Map<String, Value>> {
    let mut hashes = Map::new();
    for (name, key) in [
        ("report.md", "report_hash"),
        ("final_response.md", "final_response_hash"),
    ] {
        let path = analysis_dir.join(name);
        if !path.exists() {
            if required {
                return fail(format!("required retained artifact is missing: {name}"));
            }
            continue;
        }
        if is_link(&path) || !path.is_file() {
            return fail(format!("retained artifact is linked or not a file: {name}"));
        }
        hashes.insert(key.into(), Value::String(sha256_file(&path)?));
    }
    Ok(hashes)
}

fn verify_retained_hashes(analysis_dir: &Path, receipt: &Receipt) -> CleanupResult<()> {
    for (name, key) in [
        ("report.md", "report_sha256"),
        ("final_response.md", "final_response_sha256"),
    ] {
        let Some(expected_hash) = receipt.get(key).and_then(Value::as_str) else {
            continue;
        };
        if expected_hash.is_empty() {
            continue;
        }
        let path = analysis_dir.join(name);
        if is_link(&path) || !path.is_file() || sha256_file(&path)? != expected_hash {
            return fail(format!(
                "retained artifact changed after cleanup inventory: {name}"
            ));
        }
    }
    Ok(())
}

fn verify_retained_artifacts(analysis_dir: &Path, gate: &dyn CleanupGate) -> CleanupResult<()> {
    let report_path = analysis_dir.join("analysis.json");
    if !report_path.is_file() {
        return fail("analysis.json is required to verify the retained artifacts");
    }
    let report = read_json_file(&report_path).map_err(|error| error.message().to_string())?;
    let report_md_path = analysis_dir.join("report.md");
    if report_md_path.is_file() {
        let text = std::fs::read_to_string(&report_md_path).map_err(|error| error.to_string())?;
        if text != gate.render_markdown(&report)? {
            return fail("retained report.md does not reproduce from report.json; cleanup stopped");
        }
    }
    let response_path = analysis_dir.join("final_response.md");
    if response_path.is_file() {
        let summary_path = analysis_dir.join("agent_summary.json");
        let summary = if summary_path.is_file() {
            Some(read_json_file(&summary_path).map_err(|error| error.message().to_string())?)
        } else {
            None
        };
        let response_text =
            std::fs::read_to_string(&response_path).map_err(|error| error.to_string())?;
        if response_text != gate.render_final_response(&report, summary.as_ref())? {
            return fail(
                "retained final_response.md does not reproduce from report.json; cleanup stopped",
            );
        }
        gate.validate_response_text(&report, &response_text)?;
    }
    Ok(())
}

fn orphan_result_counts(orphans: &Value, dry_run: bool) -> (i64, i64) {
    orphan_counts(orphans, dry_run)
}

fn insert_orphan_counts(metadata: &mut Map<String, Value>, orphans: &Value, dry_run: bool) {
    let (detected, removed) = orphan_result_counts(orphans, dry_run);
    metadata.insert("orphans_removed".into(), Value::from(removed));
    metadata.insert("orphans_detected".into(), Value::from(detected));
}

/// Clean only explicitly named analysis JSON; never inspect or delete its snapshot.
pub fn cleanup_analysis(
    analysis_dir: &Path,
    response_file: Option<&Path>,
    options: &CleanupOptions,
    clock: &dyn Clock,
    gate: &dyn CleanupGate,
) -> CleanupResult<Value> {
    let (analysis, analysis_id) = read_analysis_identity(analysis_dir)?;
    if analysis
        .parent()
        .and_then(|parent| parent.file_name())
        .map(|name| name.to_string_lossy() == "research")
        .unwrap_or(false)
    {
        let run_root = analysis
            .parent()
            .and_then(|parent| parent.parent())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| analysis.clone());
        let run_manifest = run_root.join(RUN_MANIFEST);
        let run_receipt = run_root.join(CLEANUP_RECEIPT);
        if is_link(&run_manifest) {
            return fail("owned run manifest is linked; use the run directory after repairing it");
        }
        if run_manifest.exists() {
            let run_data = read_json_file(&run_manifest).map_err(|_| {
                "owned run manifest is unreadable; analysis-only cleanup is blocked".to_string()
            })?;
            let run_mode = run_data.get("mode").and_then(Value::as_str);
            if run_mode == Some("owned_pipeline") {
                return fail(
                    "analysis belongs to an owned pipeline; use --run-dir to preserve run inventory",
                );
            }
        }
        if is_link(&run_receipt) {
            return fail("owned run cleanup receipt is linked; analysis-only cleanup is blocked");
        }
        if run_receipt.exists() {
            if !run_receipt.is_file() {
                return fail("owned run cleanup receipt is not a file");
            }
            let receipt = read_receipt(&run_receipt).map_err(|_| {
                "owned run cleanup receipt is unreadable; analysis-only cleanup is blocked"
                    .to_string()
            })?;
            if receipt.get("mode").and_then(Value::as_str) == Some("run") {
                return fail(
                    "analysis belongs to an owned pipeline; use --run-dir to preserve run inventory",
                );
            }
        }
    }
    let lock_path = analysis
        .parent()
        .map(|parent| parent.join(format!(".{analysis_id}.cleanup.lock")))
        .unwrap_or_else(|| PathBuf::from(format!(".{analysis_id}.cleanup.lock")));
    let _lock = locks::acquire_cleanup_lock(&lock_path)?;
    let now_utc = now_text(clock);
    let orphans = sweep_orphans(&analysis, options.dry_run)?;
    let ignored = orphan_ignore(&orphans);
    let receipt_path = analysis.join(CLEANUP_RECEIPT);
    let existing_receipt = if receipt_path.exists() {
        Some(read_receipt(&receipt_path)?)
    } else {
        None
    };
    let mut expected: BTreeSet<String> = ANALYSIS_JSON
        .iter()
        .map(|name| (*name).to_string())
        .collect();
    expected.extend(
        RESEARCHER_IDS
            .iter()
            .map(|name| format!("researchers/{name}.json")),
    );
    if let Some(receipt) = &existing_receipt {
        if receipt.get("mode").and_then(Value::as_str) != Some("analysis")
            || receipt.get("analysis_id").and_then(Value::as_str) != Some(analysis_id.as_str())
        {
            return fail("existing cleanup receipt belongs to a different analysis");
        }
        if receipt.get("state").and_then(Value::as_str) == Some("complete") {
            let (files, directories) = tree_entries(&analysis, &ignored)?;
            let mut allowed: BTreeSet<String> = ANALYSIS_MARKDOWN
                .iter()
                .map(|name| (*name).to_string())
                .collect();
            allowed.insert(CLEANUP_RECEIPT.to_string());
            if !files.difference(&allowed).next().is_none() || !directories.is_empty() {
                return fail("JSON or unexpected files appeared after cleanup");
            }
            verify_retained_hashes(&analysis, receipt)?;
            if let Some(response_file) = response_file {
                if let Some(expected_hash) = receipt.get("response_sha256").and_then(Value::as_str)
                {
                    if !expected_hash.is_empty() && sha256_file(response_file)? != expected_hash {
                        return fail(
                            "prepared response differs from the response previously validated",
                        );
                    }
                }
            }
            let mut result = Map::new();
            result.insert(
                "cleanup_status".into(),
                Value::String("already_cleaned".into()),
            );
            result.insert("mode".into(), Value::String("analysis".into()));
            result.insert("analysis_id".into(), Value::String(analysis_id));
            result.insert(
                "analysis_status".into(),
                receipt
                    .get("analysis_status")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(|value| Value::String(value.to_string()))
                    .unwrap_or(Value::Null),
            );
            result.insert(
                "reporting_status".into(),
                receipt
                    .get("reporting_status")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(|value| Value::String(value.to_string()))
                    .unwrap_or(Value::Null),
            );
            let removed_count = receipt
                .get("removed_file_count")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(0);
            let removed_bytes = receipt
                .get("removed_json_bytes")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(0);
            result.insert("json_file_count".into(), Value::from(removed_count));
            result.insert("json_bytes".into(), Value::from(removed_bytes));
            let (detected, removed) = orphan_result_counts(&orphans, options.dry_run);
            result.insert("orphans_removed".into(), Value::from(removed));
            result.insert("orphans_detected".into(), Value::from(detected));
            result.insert(
                "retained_files".into(),
                Value::Array(
                    ANALYSIS_MARKDOWN
                        .iter()
                        .filter(|name| analysis.join(name).is_file())
                        .map(|name| {
                            Value::String(analysis.join(name).to_string_lossy().to_string())
                        })
                        .collect(),
                ),
            );
            result.insert("errors".into(), Value::Array(Vec::new()));
            result.insert(
                "receipt_file".into(),
                Value::String(receipt_path.to_string_lossy().to_string()),
            );
            return Ok(Value::Object(result));
        }
        let targets = receipt.targets.clone();
        for (name, _, _) in &targets {
            if !expected.contains(name) {
                return fail("analysis cleanup receipt contains an unowned target");
            }
        }
        let (files, directories) = tree_entries(&analysis, &ignored)?;
        let mut allowed: BTreeSet<String> = expected.clone();
        allowed.extend(ANALYSIS_MARKDOWN.iter().map(|name| (*name).to_string()));
        allowed.insert(CLEANUP_RECEIPT.to_string());
        let mut allowed_dirs: BTreeSet<String> = BTreeSet::new();
        allowed_dirs.insert("researchers".into());
        if !files.difference(&allowed).next().is_none()
            || !directories.difference(&allowed_dirs).next().is_none()
        {
            return fail("unknown files or directories prevent cleanup");
        }
        let registered: BTreeSet<String> =
            targets.iter().map(|(name, _, _)| name.clone()).collect();
        let appeared: BTreeSet<String> = files
            .difference(&registered)
            .filter(|name| expected.contains(*name))
            .cloned()
            .collect();
        if !appeared.is_empty() {
            return fail("unregistered analysis JSON appeared after cleanup inventory");
        }
        verify_retained_hashes(&analysis, receipt)?;
        if let Some(response_file) = response_file {
            if let Some(expected_hash) = receipt.get("response_sha256").and_then(Value::as_str) {
                if !expected_hash.is_empty() && sha256_file(response_file)? != expected_hash {
                    return fail(
                        "prepared response differs from the response previously validated",
                    );
                }
            }
        }
        let mut metadata = Map::new();
        metadata.insert("mode".into(), Value::String("analysis".into()));
        metadata.insert("run_id".into(), Value::String(String::new()));
        metadata.insert("analysis_id".into(), Value::String(analysis_id.clone()));
        metadata.insert("snapshot_id".into(), Value::String(String::new()));
        metadata.insert("collection_name".into(), Value::String(String::new()));
        metadata.insert(
            "response_hash".into(),
            receipt
                .get("response_sha256")
                .cloned()
                .unwrap_or(Value::Null),
        );
        metadata.insert(
            "report_hash".into(),
            receipt.get("report_sha256").cloned().unwrap_or(Value::Null),
        );
        metadata.insert(
            "final_response_hash".into(),
            receipt
                .get("final_response_sha256")
                .cloned()
                .unwrap_or(Value::Null),
        );
        metadata.insert(
            "analysis_status".into(),
            receipt
                .get("analysis_status")
                .cloned()
                .unwrap_or(Value::Null),
        );
        metadata.insert(
            "reporting_status".into(),
            receipt
                .get("reporting_status")
                .cloned()
                .unwrap_or(Value::Null),
        );
        let removed_file_count = receipt
            .get("removed_file_count")
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(0);
        let removed_bytes = receipt
            .get("removed_json_bytes")
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(0);
        metadata.insert("removed_file_count".into(), Value::from(removed_file_count));
        metadata.insert("removed_bytes".into(), Value::from(removed_bytes));
        metadata.insert(
            "empty_directories".into(),
            Value::Array(vec![Value::String("researchers".into())]),
        );
        metadata.insert(
            "retained_paths".into(),
            Value::Array(
                ANALYSIS_MARKDOWN
                    .iter()
                    .map(|name| Value::String((*name).to_string()))
                    .collect(),
            ),
        );
        metadata.insert(
            "delivery".into(),
            receipt
                .get("delivery")
                .cloned()
                .unwrap_or(Value::String(String::new())),
        );
        insert_orphan_counts(&mut metadata, &orphans, options.dry_run);
        return execute_cleanup(
            &analysis,
            &receipt_path,
            &Value::Object(metadata),
            &targets,
            options.dry_run,
            &now_utc,
        );
    }

    let actual = validate_tree(
        &analysis,
        &expected,
        &BTreeSet::from(["researchers".to_string()]),
        &ANALYSIS_MARKDOWN
            .iter()
            .map(|name| (*name).to_string())
            .collect(),
        options.force_incomplete,
        &ignored,
    )?;
    let mut validation: Option<Value> = None;
    let mut response_hash: Option<String> = None;
    if !options.force_incomplete {
        if response_file.is_none() && !options.dry_run && !options.delivery_unverified {
            return fail(
                "response_file is required unless --force-incomplete or --assume-delivered is explicit",
            );
        }
        if let Some(response_file) = response_file {
            let value = gate.validate_final_response(&analysis, response_file)?;
            if value.get("analysis_id").and_then(Value::as_str) != Some(analysis_id.as_str()) {
                return fail("prepared response validation returned a different analysis identity");
            }
            response_hash = Some(sha256_file(response_file)?);
            validation = Some(value);
        }
        verify_retained_artifacts(&analysis, gate)?;
    }
    let retained = retained_hashes(&analysis, !options.force_incomplete)?;
    let mut targets: Vec<(String, String, i64)> = Vec::new();
    for name in expected.intersection(&actual) {
        let path = analysis.join(name);
        let size = std::fs::metadata(&path)
            .map(|metadata| metadata.len() as i64)
            .unwrap_or(0);
        targets.push((name.clone(), sha256_file(&path)?, size));
    }
    targets.sort_by(|left, right| left.0.cmp(&right.0));
    let mut metadata = Map::new();
    metadata.insert("mode".into(), Value::String("analysis".into()));
    metadata.insert("run_id".into(), Value::String(String::new()));
    metadata.insert("analysis_id".into(), Value::String(analysis_id.clone()));
    metadata.insert("snapshot_id".into(), Value::String(String::new()));
    metadata.insert("collection_name".into(), Value::String(String::new()));
    metadata.insert(
        "response_hash".into(),
        response_hash.map(Value::String).unwrap_or(Value::Null),
    );
    metadata.insert(
        "report_hash".into(),
        retained.get("report_hash").cloned().unwrap_or(Value::Null),
    );
    metadata.insert(
        "final_response_hash".into(),
        retained
            .get("final_response_hash")
            .cloned()
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "analysis_status".into(),
        validation
            .as_ref()
            .and_then(|value| value.get("analysis_status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "reporting_status".into(),
        validation
            .as_ref()
            .and_then(|value| value.get("reporting_status"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "empty_directories".into(),
        Value::Array(vec![Value::String("researchers".into())]),
    );
    metadata.insert(
        "retained_paths".into(),
        Value::Array(
            ANALYSIS_MARKDOWN
                .iter()
                .map(|name| Value::String((*name).to_string()))
                .collect(),
        ),
    );
    let delivery = if validation.is_some() {
        "verified".to_string()
    } else if options.delivery_unverified {
        "unverified".to_string()
    } else {
        String::new()
    };
    metadata.insert("delivery".into(), Value::String(delivery));
    insert_orphan_counts(&mut metadata, &orphans, options.dry_run);
    let mut result = execute_cleanup(
        &analysis,
        &receipt_path,
        &Value::Object(metadata),
        &targets,
        options.dry_run,
        &now_utc,
    )?;
    if let Some(validation) = &validation {
        if result.get("cleanup_status").and_then(Value::as_str) == Some("complete") {
            if let Some(map) = result.as_object_mut() {
                for key in ["ticker", "horizon", "asof_timestamp", "prob_up"] {
                    map.insert(
                        key.into(),
                        validation.get(key).cloned().unwrap_or(Value::Null),
                    );
                }
            }
        }
    }
    Ok(result)
}

/// Validate and clean only JSON created by an isolated pipeline run.
pub fn cleanup_owned_run(
    run_dir: &Path,
    analysis_dir: Option<&Path>,
    response_file: Option<&Path>,
    options: &CleanupOptions,
    clock: &dyn Clock,
    gate: &dyn CleanupGate,
) -> CleanupResult<Value> {
    let root = checked_directory(run_dir, "run directory")?;
    let lock_path = root
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
    let _lock = locks::acquire_cleanup_lock(&lock_path)?;
    let now_utc = now_text(clock);
    let pipeline_marker = root.join(PIPELINE_LOCK);
    let mut stale_pipeline_marker = false;
    let mut pipeline_pid: Option<i64> = None;
    if is_link(&pipeline_marker) {
        return fail("pipeline lock marker was replaced by a link");
    }
    if pipeline_marker.exists() {
        let marker_text = std::fs::read_to_string(&pipeline_marker)
            .map_err(|_| "pipeline lock marker is unreadable; cleanup is blocked".to_string())?;
        let trimmed = marker_text.trim();
        let pid = trimmed
            .strip_prefix("pid=")
            .and_then(|value| value.parse::<i64>().ok())
            .ok_or_else(|| "pipeline lock marker is unreadable; cleanup is blocked".to_string())?;
        pipeline_pid = Some(pid);
        if locks::pid_is_running(pid)? {
            return fail("pipeline is still active; cleanup is blocked");
        }
        if !options.force_incomplete {
            return fail(
                "interrupted pipeline requires --force-incomplete to clear its stale lock",
            );
        }
        stale_pipeline_marker = true;
    }
    let orphans = sweep_orphans(&root, options.dry_run)?;
    let ignored = orphan_ignore(&orphans);
    let manifest_path = root.join(RUN_MANIFEST);
    let receipt_path = root.join(CLEANUP_RECEIPT);
    let mut receipt: Option<Receipt> = None;
    let manifest: Value;
    if manifest_path.is_file() && !is_link(&manifest_path) {
        manifest = read_json_file(&manifest_path).map_err(|error| error.message().to_string())?;
        let identity_ok = manifest.get("manifest_version").and_then(Value::as_i64) == Some(1)
            && manifest.get("mode").and_then(Value::as_str) == Some("owned_pipeline")
            && manifest.get("run_id").and_then(Value::as_str)
                == Some(
                    root.file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .unwrap_or_default()
                        .leak(),
                );
        if !manifest.is_object() || !identity_ok {
            return fail("owned run manifest identity is invalid");
        }
    } else {
        if !receipt_path.is_file() {
            return fail(
                "owned run manifest is missing and no cleanup receipt can resume this run",
            );
        }
        let read = read_receipt(&receipt_path)?;
        if read.get("mode").and_then(Value::as_str) != Some("run")
            || read.get("run_id").and_then(Value::as_str)
                != Some(
                    root.file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .unwrap_or_default()
                        .as_str(),
                )
        {
            return fail("cleanup receipt does not identify this run");
        }
        let mut resumed = Map::new();
        resumed.insert("manifest_version".into(), Value::from(1));
        resumed.insert("mode".into(), Value::String("owned_pipeline".into()));
        resumed.insert(
            "run_id".into(),
            Value::String(
                root.file_name()
                    .map(|name| name.to_string_lossy().to_string())
                    .unwrap_or_default(),
            ),
        );
        resumed.insert("state".into(), Value::String("cleanup_in_progress".into()));
        let non_empty = |key: &str| {
            read.get(key)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(|value| Value::String(value.to_string()))
                .unwrap_or(Value::Null)
        };
        resumed.insert("analysis_id".into(), non_empty("analysis_id"));
        resumed.insert("snapshot_id".into(), non_empty("snapshot_id"));
        resumed.insert("collection_name".into(), non_empty("collection_name"));
        manifest = Value::Object(resumed);
        receipt = Some(read);
    }
    let analysis_id = manifest
        .get("analysis_id")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let snapshot_id = manifest
        .get("snapshot_id")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let collection_name = manifest
        .get("collection_name")
        .and_then(Value::as_str)
        .map(ToString::to_string);
    for (value, label) in [(&analysis_id, "analysis_id"), (&snapshot_id, "snapshot_id")] {
        if let Some(value) = value {
            validate_path_component(value, label).map_err(|error| error.message().to_string())?;
        }
    }
    if let Some(analysis_dir) = analysis_dir {
        let Some(analysis_id) = &analysis_id else {
            return fail("run has no registered analysis directory");
        };
        let provided = checked_directory(analysis_dir, "analysis directory")?;
        let research_root = root.join("research");
        if is_link(&research_root) {
            return fail("research directory is linked");
        }
        let expected = research_root.join(analysis_id);
        let expected = expected
            .canonicalize()
            .map_err(|_| "analysis directory does not match this owned run".to_string())?;
        if provided != expected {
            return fail("analysis directory does not match this owned run");
        }
    }
    if receipt.is_none() && receipt_path.exists() {
        receipt = Some(read_receipt(&receipt_path)?);
    }
    if let Some(receipt) = &receipt {
        let run_name = root
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        if receipt.get("mode").and_then(Value::as_str) != Some("run")
            || receipt.get("run_id").and_then(Value::as_str) != Some(run_name.as_str())
        {
            return fail("cleanup receipt belongs to a different run");
        }
        for (key, value) in [
            ("analysis_id", analysis_id.as_deref()),
            ("snapshot_id", snapshot_id.as_deref()),
            ("collection_name", collection_name.as_deref()),
        ] {
            let recorded = receipt.get(key).and_then(Value::as_str).unwrap_or("");
            if recorded != value.unwrap_or("") {
                return fail("cleanup receipt does not match the registered run ownership");
            }
        }
        if receipt.get("state").and_then(Value::as_str) == Some("complete") {
            let (files, directories) = tree_entries(&root, &ignored)?;
            let mut allowed_files = allowed_markdown(analysis_id.as_deref(), true);
            allowed_files.insert(CLEANUP_RECEIPT.into());
            // Snapshot/collection evidence intentionally survives the first cleanup.
            // Admit only registered ownership paths on repeated calls as well.
            allowed_files.extend(
                expected_full_targets(&manifest)?
                    .into_iter()
                    .filter(|name| {
                        RETAINED_PREFIXES
                            .iter()
                            .any(|prefix| name.starts_with(prefix))
                    }),
            );
            if let Some(snapshot_id) = &snapshot_id {
                allowed_files.extend(optional_snapshot_targets(snapshot_id)?);
            }
            if stale_pipeline_marker {
                allowed_files.insert(PIPELINE_LOCK.into());
            }
            let allowed_dirs =
                allowed_directories(analysis_id.as_deref(), snapshot_id.as_deref(), true);
            let unknown_files = files.difference(&allowed_files).next().is_some();
            let unknown_dirs = directories.difference(&allowed_dirs).next().is_some();
            if unknown_files || unknown_dirs {
                return fail("JSON or unexpected files appeared after cleanup");
            }
            if let Some(analysis_id) = &analysis_id {
                verify_retained_hashes(&root.join("research").join(analysis_id), receipt)?;
            }
            if let Some(response_file) = response_file {
                if let Some(expected_hash) = receipt.get("response_sha256").and_then(Value::as_str)
                {
                    if !expected_hash.is_empty() && sha256_file(response_file)? != expected_hash {
                        return fail(
                            "prepared response differs from the response previously validated",
                        );
                    }
                }
            }
            if stale_pipeline_marker && !options.dry_run {
                clear_stale_pipeline_marker(&pipeline_marker, pipeline_pid)?;
            }
            let removed_count = receipt
                .get("removed_file_count")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(0);
            let removed_bytes = receipt
                .get("removed_json_bytes")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<i64>().ok())
                .unwrap_or(0);
            let mut result = Map::new();
            result.insert(
                "cleanup_status".into(),
                Value::String("already_cleaned".into()),
            );
            result.insert("mode".into(), Value::String("run".into()));
            result.insert(
                "run_id".into(),
                Value::String(
                    root.file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .unwrap_or_default(),
                ),
            );
            result.insert(
                "analysis_id".into(),
                analysis_id
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
            result.insert(
                "analysis_status".into(),
                receipt
                    .get("analysis_status")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(|value| Value::String(value.to_string()))
                    .unwrap_or(Value::Null),
            );
            result.insert(
                "reporting_status".into(),
                receipt
                    .get("reporting_status")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(|value| Value::String(value.to_string()))
                    .unwrap_or(Value::Null),
            );
            result.insert("json_file_count".into(), Value::from(removed_count));
            result.insert("json_bytes".into(), Value::from(removed_bytes));
            let (detected, removed) = orphan_result_counts(&orphans, options.dry_run);
            result.insert("orphans_removed".into(), Value::from(removed));
            result.insert("orphans_detected".into(), Value::from(detected));
            let retained: Vec<Value> = allowed_markdown(analysis_id.as_deref(), true)
                .iter()
                .filter(|name| root.join(name).is_file())
                .map(|name| Value::String(root.join(name).to_string_lossy().to_string()))
                .collect();
            result.insert("retained_files".into(), Value::Array(retained));
            result.insert("errors".into(), Value::Array(Vec::new()));
            result.insert(
                "receipt_file".into(),
                Value::String(receipt_path.to_string_lossy().to_string()),
            );
            return Ok(Value::Object(result));
        }
    }

    let state = manifest
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let resuming = receipt.is_some();
    let mut validation: Option<Value> = None;
    let force_mode = options.force_incomplete && matches!(state.as_str(), "failed" | "running");
    if !resuming && state != "awaiting_cleanup" && !force_mode {
        return fail("run is not complete; failed/incomplete runs require --force-incomplete");
    }
    if !resuming
        && !force_mode
        && response_file.is_none()
        && !options.dry_run
        && !options.delivery_unverified
    {
        return fail(
            "response_file is required before cleanup unless delivery is explicitly unverified",
        );
    }
    let mut response_hash: Option<String> = None;
    if !resuming && !force_mode {
        if let Some(response_file) = response_file {
            let (Some(analysis_id), Some(snapshot_id)) = (&analysis_id, &snapshot_id) else {
                return fail("completed run has no registered analysis/snapshot identity");
            };
            let analysis_dir = root.join("research").join(analysis_id);
            let value = gate.validate_final_response(&analysis_dir, response_file)?;
            if value.get("analysis_id").and_then(Value::as_str) != Some(analysis_id.as_str())
                || value.get("snapshot_id").and_then(Value::as_str) != Some(snapshot_id.as_str())
            {
                return fail("prepared response identity does not match the owned run");
            }
            response_hash = Some(sha256_file(response_file)?);
            validation = Some(value);
        }
    } else if let Some(receipt) = &receipt {
        response_hash = receipt
            .get("response_sha256")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(ToString::to_string);
        if let Some(response_file) = response_file {
            if let Some(expected_hash) = &response_hash {
                if sha256_file(response_file)? != *expected_hash {
                    return fail(
                        "prepared response differs from the response previously validated",
                    );
                }
            }
        }
    }

    if !resuming && !force_mode {
        let Some(analysis_id) = &analysis_id else {
            return fail("completed run has no registered analysis directory");
        };
        verify_retained_artifacts(&root.join("research").join(analysis_id), gate)?;
    }

    let mut expected = expected_full_targets(&manifest)?;
    if let Some(snapshot_id) = &snapshot_id {
        for name in optional_snapshot_targets(snapshot_id)? {
            // A resumed receipt must remain valid after a target was deleted.
            // Newly appearing files still fail the registered inventory check.
            if receipt.is_some() || root.join(&name).exists() {
                expected.insert(name);
            }
        }
    }
    let allowed_dirs = allowed_directories(analysis_id.as_deref(), snapshot_id.as_deref(), true);
    let allow_markdown = allowed_markdown(analysis_id.as_deref(), true);
    let targets: Vec<(String, String, i64)> = if let Some(receipt) = &receipt {
        for (name, _, _) in &receipt.targets {
            if !expected.contains(name) {
                return fail("cleanup receipt contains an unowned target");
            }
        }
        let (files, directories) = tree_entries(&root, &ignored)?;
        let mut allowed_run_files = allow_markdown.clone();
        allowed_run_files.insert(CLEANUP_RECEIPT.into());
        if stale_pipeline_marker {
            allowed_run_files.insert(PIPELINE_LOCK.into());
        }
        if files
            .difference(&expected)
            .any(|name| !allowed_run_files.contains(name))
        {
            return fail("unknown files prevent resumed cleanup");
        }
        if directories.difference(&allowed_dirs).next().is_some() {
            return fail("unknown directories prevent resumed cleanup");
        }
        let registered: BTreeSet<String> = receipt
            .targets
            .iter()
            .map(|(name, _, _)| name.clone())
            .collect();
        let retained: BTreeSet<String> = expected
            .iter()
            .filter(|name| {
                RETAINED_PREFIXES
                    .iter()
                    .any(|prefix| name.starts_with(prefix))
            })
            .cloned()
            .collect();
        let appeared: Vec<String> = files
            .intersection(&expected)
            .filter(|name| !registered.contains(*name) && !retained.contains(*name))
            .cloned()
            .collect();
        if !appeared.is_empty() {
            return fail("unregistered run JSON appeared after cleanup inventory");
        }
        if let Some(analysis_id) = &analysis_id {
            verify_retained_hashes(&root.join("research").join(analysis_id), receipt)?;
        }
        receipt.targets.clone()
    } else {
        let mut initial_allowed = allow_markdown.clone();
        if stale_pipeline_marker {
            initial_allowed.insert(PIPELINE_LOCK.into());
        }
        let actual = validate_tree(
            &root,
            &expected,
            &allowed_dirs,
            &initial_allowed,
            force_mode,
            &ignored,
        )?;
        if !force_mode {
            let Some(analysis_id) = &analysis_id else {
                return fail("completed run has no registered analysis directory");
            };
            retained_hashes(&root.join("research").join(analysis_id), true)?;
        }
        let mut collected: Vec<(String, String, i64)> = Vec::new();
        for name in expected.intersection(&actual) {
            if RETAINED_PREFIXES
                .iter()
                .any(|prefix| name.starts_with(prefix))
            {
                continue;
            }
            let path = root.join(name);
            let size = std::fs::metadata(&path)
                .map(|metadata| metadata.len() as i64)
                .unwrap_or(0);
            collected.push((name.clone(), sha256_file(&path)?, size));
        }
        collected.sort_by(|left, right| left.0.cmp(&right.0));
        collected
    };

    let mut retained: Map<String, Value> = Map::new();
    let mut retained_paths: Vec<Value> = Vec::new();
    if let Some(analysis_id) = &analysis_id {
        let analysis_dir = root.join("research").join(analysis_id);
        retained = retained_hashes(&analysis_dir, !force_mode)?;
        retained_paths = ANALYSIS_MARKDOWN
            .iter()
            .filter(|name| analysis_dir.join(name).is_file())
            .map(|name| Value::String(format!("research/{analysis_id}/{name}")))
            .collect();
    }
    let mut empty_directories: Vec<String> = allowed_dirs
        .iter()
        .filter(|name| !matches!(name.as_str(), "snapshots" | "collections"))
        .filter(|name| {
            !RETAINED_PREFIXES
                .iter()
                .any(|prefix| name.starts_with(prefix))
        })
        .cloned()
        .collect();
    if let Some(analysis_id) = &analysis_id {
        empty_directories
            .retain(|name| name != "research" && name != &format!("research/{analysis_id}"));
    }
    let delivery = if validation.is_some() {
        "verified".to_string()
    } else if options.delivery_unverified {
        "unverified".to_string()
    } else {
        receipt
            .as_ref()
            .and_then(|receipt| receipt.get("delivery"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let mut metadata = Map::new();
    metadata.insert("mode".into(), Value::String("run".into()));
    metadata.insert(
        "run_id".into(),
        Value::String(
            root.file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default(),
        ),
    );
    metadata.insert(
        "analysis_id".into(),
        Value::String(analysis_id.clone().unwrap_or_default()),
    );
    metadata.insert(
        "snapshot_id".into(),
        Value::String(snapshot_id.clone().unwrap_or_default()),
    );
    metadata.insert(
        "collection_name".into(),
        Value::String(collection_name.clone().unwrap_or_default()),
    );
    metadata.insert(
        "response_hash".into(),
        Value::String(response_hash.unwrap_or_default()),
    );
    metadata.insert(
        "report_hash".into(),
        retained.get("report_hash").cloned().unwrap_or(Value::Null),
    );
    metadata.insert(
        "final_response_hash".into(),
        retained
            .get("final_response_hash")
            .cloned()
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "analysis_status".into(),
        validation
            .as_ref()
            .and_then(|value| value.get("analysis_status"))
            .cloned()
            .or_else(|| {
                receipt
                    .as_ref()
                    .and_then(|receipt| receipt.get("analysis_status"))
                    .cloned()
            })
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "reporting_status".into(),
        validation
            .as_ref()
            .and_then(|value| value.get("reporting_status"))
            .cloned()
            .or_else(|| {
                receipt
                    .as_ref()
                    .and_then(|receipt| receipt.get("reporting_status"))
                    .cloned()
            })
            .unwrap_or(Value::Null),
    );
    metadata.insert(
        "empty_directories".into(),
        Value::Array(empty_directories.into_iter().map(Value::String).collect()),
    );
    metadata.insert("retained_paths".into(), Value::Array(retained_paths));
    metadata.insert("delivery".into(), Value::String(delivery));
    insert_orphan_counts(&mut metadata, &orphans, options.dry_run);
    if let Some(receipt) = &receipt {
        let removed_file_count = receipt
            .get("removed_file_count")
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(0);
        let removed_bytes = receipt
            .get("removed_json_bytes")
            .and_then(Value::as_str)
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(0);
        metadata.insert("removed_file_count".into(), Value::from(removed_file_count));
        metadata.insert("removed_bytes".into(), Value::from(removed_bytes));
    }
    if stale_pipeline_marker && !options.dry_run {
        clear_stale_pipeline_marker(&pipeline_marker, pipeline_pid)?;
    }
    let mut result = execute_cleanup(
        &root,
        &receipt_path,
        &Value::Object(metadata),
        &targets,
        options.dry_run,
        &now_utc,
    )?;
    if let Some(validation) = &validation {
        if result.get("cleanup_status").and_then(Value::as_str) == Some("complete") {
            if let Some(map) = result.as_object_mut() {
                for key in ["ticker", "horizon", "asof_timestamp", "prob_up"] {
                    map.insert(
                        key.into(),
                        validation.get(key).cloned().unwrap_or(Value::Null),
                    );
                }
            }
        }
    }
    Ok(result)
}

fn clear_stale_pipeline_marker(path: &Path, pid: Option<i64>) -> CleanupResult<()> {
    let Some(pid) = pid else {
        return fail("pipeline lock marker changed before cleanup");
    };
    let text = std::fs::read_to_string(path).unwrap_or_default();
    if is_link(path) || !path.is_file() || text.trim() != format!("pid={pid}") {
        return fail("pipeline lock marker changed before cleanup");
    }
    if locks::pid_is_running(pid)? {
        return fail("pipeline lock marker changed before cleanup");
    }
    std::fs::remove_file(path).map_err(|error| error.to_string())
}
