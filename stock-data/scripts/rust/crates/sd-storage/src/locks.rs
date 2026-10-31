//! Inter-process workspace locks (R10).
//!
//! Two lock shapes are used by the legacy runtime:
//!
//! * `.pipeline.lock` inside a run directory: an *ownership marker* written by the
//!   pipeline process (`pid=<pid>\n`). It is not an OS lock; cleanup only treats a
//!   run as abandoned after [`pid_is_running`] proves the recorded process is gone.
//! * `.<name>.cleanup.lock` next to the guarded directory: an OS-level exclusive
//!   lock held for the whole cleanup/GC critical section. The marker content is the
//!   holder PID; the file is never unlinked while held (unlinking would let another
//!   process lock a fresh inode while a waiter still holds the old one).
//!
//! Liveness is tri-valued: alive / dead / unknown. Diagnostics that cannot answer
//! (missing `tasklist`/`kill`, permission errors, unparsable output) are *unknown* and
//! block cleanup instead of being misread as dead. The current process is always alive.
//!
//! Windows and Unix lock behaviour differs in the legacy implementation
//! (`msvcrt.locking` vs `fcntl.flock`); Rust's `std::fs::File::try_lock` is the same
//! advisory lock on both platforms, so the two shapes keep equivalent semantics.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use crate::path_guard::is_link;

/// An acquired exclusive cleanup lock; unlocking happens on drop.
pub struct CleanupLock {
    file: File,
    #[allow(dead_code)]
    path: PathBuf,
}

impl std::fmt::Debug for CleanupLock {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CleanupLock")
            .field("path", &self.path)
            .finish()
    }
}

/// Acquire the exclusive cleanup lock at `path`.
///
/// Mirrors the legacy `_exclusive_lock`: the parent is created, a link is refused, an
/// existing marker must be a valid `pid=<positive int>\n` of at most 64 bytes, and a
/// concurrent holder is an error (never a wait).
pub fn acquire_cleanup_lock(path: &Path) -> Result<CleanupLock, String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    if is_link(path) {
        return Err("cleanup lock is a link; refusing to continue".into());
    }
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|error| error.to_string())?;
    if is_link(path) {
        return Err("cleanup lock is linked or not a regular file".into());
    }
    let metadata = file.metadata().map_err(|error| error.to_string())?;
    if metadata.is_dir() {
        return Err("cleanup lock is linked or not a regular file".into());
    }
    match file.try_lock() {
        Ok(()) => {}
        Err(_) => return Err("another cleanup is active or its lock is unavailable".into()),
    }
    if metadata.len() > 64 {
        let _ = file.unlock();
        return Err("cleanup lock file has unexpected contents".into());
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|error| error.to_string())?;
    let mut previous = String::new();
    file.read_to_string(&mut previous)
        .map_err(|error| error.to_string())?;
    if !previous.is_empty() && parse_marker(&previous).is_none() {
        let _ = file.unlock();
        return Err("cleanup lock file has unexpected contents".into());
    }
    file.set_len(0).map_err(|error| error.to_string())?;
    file.seek(SeekFrom::Start(0))
        .map_err(|error| error.to_string())?;
    writeln!(file, "pid={}", std::process::id()).map_err(|error| error.to_string())?;
    file.flush().map_err(|error| error.to_string())?;
    Ok(CleanupLock {
        file,
        path: path.to_path_buf(),
    })
}

impl Drop for CleanupLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

/// Parse a `pid=<n>` marker; returns the PID when the marker is well formed.
pub fn parse_marker(text: &str) -> Option<i64> {
    if !text.ends_with('\n') || !text.starts_with("pid=") || text.len() <= 5 {
        return None;
    }
    let pid: i64 = text[4..text.len() - 1].parse().ok()?;
    if pid <= 0 {
        None
    } else {
        Some(pid)
    }
}

/// Result of inspecting a recorded pipeline PID.
///
/// Unknown is a first-class outcome: a missing `tasklist`/`kill`, a permission or tool
/// failure must never be read as "dead" (that would let cleanup delete a live writer's
/// workspace). Callers that only need a boolean fail closed on Unknown.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcessStatus {
    /// The PID is this process or the OS reports it as running.
    Alive,
    /// The OS positively reports that no such process exists.
    Dead,
    /// The OS/tool could not answer (missing tool, permission error, unparsable output).
    Unknown(String),
}

