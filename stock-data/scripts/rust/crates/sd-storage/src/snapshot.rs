//! Frozen snapshot loading.
//!
//! `freeze_snapshot` (which re-runs the data gate) arrives with the `data_checks`
//! port; this module implements the **load** half that every research path needs:
//! verify the frozen digests first, then hand out the verified market view.
//!
//! Verification order mirrors `snapshot.load_snapshot`:
//!
//! 1. `manifest.json` + `market.json` exist and the schema is readable;
//! 2. `snapshot_id` is one safe path component;
//! 3. every normalized and raw domain file matches `domain_sha256s` /
//!    `raw_domain_sha256s`;
//! 4. the recomputed content digest equals `snapshot_content_sha256` and is the
//!    suffix of `snapshot_id`;
//! 5. ticker/market/horizon/as-of agree between the manifest and its request;
//! 6. for schema 1.3 the manifest digest covers every other field, and the
//!    collection capture cannot post-date the snapshot creation;
//! 7. the request re-normalizes and the bar count matches.
//!
//! Step 7's *derived metadata recomputation* (`_derived_metadata`) needs the data
//! gate; until it lands [`LoadedSnapshot::validation_recomputed`] is `false` and the
//! frozen `data_validation` block is returned unchanged, so a caller cannot mistake
//! the frozen claim for a fresh check.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::Value;

use sd_core::contracts::normalize_request;
use sd_core::error::{SdError, SdResult};
use sd_core::interfaces::{Bar, MarketView, SnapshotIdentity};
use sd_core::versions::{PRODUCTION_SNAPSHOT_SCHEMA, READABLE_SNAPSHOT_SCHEMAS};

use crate::jsonio::{read_json_file, read_text_file};
use crate::legacy_json::canonical_sha256;
use crate::path_guard::validate_path_component;

/// The nine frozen domains, in the legacy order.
pub const DOMAINS: [&str; 9] = [
    "market",
    "fundamentals",
    "expectations",
    "news",
    "insider",
    "options",
    "short_volume",
    "sec",
    "macro",
];

/// A digest-verified snapshot.
#[derive(Debug, Clone)]
pub struct LoadedSnapshot {
    /// Full manifest, including the frozen derived blocks.
    pub manifest: Value,
    /// Identity fields used to bind artifacts to this snapshot.
    pub identity: SnapshotIdentity,
    /// Normalized domains by name.
    pub domains: BTreeMap<String, Value>,
    /// Verified market view for the researchers (calendar included when frozen).
    pub market: MarketView,
    /// The frozen `data_validation` block (see [`Self::validation_recomputed`]).
    pub frozen_validation: Value,
    /// True only when the data gate was re-run at load time (needs `data_checks`).
    pub validation_recomputed: bool,
    /// Schema version read from the manifest.
    pub schema_version: String,
    /// True when this schema is the current production schema.
    pub production_schema: bool,
}

impl LoadedSnapshot {
    /// Ticker recorded by the manifest.
    pub fn ticker(&self) -> &str {
        &self.identity.ticker
    }

    /// Snapshot directory id.
    pub fn snapshot_id(&self) -> &str {
        &self.identity.snapshot_id
    }
}

