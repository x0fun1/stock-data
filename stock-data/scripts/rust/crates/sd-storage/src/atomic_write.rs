//! Atomic file replacement in the destination directory.
//!
//! Same-directory scratch file + `os.replace`/`MoveFileEx` semantics: a reader sees
//! either the previous complete file or the new complete file, never a partial one.
//! The scratch name matches the legacy cleanup pattern
//! `.<target>.<12 lowercase hex>.tmp`, so a hard kill leaves only provably owned
//! scratch files that `cleanup`/`gc` may sweep (R10).

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use sd_core::error::{SdError, SdResult};

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// 12 lowercase hex characters derived from time, process and a counter.
fn scratch_token() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let counter = COUNTER.fetch_add(1, Ordering::Relaxed);
    let mixed = nanos ^ ((std::process::id() as u128) << 64) ^ ((counter as u128) << 32);
    let digest = crate::legacy_json::sha256_hex(&mixed.to_le_bytes());
    digest[..12].to_string()
}

/// Scratch path used for the next atomic replace of `path`.
pub fn scratch_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let scratch = format!(".{name}.{}.tmp", scratch_token());
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.join(scratch),
        _ => PathBuf::from(scratch),
    }
}

/// Write `content` to `path` atomically (UTF-8, `\n` line endings preserved).
pub fn atomic_write_text(path: &Path, content: &str) -> SdResult<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(SdError::from)?;
    }
    let temporary = scratch_path(path);
    let result = (|| -> SdResult<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(SdError::from)?;
        file.write_all(content.as_bytes()).map_err(SdError::from)?;
        file.flush().map_err(SdError::from)?;
        drop(file);
        std::fs::rename(&temporary, path).map_err(SdError::from)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

/// Bytes-level variant used by artifact writers.
pub fn atomic_write_bytes(path: &Path, content: &[u8]) -> SdResult<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(SdError::from)?;
    }
    let temporary = scratch_path(path);
    let result = (|| -> SdResult<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(SdError::from)?;
        file.write_all(content).map_err(SdError::from)?;
        file.flush().map_err(SdError::from)?;
        drop(file);
        std::fs::rename(&temporary, path).map_err(SdError::from)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

/// True when `name` is the legacy atomic-write scratch file pattern:
/// `.` + `[A-Za-z0-9]` + `[A-Za-z0-9._-]*` + `.` + 12 lowercase hex + `.tmp`.
pub fn is_orphan_temp_file(name: &str) -> bool {
    let Some(rest) = name.strip_prefix('.') else {
        return false;
    };
    if !rest.starts_with(|character: char| character.is_ascii_alphanumeric()) {
        return false;
    }
    if !rest.ends_with(".tmp") {
        return false;
    }
    let trimmed = &rest[..rest.len() - 4];
    let Some((prefix, token)) = trimmed.rsplit_once('.') else {
        return false;
    };
    !prefix.is_empty()
        && prefix.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
        && token.len() == 12
        && token
            .chars()
            .all(|character| matches!(character, '0'..='9' | 'a'..='f'))
}

/// True when `name` is the legacy snapshot scratch directory pattern:
/// `.` + `[A-Za-z0-9]` + `[A-Za-z0-9._-]*` + `.` + 8 chars of `[a-z0-9_]`.
pub fn is_orphan_temp_dir(name: &str) -> bool {
    let Some(rest) = name.strip_prefix('.') else {
        return false;
    };
    if !rest.starts_with(|character: char| character.is_ascii_alphanumeric()) {
        return false;
    }
    let Some((prefix, token)) = rest.rsplit_once('.') else {
        return false;
    };
    !prefix.is_empty()
        && prefix.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
        && token.len() == 8
        && token.chars().all(|character| {
            character.is_ascii_lowercase() || character.is_ascii_digit() || character == '_'
        })
}