/// Inspect a recorded PID with explicit alive/dead/unknown semantics.
pub fn pid_status(pid: i64) -> Result<ProcessStatus, String> {
    if pid <= 0 {
        return Err("pipeline lock marker contains an invalid process id".into());
    }
    // Never depend on an external process-listing tool for our own process.
    if pid == i64::from(std::process::id()) {
        return Ok(ProcessStatus::Alive);
    }
    #[cfg(windows)]
    {
        let output = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .output();
        match output {
            Ok(output) => Ok(classify_tasklist(
                pid,
                output.status.success(),
                &output.stdout,
            )),
            Err(error) => Ok(ProcessStatus::Unknown(format!(
                "tasklist could not be executed: {error}"
            ))),
        }
    }
    #[cfg(not(windows))]
    {
        // A visible /proc entry proves liveness. A missing entry does not prove
        // death: hidepid mounts can conceal another user's live process.
        let proc_root = std::path::Path::new("/proc");
        if proc_root.join(pid.to_string()).exists() {
            return Ok(ProcessStatus::Alive);
        }
        let output = std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .env("LC_ALL", "C")
            .output();
        match output {
            Ok(output) => Ok(classify_kill(output.status.success(), &output.stderr)),
            Err(error) => Ok(ProcessStatus::Unknown(format!(
                "kill -0 could not be executed: {error}"
            ))),
        }
    }
}

/// Parse `tasklist /NH /FO CSV` output into a liveness answer.
///
/// `tasklist` is a reporting tool, not an existence oracle: a *recognisable* successful
/// filter response is either a CSV row for the PID or an informational "no tasks match"
/// line (localized text, so it is recognised structurally: a non-empty unmatched line
/// with no CSV quoting and a `:` separator). Everything else is an output *shape* the
/// classifier does not understand and must be `Unknown` — never `Dead`, because a
/// false dead report would let cleanup delete a live writer's workspace.
///
/// Residual risk (documented, not fixable from this output): a successful filter that
/// returns no row because the OS cannot see the process (session/permission isolation)
/// is indistinguishable from a genuinely absent PID; the informational line is treated
/// as a positive dead report.
#[cfg_attr(not(windows), allow(dead_code))]
fn classify_tasklist(pid: i64, success: bool, stdout: &[u8]) -> ProcessStatus {
    if !success {
        return ProcessStatus::Unknown("tasklist exited unsuccessfully".into());
    }
    let text = String::from_utf8_lossy(stdout);
    let mut saw_row = false;
    let mut saw_info_line = false;
    let mut unparsable = false;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if !line.starts_with('"') {
            // A localized "no tasks match" message has no CSV quoting; accept it only
            // when it looks like a message (contains a separator) instead of a row.
            if line.contains(':') {
                saw_info_line = true;
            } else {
                unparsable = true;
            }
            continue;
        }
        saw_row = true;
        let mut fields = line.split(',');
        let _image = fields.next();
        let Some(pid_field) = fields.next() else {
            unparsable = true;
            continue;
        };
        let candidate = pid_field.trim().trim_matches('"').trim();
        if candidate.is_empty() || candidate.parse::<i64>().is_err() {
            unparsable = true;
            continue;
        }
        if candidate == pid.to_string() {
            return ProcessStatus::Alive;
        }
        // An exact PID filter that returns a valid row for a different PID is an
        // output shape this classifier does not understand.
        unparsable = true;
    }
    if unparsable {
        return ProcessStatus::Unknown(
            "tasklist output shape is not a recognisable PID filter response".into(),
        );
    }
    if saw_row {
        // Only escaped from the loop above on a match; rows present without a match are
        // handled by the `unparsable` branch.
        return ProcessStatus::Unknown("tasklist rows did not match the requested PID".into());
    }
    if saw_info_line {
        return ProcessStatus::Dead;
    }
    ProcessStatus::Unknown(
        "tasklist succeeded but returned no rows and no informational line".into(),
    )
}

/// Classify `kill -0` without treating EPERM as "dead".
#[cfg_attr(windows, allow(dead_code))]
fn classify_kill(success: bool, stderr: &[u8]) -> ProcessStatus {
    if success {
        return ProcessStatus::Alive;
    }
    let text = String::from_utf8_lossy(stderr).to_ascii_lowercase();
    if text.contains("no such process") || text.contains("does not exist") {
        ProcessStatus::Dead
    } else if text.contains("not permitted") || text.contains("permission") {
        // The process exists but belongs to another user; it is not provably dead.
        ProcessStatus::Unknown("kill -0 was denied by the OS; process state is unknown".into())
    } else {
        ProcessStatus::Unknown("kill -0 failed without a recognisable reason".into())
    }
}

/// True when the recorded process is still running.
///
/// Unknown liveness is returned as an error so every caller fails closed (cleanup/GC is
/// blocked rather than deleting a possibly live workspace).
pub fn pid_is_running(pid: i64) -> Result<bool, String> {
    match pid_status(pid)? {
        ProcessStatus::Alive => Ok(true),
        ProcessStatus::Dead => Ok(false),
        ProcessStatus::Unknown(reason) => Err(format!("process liveness is unknown: {reason}")),
    }
}

/// Write the pipeline ownership marker (`pid=<n>\n`) exclusively.
pub fn write_pipeline_marker(run_dir: &Path, pid: u32) -> Result<(), String> {
    let path = run_dir.join(crate::cleanup::PIPELINE_LOCK);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|error| error.to_string())?;
    writeln!(file, "pid={pid}").map_err(|error| error.to_string())
}

