//! The four named legacy JSON profiles and their digests.
//!
//! The Python runtime does not use one serializer. Each profile below is a
//! separate on-disk contract, and hashes computed with one profile are never
//! interchangeable with another:
//!
//! | profile | Python source | shape |
//! |---|---|---|
//! | snapshot/News canonical | `jsonio.canonical_bytes` | sorted keys, `(",", ":")`, UTF-8 literal, NaN rejected |
//! | readable artifact | `jsonio.json_payload` | `ensure_ascii=False`, indent 2, trailing `\n` |
//! | stage digest | `orchestrator._canonical_digest` | sorted keys, compact, `ensure_ascii=True`, `default=str` |
//! | ledger event | `ledger._canonical_sha256` | sorted keys, compact, `ensure_ascii=False`, `default=str`, SHA-256 |
//!
//! Snapshot manifests, frozen stage results and cleanup receipts hash the bytes, so
//! changing any of these rules silently invalidates historical artifacts.

use serde_json::Value;
use sha2::{Digest, Sha256};

use sd_core::error::SdResult;
use sd_core::json_text::{canonical_text, canonical_text_with_ascii, python_readable};
use sd_core::numbers::ensure_finite_payload;
use sd_core::versions::{
    JSON_PROFILE_LEDGER, JSON_PROFILE_READABLE, JSON_PROFILE_SNAPSHOT, JSON_PROFILE_STAGE,
};

/// Lowercase hex SHA-256 of raw bytes.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(bytes);
    format!("{:x}", digest.finalize())
}

/// `jsonio.canonical_bytes`: snapshot/News canonical bytes.
pub fn canonical_bytes(value: &Value) -> SdResult<Vec<u8>> {
    ensure_finite_payload(value)?;
    Ok(canonical_text(value).into_bytes())
}

/// Same as [`canonical_bytes`] but returns the hex digest the manifests store.
pub fn canonical_sha256(value: &Value) -> SdResult<String> {
    Ok(sha256_hex(&canonical_bytes(value)?))
}

/// `jsonio.json_payload`: readable on-disk form with a trailing newline.
pub fn readable_payload(value: &Value) -> SdResult<String> {
    ensure_finite_payload(value)?;
    Ok(python_readable(value))
}

/// `orchestrator._canonical_digest`: stage payload digest (`ensure_ascii=True`).
pub fn stage_digest(value: &Value) -> SdResult<String> {
    ensure_finite_payload(value)?;
    Ok(sha256_hex(
        canonical_text_with_ascii(value, true).as_bytes(),
    ))
}

/// `ledger._canonical_sha256`: ledger event digest (`ensure_ascii=False`).
pub fn ledger_event_sha256(value: &Value) -> SdResult<String> {
    ensure_finite_payload(value)?;
    Ok(sha256_hex(canonical_text(value).as_bytes()))
}

/// One ledger line exactly as `ledger._append` writes it: sorted compact keys,
/// literal UTF-8, plus a trailing newline.
pub fn ledger_event_line(value: &Value) -> SdResult<String> {
    ensure_finite_payload(value)?;
    Ok(format!("{}\n", canonical_text(value)))
}

/// Metadata naming the profile a digest was produced with.
pub fn profile_name(profile: &str) -> &'static str {
    match profile {
        JSON_PROFILE_SNAPSHOT => JSON_PROFILE_SNAPSHOT,
        JSON_PROFILE_READABLE => JSON_PROFILE_READABLE,
        JSON_PROFILE_STAGE => JSON_PROFILE_STAGE,
        JSON_PROFILE_LEDGER => JSON_PROFILE_LEDGER,
        _ => "unknown",
    }
}