/// Load and verify a frozen snapshot directory.
pub fn load_snapshot(snapshot_dir: &Path) -> SdResult<LoadedSnapshot> {
    let manifest_path = snapshot_dir.join("manifest.json");
    let market_path = snapshot_dir.join("market.json");
    if !manifest_path.is_file() || !market_path.is_file() {
        return Err(SdError::validation(
            "snapshot must include manifest.json and market.json",
        ));
    }
    let mut manifest = read_json_file(&manifest_path)?;
    let schema_version = manifest
        .get("schema_version")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !READABLE_SNAPSHOT_SCHEMAS.contains(&schema_version.as_str()) {
        return Err(SdError::validation(format!(
            "unsupported snapshot schema: {schema_version:?}"
        )));
    }
    let snapshot_id = manifest
        .get("snapshot_id")
        .and_then(Value::as_str)
        .ok_or_else(|| SdError::validation("snapshot_id must be a string"))?;
    let snapshot_id = validate_path_component(snapshot_id, "snapshot_id")?;

    // Domains: digest check before anything is interpreted.
    let mut domains: BTreeMap<String, Value> = BTreeMap::new();
    for name in DOMAINS {
        let path = snapshot_dir.join(format!("{name}.json"));
        let raw_path = snapshot_dir.join("raw").join(format!("{name}.json"));
        if !path.is_file() || !raw_path.is_file() {
            return Err(SdError::validation(format!(
                "snapshot is missing {name}.json or raw/{name}.json"
            )));
        }
        let block = read_json_file(&path)?;
        let raw_block = read_json_file(&raw_path)?;
        let expected = manifest
            .get("domain_sha256s")
            .and_then(|value| value.get(name))
            .and_then(Value::as_str);
        let raw_expected = manifest
            .get("raw_domain_sha256s")
            .and_then(|value| value.get(name))
            .and_then(Value::as_str);
        if expected != Some(canonical_sha256(&block)?.as_str()) {
            return Err(SdError::validation(format!(
                "snapshot domain digest mismatch: {name}"
            )));
        }
        if raw_expected != Some(canonical_sha256(&raw_block)?.as_str()) {
            return Err(SdError::validation(format!(
                "snapshot raw domain digest mismatch: {name}"
            )));
        }
        domains.insert(name.to_string(), block);
    }
    let domain_values: BTreeMap<String, &Value> = domains
        .iter()
        .map(|(name, value)| (name.clone(), value))
        .collect();
    let content_digest = content_sha256(&manifest, &domain_values)?;
    if manifest
        .get("snapshot_content_sha256")
        .and_then(Value::as_str)
        != Some(content_digest.as_str())
    {
        return Err(SdError::validation("snapshot content identity mismatch"));
    }
    if !snapshot_id.ends_with(&content_digest[..10]) {
        return Err(SdError::validation("snapshot content identity mismatch"));
    }

    // Request identity: the manifest's own copies must agree with the request.
    let request = manifest
        .get("request")
        .ok_or_else(|| SdError::validation("snapshot manifest is missing its request"))?;
    for key in ["ticker", "horizon", "market"] {
        if manifest.get(key) != request.get(key) {
            return Err(SdError::validation("snapshot request identity mismatch"));
        }
    }
    if manifest.get("asof_timestamp") != request.get("asof") {
        return Err(SdError::validation("snapshot request identity mismatch"));
    }

    let production_schema = schema_version == PRODUCTION_SNAPSHOT_SCHEMA;
    if production_schema {
        let Some(expected) = manifest.get("manifest_sha256").and_then(Value::as_str) else {
            return Err(SdError::validation("snapshot manifest digest mismatch"));
        };
        let mut without_digest = manifest.clone();
        if let Value::Object(object) = &mut without_digest {
            object.remove("manifest_sha256");
        }
        if canonical_sha256(&without_digest)? != expected {
            return Err(SdError::validation("snapshot manifest digest mismatch"));
        }
        if let (Some(captured), Some(created)) = (
            manifest
                .get("collection_captured_at")
                .and_then(Value::as_str),
            manifest.get("snapshot_created_at").and_then(Value::as_str),
        ) {
            if sd_core::contracts::parse_timestamp_str(captured)?
                > sd_core::contracts::parse_timestamp_str(created)?
            {
                return Err(SdError::validation(
                    "collection capture occurs after snapshot creation",
                ));
            }
        }
    }

    let normalized = normalize_request(request)?;
    if normalized.ticker != snapshot_id.split('_').next().unwrap_or_default() {
        // The slug is derived from the ticker (`re.sub(r"[^A-Z0-9._-]+", "_", ticker)`),
        // so a mismatch means the manifest was assembled inconsistently.
        let slug: String = normalized
            .ticker
            .chars()
            .map(|character| {
                if character.is_ascii_uppercase()
                    || character.is_ascii_digit()
                    || matches!(character, '.' | '_' | '-')
                {
                    character
                } else {
                    '_'
                }
            })
            .collect();
        if !snapshot_id.starts_with(&format!("{slug}_")) {
            return Err(SdError::validation("snapshot ticker identity mismatch"));
        }
    }

    // Load-time derived recomputation (`_derived_metadata`): the frozen gate verdict
    // is re-derived from the frozen domains so a stale claim cannot survive a policy
    // change, and a legacy schema can only ever be diagnostic.
    let resolved_evidence = manifest
        .get("data_validation")
        .and_then(|value| value.get("resolved_calendar_evidence"))
        .filter(|value| !value.is_null())
        .cloned();
    let resolved_resource = match &resolved_evidence {
        Some(value) => {
            sd_core::calendar::CalendarResource::from_snapshot_calendar(value, &normalized.market)
                .ok()
        }
        None => None,
    };
    let legacy = schema_version != PRODUCTION_SNAPSHOT_SCHEMA;
    let captured_at = manifest
        .get("collection_captured_at")
        .and_then(Value::as_str)
        .map(str::to_string);
    let derived = crate::freeze::derived_metadata(
        &mut domains,
        &normalized.to_json(),
        captured_at.as_deref(),
        legacy,
        resolved_resource.as_ref(),
        false,
        None,
    )?;
    if let Value::Object(derived_object) = derived {
        if let Value::Object(manifest_object) = &mut manifest {
            for (key, value) in derived_object {
                manifest_object.insert(key, value);
            }
        }
    }
    let market_block = domains.get("market").expect("market domain always present");
    let market_data = market_block.get("data").and_then(Value::as_object);
    let Some(bars_value) = market_data
        .and_then(|data| data.get("bars"))
        .and_then(Value::as_array)
    else {
        return Err(SdError::validation("snapshot has no market OHLCV bars"));
    };
    if let Some(declared) = manifest.get("market_bar_count").and_then(Value::as_i64) {
        if declared as usize != bars_value.len() {
            return Err(SdError::validation("snapshot market bar count mismatch"));
        }
    }
    let bars = parse_bars(bars_value)?;

    // Frozen session calendar (normalized block, then the unwrapped data block).
    let calendar_value = market_block
        .get("session_calendar")
        .or_else(|| market_data.and_then(|data| data.get("session_calendar")));
    let calendar = match calendar_value {
        Some(value) if !value.is_null() => Some(
            sd_core::calendar::CalendarResource::from_snapshot_calendar(value, &normalized.market)?,
        ),
        _ => None,
    };

    let identity = SnapshotIdentity {
        snapshot_id: snapshot_id.clone(),
        ticker: normalized.ticker.clone(),
        market: normalized.market.clone(),
        horizon: normalized.horizon.clone(),
        asof_timestamp: manifest
            .get("asof_timestamp")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        schema_version: schema_version.clone(),
        snapshot_content_sha256: content_digest,
    };
    let market = MarketView {
        ticker: identity.ticker.clone(),
        market: identity.market.clone(),
        horizon: identity.horizon.clone(),
        asof_timestamp: identity.asof_timestamp.clone(),
        bars,
        calendar,
        price_basis: market_block
            .get("price_basis")
            .and_then(Value::as_str)
            .map(str::to_string),
        adjustment: market_block
            .get("adjustment")
            .and_then(Value::as_str)
            .map(str::to_string),
        actual_source: market_block
            .get("actual_source")
            .or_else(|| market_block.get("source"))
            .and_then(Value::as_str)
            .map(str::to_string),
        currency: market_block
            .get("currency")
            .and_then(Value::as_str)
            .map(str::to_string),
        unit: market_block
            .get("unit")
            .and_then(Value::as_str)
            .map(str::to_string),
    };
    let frozen_validation = manifest
        .get("data_validation")
        .cloned()
        .unwrap_or(Value::Null);
    Ok(LoadedSnapshot {
        manifest,
        identity,
        domains,
        market,
        frozen_validation,
        validation_recomputed: true,
        schema_version,
        production_schema,
    })
}

