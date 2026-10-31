//! Load a verified calendar resource pack from disk.
//!
//! The pack has two parts: `manifest.json` (identity, coverage, completion rule and
//! SHA-256 per market) and one `<market>.sessions.json` per market. Loading verifies
//! every digest before the data is used, so a truncated or edited resource cannot be
//! mistaken for a verified exchange calendar.
//!
//! A-CAL-01 compatibility rule: the production pack is schema 2.0 and carries sourced
//! completion semantics. A schema-1.0 pack (continuous close only) still loads so old
//! installations can be diagnosed, but its completion semantics are unverified, so it
//! can never produce `library_verified` evidence or a formal forecast probability. A
//! schema-2.0 body without valid completion provenance is rejected outright: a
//! present-but-malformed completion block is tampering, not a downgrade.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::jsonio::read_json_file;
use sd_core::calendar::CalendarResource;
use sd_core::error::{SdError, SdResult};
use sd_core::interfaces::CalendarProvider;
use sd_core::versions::{
    CALENDAR_COMPLETION_RULE_VERSION, CALENDAR_RESOURCE_SCHEMA_VERSION,
    READABLE_CALENDAR_RESOURCE_SCHEMAS,
};

/// A loaded, digest-verified calendar pack.
#[derive(Debug, Clone)]
pub struct CalendarPack {
    root: PathBuf,
    resources: BTreeMap<String, CalendarResource>,
    generator: Option<Value>,
    schema: String,
    completion_rule_version: Option<String>,
}

impl CalendarPack {
    /// Load and verify a pack directory (`stock-data/resources/calendars`).
    pub fn load_dir(root: &Path) -> SdResult<Self> {
        let manifest = read_json_file(&root.join("manifest.json"))?;
        let schema = manifest
            .get("resource_schema")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if !READABLE_CALENDAR_RESOURCE_SCHEMAS.contains(&schema.as_str()) {
            return Err(SdError::validation(format!(
                "unsupported calendar manifest schema {schema:?}; readable schemas are {READABLE_CALENDAR_RESOURCE_SCHEMAS:?}"
            )));
        }
        let completion_rule_version = manifest
            .get("completion_rule_version")
            .and_then(Value::as_str)
            .map(str::to_string);
        if schema == CALENDAR_RESOURCE_SCHEMA_VERSION {
            let declared = completion_rule_version.as_deref().unwrap_or_default();
            if declared != CALENDAR_COMPLETION_RULE_VERSION {
                return Err(SdError::validation(format!(
                    "calendar manifest schema {schema} must declare completion_rule_version {CALENDAR_COMPLETION_RULE_VERSION}; got {declared:?}"
                )));
            }
        }
        let Some(markets) = manifest.get("markets").and_then(Value::as_array) else {
            return Err(SdError::validation(
                "calendar manifest is missing the markets array",
            ));
        };
        let mut resources = BTreeMap::new();
        for entry in markets {
            let Some(market) = entry.get("market").and_then(Value::as_str) else {
                return Err(SdError::validation(
                    "calendar manifest entry is missing its market",
                ));
            };
            let Some(file) = entry.get("file").and_then(Value::as_str) else {
                return Err(SdError::validation(format!(
                    "calendar manifest entry {market} is missing its file"
                )));
            };
            let Some(expected_sha) = entry.get("sha256").and_then(Value::as_str) else {
                return Err(SdError::validation(format!(
                    "calendar manifest entry {market} is missing its digest"
                )));
            };
            let path = root.join(file);
            let bytes = std::fs::read(&path).map_err(SdError::from)?;
            // The digest covers the canonical body without the trailing newline the
            // file carries for readability.
            let body = bytes.strip_suffix(b"\n").unwrap_or(&bytes);
            let mut digest = Sha256::new();
            digest.update(body);
            let actual = format!("{:x}", digest.finalize());
            if actual != expected_sha {
                return Err(SdError::validation(format!(
                    "calendar resource {file} failed its digest check: expected {expected_sha}, got {actual}"
                )));
            }
            let value: Value = serde_json::from_slice(body).map_err(|error| {
                SdError::validation(format!(
                    "calendar resource {file} is not valid JSON: {error}"
                ))
            })?;
            // The body label must match the manifest label: a mixed pack is a
            // configuration error, not something to reconcile silently.
            let body_schema = value
                .get("resource_schema")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if body_schema != schema {
                return Err(SdError::validation(format!(
                    "calendar resource {file} declares schema {body_schema:?} but the manifest says {schema:?}"
                )));
            }
            let resource = CalendarResource::from_json(&value)?;
            if resource.market != market {
                return Err(SdError::validation(format!(
                    "calendar resource {file} declares market {} but the manifest says {market}",
                    resource.market
                )));
            }
            if let Some(provenance) = resource.completion.provenance() {
                if let Some(declared) = &completion_rule_version {
                    if &provenance.rule_version != declared {
                        return Err(SdError::validation(format!(
                            "calendar resource {file} completion rule {} differs from manifest {declared}",
                            provenance.rule_version
                        )));
                    }
                }
            }
            resources.insert(market.to_string(), resource);
        }
        Ok(Self {
            root: root.to_path_buf(),
            resources,
            generator: manifest.get("generator").cloned(),
            schema,
            completion_rule_version,
        })
    }

    /// Pack directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Generator identity recorded in the manifest.
    pub fn generator(&self) -> Option<&Value> {
        self.generator.as_ref()
    }

