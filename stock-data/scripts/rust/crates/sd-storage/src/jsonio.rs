//! JSON file I/O: BOM-tolerant reads, readable payloads, exclusive writes.
//!
//! The legacy CLI reads request/response files with `encoding="utf-8-sig"`, so a
//! UTF-8 BOM must be stripped before parsing. Writes are byte-identical to
//! [`legacy_json::readable_payload`] and, when exclusive, refuse to replace an
//! existing artifact.

use std::path::{Path, PathBuf};

use serde_json::Value;

use sd_core::error::{SdError, SdResult};

use crate::atomic_write::atomic_write_text;
use crate::legacy_json::readable_payload;

const BOM: &[u8] = b"\xef\xbb\xbf";

/// Strip a leading UTF-8 BOM (the `utf-8-sig` codec behaviour).
pub fn strip_bom(bytes: &[u8]) -> &[u8] {
    bytes.strip_prefix(BOM).unwrap_or(bytes)
}

/// Read a UTF-8 JSON file, tolerating a BOM.
pub fn read_json_file(path: &Path) -> SdResult<Value> {
    let bytes = std::fs::read(path).map_err(SdError::from)?;
    let text = String::from_utf8(strip_bom(&bytes).to_vec())
        .map_err(|error| SdError::validation(format!("file is not valid UTF-8: {error}")))?;
    serde_json::from_str(&text).map_err(|error| {
        SdError::validation(format!("invalid JSON in {}: {error}", path.display()))
    })
}

/// Write a readable JSON artifact. `exclusive` maps to Python's `open("x")`.
pub fn write_json(path: &Path, value: &Value, exclusive: bool) -> SdResult<PathBuf> {
    let payload = readable_payload(value)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(SdError::from)?;
    }
    if exclusive {
        use std::io::Write;
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(SdError::from)?;
        file.write_all(payload.as_bytes()).map_err(SdError::from)?;
        return Ok(path.to_path_buf());
    }
    atomic_write_text(path, &payload)?;
    Ok(path.to_path_buf())
}

/// Read a file as UTF-8 text, tolerating a BOM (used for Markdown/JSON inputs).
pub fn read_text_file(path: &Path) -> SdResult<String> {
    let bytes = std::fs::read(path).map_err(SdError::from)?;
    String::from_utf8(strip_bom(&bytes).to_vec())
        .map_err(|error| SdError::validation(format!("file is not valid UTF-8: {error}")))
}