/// Recompute `snapshot_content_sha256` from the request, as-of and domain blocks.
fn content_sha256(manifest: &Value, domains: &BTreeMap<String, &Value>) -> SdResult<String> {
    let mut payload = serde_json::Map::new();
    payload.insert(
        "request".into(),
        manifest.get("request").cloned().unwrap_or(Value::Null),
    );
    payload.insert(
        "asof_timestamp".into(),
        manifest
            .get("asof_timestamp")
            .cloned()
            .unwrap_or(Value::Null),
    );
    let mut domain_map = serde_json::Map::new();
    // Insert in the legacy DOMAINS order: canonical serialization sorts keys, so the
    // map only has to contain the same set, but keeping the order documents intent.
    for name in DOMAINS {
        if let Some(value) = domains.get(name) {
            domain_map.insert(name.to_string(), (*value).clone());
        }
    }
    payload.insert("domains".into(), Value::Object(domain_map));
    canonical_sha256(&Value::Object(payload))
}

fn parse_bars(rows: &[Value]) -> SdResult<Vec<Bar>> {
    let mut bars = Vec::with_capacity(rows.len());
    for (index, row) in rows.iter().enumerate() {
        let Value::Object(_) = row else {
            return Err(SdError::validation(format!(
                "market bar {index} is not an object"
            )));
        };
        let number = |field: &str| -> SdResult<f64> {
            match row.get(field) {
                Some(Value::Number(value)) => value.as_f64().ok_or_else(|| {
                    SdError::validation(format!("market bar {index} has a non-finite {field}"))
                }),
                _ => Err(SdError::validation(format!(
                    "market bar {index} is missing numeric {field}"
                ))),
            }
        };
        bars.push(Bar {
            date: row
                .get("date")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    SdError::validation(format!("market bar {index} is missing its date"))
                })?
                .to_string(),
            open: number("open")?,
            high: number("high")?,
            low: number("low")?,
            close: number("close")?,
            volume: number("volume")?,
        });
    }
    Ok(bars)
}

/// Read the raw manifest text (used by callers that must not re-serialize it).
pub fn manifest_text(snapshot_dir: &Path) -> SdResult<String> {
    read_text_file(&snapshot_dir.join("manifest.json"))
}
