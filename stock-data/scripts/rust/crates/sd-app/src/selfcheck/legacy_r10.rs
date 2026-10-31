//! Migrated R10 checks: orphan scratch sweep and retention/GC.
//!
//! Both checks run entirely inside a temporary directory; nothing in the repository is
//! touched. Time is injected through `FixedClock`.

use std::path::{Path, PathBuf};
use std::time::{Duration as StdDuration, SystemTime};

use chrono::{DateTime, Utc};
use serde_json::json;

use sd_core::clock::FixedClock;
use sd_storage::cleanup::{receipt_data, sweep_orphans, UnwiredGate, CLEANUP_RECEIPT};
use sd_storage::retention::{gc_runtime, GcOptions};

use crate::selfcheck::registry::CheckFn;

fn failure(message: impl Into<String>) -> String {
    message.into()
}

fn scratch_dir(label: &str) -> PathBuf {
    let directory =
        std::env::temp_dir().join(format!("sd-selfcheck-r10-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).expect("scratch dir");
    directory
}

fn write(path: &Path, text: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("parent dir");
    }
    std::fs::write(path, text).expect("write file");
}

fn touch_days_ago(path: &Path, days: i64) {
    let moment = DateTime::parse_from_rfc3339("2026-06-01T00:00:00Z")
        .expect("moment")
        .with_timezone(&Utc);
    let moment = SystemTime::UNIX_EPOCH + StdDuration::from_secs(moment.timestamp().max(0) as u64);
    let stamp = moment - StdDuration::from_secs((days.max(0) * 86_400) as u64);
    let file = std::fs::OpenOptions::new()
        .write(true)
        .open(path)
        .expect("open for mtime");
    file.set_times(std::fs::FileTimes::new().set_modified(stamp))
        .expect("set mtime");
}

/// F18: the orphan sweep removes only scratch residue and never touches real artifacts.
pub fn legacy_f18() -> Result<(), String> {
    let work = scratch_dir("f18");
    let root = work.join("run");
    write(&root.join("run_manifest.json"), "{}");
    write(&root.join(".pipeline.lock"), "pid=1\n");
    write(&root.join("research/a_1/report.md"), "md");
    write(&root.join("notes.txt"), "keep");
    let orphan_file = root.join("snapshots/.market.json.0123456789ab.tmp");
    write(&orphan_file, "x");
    let scratch = root.join("snapshots/.snap_20260101T000000000000Z_deadbeef.abc123xy");
    write(&scratch.join("manifest.json"), "{}");
    write(&scratch.join("raw/market.json"), "{}");

    let preview = sweep_orphans(&root, true).expect("dry run");
    if preview["removed"].as_bool() != Some(false) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("a dry run must report removed=false"));
    }
    let files: Vec<String> = preview["orphan_files"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    let directories: Vec<String> = preview["orphan_directories"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    if files != vec!["snapshots/.market.json.0123456789ab.tmp".to_string()] {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("unexpected orphan files: {files:?}")));
    }
    if directories != vec!["snapshots/.snap_20260101T000000000000Z_deadbeef.abc123xy".to_string()] {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "unexpected orphan directories: {directories:?}"
        )));
    }
    if !scratch.exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("a dry run must not delete"));
    }

    let report = sweep_orphans(&root, false).expect("real sweep");
    if report["removed"].as_bool() != Some(true) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the real sweep must report removed=true"));
    }
    if orphan_file.exists() || scratch.exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("orphan scratch residue survived"));
    }
    for kept in ["run_manifest.json", ".pipeline.lock", "notes.txt"] {
        if !root.join(kept).exists() {
            let _ = std::fs::remove_dir_all(&work);
            return Err(failure(format!("{kept} was removed by the orphan sweep")));
        }
    }
    if !root.join("research/a_1/report.md").exists() || !root.join("snapshots").is_dir() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("real artifacts were removed by the orphan sweep"));
    }
    let _ = std::fs::remove_dir_all(&work);
    Ok(())
}

fn make_run(
    work: &Path,
    moment: DateTime<Utc>,
    run_id: &str,
    age_days: i64,
    receipt: bool,
    extra: Option<&str>,
    state: &str,
) -> PathBuf {
    let root = work.join("runs").join(run_id);
    write(&root.join("research/a_1/report.md"), "md");
    write(&root.join("research/a_1/final_response.md"), "fr");
    let created = moment - chrono::Duration::days(age_days);
    write(
        &root.join("run_manifest.json"),
        &serde_json::to_string(&json!({
            "manifest_version": 1,
            "mode": "owned_pipeline",
            "run_id": run_id,
            "state": state,
            "created_at_utc": created.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
            "collection_name": null,
            "snapshot_id": null,
            "analysis_id": "a_1",
        }))
        .expect("manifest"),
    );
    if receipt {
        let text = receipt_data(
            "run",
            run_id,
            Some("a_1"),
            None,
            None,
            "complete",
            None,
            None,
            None,
            None,
            None,
            &[],
            0,
            0,
            &[],
            "verified",
            &moment.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        );
        write(&root.join(CLEANUP_RECEIPT), &text);
    }
    if let Some(extra) = extra {
        write(&root.join(extra), "x");
    }
    root
}