    /// Manifest resource schema (`1.0` or `2.0`).
    pub fn schema(&self) -> &str {
        &self.schema
    }

    /// Completion rule version recorded in the manifest.
    pub fn completion_rule_version(&self) -> Option<&str> {
        self.completion_rule_version.as_deref()
    }

    /// True when every market resource establishes verified completion semantics.
    pub fn completion_verified(&self) -> bool {
        !self.resources.is_empty()
            && self
                .resources
                .values()
                .all(|resource| resource.completion_is_verified())
    }

    /// Resource for a market.
    pub fn resource(&self, market: &str) -> Option<&CalendarResource> {
        self.resources.get(market)
    }

    /// Markets in the pack.
    pub fn markets(&self) -> Vec<&str> {
        self.resources.keys().map(String::as_str).collect()
    }
}

impl CalendarProvider for CalendarPack {
    fn resource(&self, market: &str) -> Option<&CalendarResource> {
        self.resources.get(market)
    }
}

/// Resolve the pack next to an installed Skill directory.
///
/// Layout contract: `<skill>/assets/calendars/manifest.json`.
pub fn default_pack_path(skill_root: &Path) -> PathBuf {
    skill_root.join("assets").join("calendars")
}

/// Resolve the mounted Skill root.
///
/// Production runs receive `STOCK_DATA_SKILL_ROOT` from `scripts/run.sh`; development
/// runs fall back to walking up from the executable/cwd for `<root>/assets/calendars`
/// (directly inside the Skill) or `<cwd>/stock-data/assets/calendars` (repository).
/// A build-machine `CARGO_MANIFEST_DIR` is never used at runtime.
pub fn resolve_skill_root() -> Option<PathBuf> {
    if let Ok(root) = std::env::var("STOCK_DATA_SKILL_ROOT") {
        let path = PathBuf::from(root);
        if path.is_dir() {
            return Some(path);
        }
    }
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(executable) = std::env::current_exe() {
        if let Some(directory) = executable.parent() {
            starts.push(directory.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    for start in starts {
        let mut current: Option<&Path> = Some(start.as_path());
        while let Some(directory) = current {
            if has_calendar_pack(directory) {
                return Some(directory.to_path_buf());
            }
            let nested = directory.join("stock-data");
            if has_calendar_pack(&nested) {
                return Some(nested);
            }
            current = directory.parent();
        }
    }
    None
}

fn has_calendar_pack(root: &Path) -> bool {
    root.join("assets")
        .join("calendars")
        .join("manifest.json")
        .is_file()
}

/// Resolve the calendar pack directory.
///
/// Precedence: explicit CLI flag, `STOCK_DATA_CALENDAR_DIR`, then the mounted Skill.
pub fn resolve_pack_dir(explicit: Option<&Path>) -> Option<PathBuf> {
    if let Some(directory) = explicit {
        return Some(directory.to_path_buf());
    }
    if let Ok(directory) = std::env::var("STOCK_DATA_CALENDAR_DIR") {
        if !directory.trim().is_empty() {
            return Some(PathBuf::from(directory));
        }
    }
    resolve_skill_root().map(|root| default_pack_path(&root))
}

/// Load a verified calendar pack.
///
/// A *missing* pack is missing evidence (`Ok(None)`, downstream `CALENDAR_UNVERIFIED`);
/// a present but corrupt/tampered pack is a hard error and must never degrade silently.
/// An explicitly requested directory must exist and contain a manifest.
pub fn load_pack(explicit: Option<&Path>) -> SdResult<Option<CalendarPack>> {
    let Some(directory) = resolve_pack_dir(explicit) else {
        return Ok(None);
    };
    let manifest = directory.join("manifest.json");
    if !manifest.is_file() {
        if explicit.is_some() || std::env::var("STOCK_DATA_CALENDAR_DIR").is_ok() {
            return Err(sd_core::error::SdError::validation(format!(
                "calendar pack directory {} has no manifest.json",
                directory.display()
            )));
        }
        return Ok(None);
    }
    CalendarPack::load_dir(&directory).map(Some)
}

impl CalendarPack {
    /// Convert one market resource into the collector `session_calendar` block.
    ///
    /// The block carries the per-session three-instant semantics and, when the
    /// resource has sourced provenance, the completion block itself, so a frozen
    /// snapshot can be re-read without consulting the installed pack. Resources
    /// without declared provenance (legacy schema 1.0) omit the block: the snapshot
    /// then keeps the resource's honest unverified/rule-based semantics instead of
    /// inheriting a claim the pack could not make.
    pub fn schedule_value(&self, market: &str) -> Option<Value> {
        let resource = self.resource(market)?;
        let sessions: Vec<Value> = resource
            .sessions
            .iter()
            .map(|session| {
                serde_json::json!({
                    "date": &session.date,
                    "close_at": &session.close_at,
                    "auction_end_at": &session.auction_end_at,
                    "complete_at": &session.complete_at,
                })
            })
            .collect();
        let mut block = serde_json::json!({
            "source": resource.source,
            "timezone": resource.timezone,
            "coverage_start": resource.coverage_start,
            "coverage_end": resource.coverage_end,
            "sessions": sessions,
        });
        if let Some(provenance) = resource.completion.provenance() {
            if let Value::Object(object) = &mut block {
                object.insert("completion".into(), provenance.value().clone());
            }
        }
        Some(block)
    }
}
