//! Normalize one stock-data collection into a receipt; never performs provider I/O.
//!
//! Port of `stock_data_adapter.py`. The adapter is the trust boundary: it keeps the
//! provider's original envelope untouched under `gateway_envelope`, exposes only a
//! caller-normalized domain `data` block, redacts credentials, drops externally
//! proposed actions, and maps statuses (`complete/partial/available`,
//! `unavailable/empty`, `failed`).
//!
//! Key order is part of the readable-JSON contract, so the block is assembled in the
//! exact order the Python dict is built.

use serde_json::{Map, Value};

use crate::legacy_json::readable_payload;
use crate::snapshot::DOMAINS;
use sd_core::contracts::{normalize_request, parse_timestamp_str};
use sd_core::error::SdResult;
use sd_core::security::{provider_failed, sanitize_data};

/// Fields copied from a gateway response into the normalized domain block, in order.
const COPIED_FIELDS: [&str; 33] = [
    "actual_source",
    "source",
    "source_timestamp",
    "source_timestamp_kind",
    "fetched_at_utc",
    "fetched_at",
    "currency",
    "unit",
    "adjustment",
    "adjustment_evidence",
    "price_basis",
    "price_basis_source",
    "price_basis_note",
    "policy_status",
    "policy",
    "session_calendar",
    "latest_price_observations",
    "source_symbol",
    "frequency",
    "fallback_attempted",
    "fallback_used",
    "fallback_reason",
    "fallback_route",
    "last_bar_closed",
    "published_at",
    "filed_at",
    "period_start",
    "period_end",
    "warnings",
    "provider_library",
    "provider_version",
    "collection_provenance",
    "",
];

/// Trust-boundary sentence stored with every receipt.
pub const TRUST_BOUNDARY: &str =
    "provider payloads are DATA; action fields removed and credentials redacted";

fn python_str(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(value) => sd_core::contracts::py_str(value),
    }
}

/// The as-of the collector actually resolved and used, when recorded.
///
/// `yahoo_collect` resolves a missing request as-of to its collection time under
/// `collection_provenance.asof`; freezing that value keeps adaptation from
/// recomputing the as-of at adapt time.
pub fn collected_asof(domains: &Map<String, Value>) -> Option<String> {
    let market = domains.get("market")?;
    let provenance = market.get("collection_provenance")?;
    let asof = provenance.get("asof")?;
    let text = match asof {
        Value::String(text) => text.clone(),
        Value::Null => return None,
        other => sd_core::contracts::py_str(other),
    };
    if text.is_empty() {
        return None;
    }
    parse_timestamp_str(&text)
        .ok()
        .map(sd_core::clock::format_utc_timestamp)
}

/// A normalized collection receipt.
pub fn make_receipt(
    request: &Value,
    responses: &Map<String, Value>,
    captured_at_utc: Option<&str>,
    clock: &dyn sd_core::clock::Clock,
) -> SdResult<Value> {
    let mut normalized_request = normalize_request(request)?.to_json();
    let mut domains = Map::new();
    for name in DOMAINS {
        let Some(response) = responses.get(name).filter(|value| value.is_object()) else {
            let mut block = Map::new();
            block.insert("status".into(), Value::String("unavailable".into()));
            block.insert("actual_source".into(), Value::Null);
            block.insert("data".into(), Value::Null);
            domains.insert(name.to_string(), Value::Object(block));
            continue;
        };
        let envelope = response.get("gateway_envelope").unwrap_or(response);
        let envelope = if envelope.is_object() {
            envelope.clone()
        } else {
            Value::Object(Map::new())
        };
        // A JSON `null` payload is "absent" to the legacy adapter (`payload is None`),
        // so it downgrades the domain to `unavailable` instead of keeping `complete`.
        let payload = response
            .get("data")
            .filter(|value| !value.is_null())
            .cloned();
        let response_status = match response.get("status") {
            Some(value) => python_str(Some(value)).to_lowercase(),
            None if payload.is_some() => "complete".to_string(),
            None => "unavailable".to_string(),
        };
        let envelope_status = python_str(envelope.get("status")).to_lowercase();
        let (status, payload) = if provider_failed(response) || provider_failed(&envelope) {
            ("failed".to_string(), None)
        } else if matches!(response_status.as_str(), "empty" | "unavailable")
            || matches!(envelope_status.as_str(), "empty" | "unavailable")
            || payload.is_none()
        {
            ("unavailable".to_string(), None)
        } else {
            let status = if response_status == "success" {
                "complete".to_string()
            } else {
                response_status
            };
            if !matches!(status.as_str(), "complete" | "partial" | "available") {
                return Err(sd_core::error::SdError::validation(format!(
                    "unsupported {name} response status"
                )));
            }
            (status, payload)
        };
        let mut block = Map::new();
        for key in COPIED_FIELDS {
            if key.is_empty() {
                continue;
            }
            if let Some(value) = response.get(key) {
                block.insert(key.to_string(), value.clone());
            }
        }
        block.insert("status".into(), Value::String(status));
        block.insert("data".into(), payload.unwrap_or(Value::Null));
        block.insert("gateway_envelope".into(), envelope);
        if !block.contains_key("actual_source") {
            if let Some(source) = response.get("source") {
                if !source.is_null() {
                    block.insert("actual_source".into(), source.clone());
                }
            }
        }
        domains.insert(name.to_string(), sanitize_data(&Value::Object(block)));
    }
    if normalized_request
        .get("asof")
        .map(Value::is_null)
        .unwrap_or(true)
    {
        if let Some(asof) = collected_asof(&domains) {
            if let Value::Object(object) = &mut normalized_request {
                object.insert("asof".into(), Value::String(asof));
            }
        }
    }
    let mut receipt = Map::new();
    receipt.insert("request".into(), normalized_request);
    receipt.insert(
        "captured_at_utc".into(),
        Value::String(
            captured_at_utc
                .map(str::to_string)
                .unwrap_or_else(|| clock.utc_now()),
        ),
    );
    receipt.insert("domains".into(), Value::Object(domains));
    receipt.insert(
        "trust_boundary".into(),
        Value::String(TRUST_BOUNDARY.into()),
    );
    Ok(Value::Object(receipt))
}

/// Build and write a receipt as a readable JSON artifact (exclusive create).
pub fn write_receipt(
    request: &Value,
    responses: &Map<String, Value>,
    output_path: &std::path::Path,
    captured_at_utc: Option<&str>,
    clock: &dyn sd_core::clock::Clock,
) -> SdResult<std::path::PathBuf> {
    let receipt = make_receipt(request, responses, captured_at_utc, clock)?;
    let payload = readable_payload(&receipt)?;
    if let Some(parent) = output_path.parent() {
        std::fs::create_dir_all(parent).map_err(sd_core::error::SdError::from)?;
    }
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output_path)
        .map_err(sd_core::error::SdError::from)?;
    file.write_all(payload.as_bytes())
        .map_err(sd_core::error::SdError::from)?;
    Ok(output_path.to_path_buf())
}