/// F19: retention expires only complete, old, clean runs; cache ages out; GC throttles.
pub fn legacy_f19() -> Result<(), String> {
    let work = scratch_dir("f19");
    let moment = DateTime::parse_from_rfc3339("2026-06-01T00:00:00Z")
        .expect("moment")
        .with_timezone(&Utc);
    let clock = FixedClock::new(moment);
    std::fs::create_dir_all(work.join("runs")).expect("runs root");
    let expired = make_run(
        &work,
        moment,
        "run_20260101T000000000000Z_aaaaaaaa",
        40,
        true,
        None,
        "cleanup_in_progress",
    );
    let fresh = make_run(
        &work,
        moment,
        "run_20260530T000000000000Z_bbbbbbbb",
        2,
        true,
        None,
        "cleanup_in_progress",
    );
    let dirty = make_run(
        &work,
        moment,
        "run_20260101T000000000000Z_cccccccc",
        40,
        true,
        Some("notes.txt"),
        "cleanup_in_progress",
    );
    let unfinished = make_run(
        &work,
        moment,
        "run_20260101T000000000000Z_dddddddd",
        40,
        false,
        None,
        "cleanup_in_progress",
    );
    let pending = make_run(
        &work,
        moment,
        "run_20260101T000000000000Z_eeeeeeee",
        40,
        false,
        None,
        "awaiting_cleanup",
    );
    let cache = work.join("yfinance-cache/tkr");
    write(&cache.join("stale.json"), "x");
    touch_days_ago(&cache.join("stale.json"), 30);
    write(&cache.join("fresh.json"), "x");

    let options = |dry_run: bool, throttle_hours: Option<f64>| GcOptions {
        retain_days: 30,
        cache_days: 7,
        pending_days: 1,
        cache_dir: Some(work.join("yfinance-cache")),
        dry_run,
        throttle_hours,
    };
    let preview = gc_runtime(&work, &options(true, None), &clock, &UnwiredGate).expect("preview");
    if !expired.exists() || !dirty.exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("a dry run must not delete"));
    }
    let would: Vec<String> = preview["runs_would_delete"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    if !would
        .iter()
        .any(|name| name == "run_20260101T000000000000Z_aaaaaaaa")
    {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("dry-run candidates: {would:?}")));
    }

    let report = gc_runtime(&work, &options(false, None), &clock, &UnwiredGate).expect("gc");
    if report["status"].as_str() != Some("success") {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("gc status: {}", report["status"])));
    }
    let deleted: Vec<String> = report["runs_deleted"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    if deleted != vec!["run_20260101T000000000000Z_aaaaaaaa".to_string()] {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("unexpected deleted runs: {deleted:?}")));
    }
    if expired.exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("expired run directory survived"));
    }
    for (name, path) in [
        ("young run", &fresh),
        ("dirty run", &dirty),
        ("run without receipt", &unfinished),
        ("pending run", &pending),
    ] {
        if !path.exists() {
            let _ = std::fs::remove_dir_all(&work);
            return Err(failure(format!("{name} was expired")));
        }
    }
    let skipped: Vec<(String, String)> = report["runs_skipped"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|row| {
                    Some((
                        row["run_id"].as_str()?.to_string(),
                        row["reason"].as_str()?.to_string(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();
    let reason_for = |run: &str| {
        skipped
            .iter()
            .find(|(name, _)| name == run)
            .map(|(_, reason)| reason.clone())
            .unwrap_or_default()
    };
    if !reason_for("run_20260101T000000000000Z_cccccccc").contains("non-retained file") {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("dirty run reason: {skipped:?}")));
    }
    if reason_for("run_20260101T000000000000Z_dddddddd").is_empty() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "run without receipt not skipped: {skipped:?}"
        )));
    }
    if reason_for("run_20260101T000000000000Z_eeeeeeee").is_empty() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!("pending run not skipped: {skipped:?}")));
    }
    let lock = work
        .join("runs")
        .join(".run_20260101T000000000000Z_aaaaaaaa.cleanup.lock");
    if lock.exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("the cleanup lock survived"));
    }
    if cache.join("stale.json").exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("stale cache file survived"));
    }
    if !cache.join("fresh.json").exists() {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure("fresh cache file was pruned"));
    }

    let throttled =
        gc_runtime(&work, &options(false, Some(24.0)), &clock, &UnwiredGate).expect("throttled");
    if throttled["status"].as_str() != Some("throttled") {
        let _ = std::fs::remove_dir_all(&work);
        return Err(failure(format!(
            "expected throttle, got {}",
            throttled["status"]
        )));
    }
    let later = FixedClock::new(moment + chrono::Duration::hours(25));
    let unthrottled =
        gc_runtime(&work, &options(false, Some(24.0)), &later, &UnwiredGate).expect("unthrottled");
    let outcome = unthrottled["status"].as_str() == Some("success");
    let _ = std::fs::remove_dir_all(&work);
    if !outcome {
        return Err(failure(format!(
            "expected success after the throttle window, got {}",
            unthrottled["status"]
        )));
    }
    let _ = clock;
    Ok(())
}

/// Implemented R10 checks.
pub fn implementations() -> Vec<(&'static str, CheckFn)> {
    vec![
        ("F18", legacy_f18 as CheckFn),
        ("F19", legacy_f19 as CheckFn),
    ]
}