/// Read the PID recorded in the pipeline marker.
pub fn read_pipeline_pid(run_dir: &Path) -> Result<i64, String> {
    let path = run_dir.join(crate::cleanup::PIPELINE_LOCK);
    let text = std::fs::read_to_string(&path).map_err(|error| error.to_string())?;
    let trimmed = text.trim();
    if !trimmed.starts_with("pid=") {
        return Err("pipeline lock marker is unreadable; cleanup is blocked".into());
    }
    trimmed[4..]
        .parse::<i64>()
        .map_err(|_| "pipeline lock marker is unreadable; cleanup is blocked".to_string())
}
#[cfg(test)]
mod liveness_tests {
    use super::*;

    #[test]
    fn current_process_is_alive_without_any_tool() {
        let status = pid_status(i64::from(std::process::id())).expect("status");
        assert_eq!(status, ProcessStatus::Alive);
        assert!(pid_is_running(i64::from(std::process::id())).expect("running"));
    }

    #[test]
    fn implausible_pid_is_dead() {
        let status = pid_status(999_999).expect("status");
        assert_eq!(status, ProcessStatus::Dead);
        assert!(!pid_is_running(999_999).expect("dead"));
    }

    #[test]
    fn tasklist_csv_is_parsed_field_wise() {
        let alive = b"\"stock-data.exe\",\"4242\",\"Console\",\"1\",\"12,345 K\"\r\n";
        assert_eq!(classify_tasklist(4242, true, alive), ProcessStatus::Alive);
        // A valid row for a different PID under an exact filter is an unexpected shape.
        let other = b"\"stock-data.exe\",\"4243\",\"Console\",\"1\",\"12,345 K\"\r\n";
        assert!(matches!(
            classify_tasklist(4242, true, other),
            ProcessStatus::Unknown(_)
        ));
        // Localized "no tasks" message: the filter succeeded and found nothing.
        let localized = "정보: 실행 중인 작업이 없습니다.\r\n".as_bytes();
        assert_eq!(
            classify_tasklist(4242, true, localized),
            ProcessStatus::Dead
        );
        let english_info = b"INFO: No tasks are running which match the specified criteria.\r\n";
        assert_eq!(
            classify_tasklist(4242, true, english_info),
            ProcessStatus::Dead
        );
        // Silence is not positive evidence of death.
        assert!(matches!(
            classify_tasklist(4242, true, b""),
            ProcessStatus::Unknown(_)
        ));
    }

    #[test]
    fn tasklist_output_shape_anomalies_are_unknown_not_dead() {
        // Header-only output (no /NH) is a different shape than the requested one.
        let header = b"\"Image Name\",\"PID\",\"Session Name\",\"Session#\",\"Mem Usage\"\r\n";
        assert!(matches!(
            classify_tasklist(4242, true, header),
            ProcessStatus::Unknown(_)
        ));
        // Non-numeric PID field in a quoted row.
        let unparsable = b"\"image\",\"not-a-pid\"\r\n";
        assert!(matches!(
            classify_tasklist(4242, true, unparsable),
            ProcessStatus::Unknown(_)
        ));
        // Garbage that is neither CSV nor an informational message.
        let garbage = b"garbage success output\r\n";
        assert!(matches!(
            classify_tasklist(4242, true, garbage),
            ProcessStatus::Unknown(_)
        ));
        // A matching row with extra trailing fields is still recognised.
        let extra = b"\"System\",\"4\",\"Services\",\"0\",\"1,000 K\",\"extra\"\r\n";
        assert_eq!(classify_tasklist(4, true, extra), ProcessStatus::Alive);
        // Non-zero exit is unknown even when the bytes look like a match.
        assert!(matches!(
            classify_tasklist(4, false, b"\"System\",\"4\"\r\n"),
            ProcessStatus::Unknown(_)
        ));
    }

    #[test]
    fn failed_or_unparsable_diagnostics_are_unknown_not_dead() {
        assert!(matches!(
            classify_tasklist(4242, false, b"\"stock-data.exe\",\"4242\""),
            ProcessStatus::Unknown(_)
        ));
    }

    #[test]
    fn kill_eperm_and_unexpected_failures_are_unknown() {
        assert_eq!(classify_kill(true, b""), ProcessStatus::Alive);
        assert_eq!(
            classify_kill(false, b"kill: (4242) - No such process\n"),
            ProcessStatus::Dead
        );
        assert!(matches!(
            classify_kill(false, b"kill: (4242) - Operation not permitted\n"),
            ProcessStatus::Unknown(_)
        ));
        assert!(matches!(
            classify_kill(false, b"kill: something unexpected\n"),
            ProcessStatus::Unknown(_)
        ));
    }
}
