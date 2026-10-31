//! Frozen version labels.
//!
//! A version label is part of an on-disk contract. Changing one here without a
//! matching format change is a defect: R17's `T44 documented versions match the
//! runtime` check compares these constants with SKILL.md/README.md.

/// Skill release that ships these documents and the runtime.
pub const SKILL_VERSION: &str = "3.4.0";
/// Research runtime version.
pub const RUNTIME_VERSION: &str = "0.24.0";
/// Forecast policy version.
pub const POLICY_VERSION: &str = "fixed_52w_5d_v1";

/// Snapshot manifest schema written by this runtime.
pub const SNAPSHOT_SCHEMA_VERSION: &str = "1.3";
/// Snapshot schemas this runtime may *read* for diagnostics only.
pub const READABLE_SNAPSHOT_SCHEMAS: &[&str] = &["1.0", "1.1", "1.2", "1.3"];
/// Snapshot schemas that can carry a production-eligible forecast.
pub const PRODUCTION_SNAPSHOT_SCHEMA: &str = "1.3";

/// Analysis artifact schema (`analysis.json`).
pub const ANALYSIS_SCHEMA_VERSION: &str = "analysis-3.0";
/// Researcher result schema.
pub const RESEARCHER_SCHEMA_VERSION: &str = "3.1";
/// Request schema accepted by `research request` / `pipeline`.
pub const REQUEST_SCHEMA_VERSION: &str = "3.0";
/// Collection receipt schema.
pub const COLLECTION_SCHEMA_VERSION: &str = "1.3";
/// Final-response schema.
pub const FINAL_SCHEMA_VERSION: &str = "3.1";
/// News input/result contract label.
pub const NEWS_SCHEMA_VERSION: &str = "1.0";
/// News candidate export schema.
pub const NEWS_CANDIDATE_SCHEMA_VERSION: &str = "1.1";
/// Host AI assessment schema.
pub const NEWS_AI_ASSESSMENT_SCHEMA_VERSION: &str = "1.0";

/// Forward ledger schema written by this runtime (see conflicts.md C01).
pub const LEDGER_SCHEMA_VERSION: &str = "ledger-3.1";
/// Ledger schemas that remain readable for audit and grouping.
pub const READABLE_LEDGER_SCHEMAS: &[&str] =
    &["ledger-1.0", "ledger-2.0", "ledger-3.0", "ledger-3.1"];

/// Calendar resource manifest schema (R03). Schema 2.0 adds the sourced completion
/// semantics (`auction_end_at` / `complete_at`) required by A-CAL-01.
pub const CALENDAR_RESOURCE_SCHEMA_VERSION: &str = "2.0";
/// Calendar resource schemas this runtime may read. Schema 1.0 carries only the
/// continuous close, so it stays readable for diagnostics but can never produce
/// `library_verified` completion evidence or a formal forecast probability.
pub const READABLE_CALENDAR_RESOURCE_SCHEMAS: &[&str] = &["1.0", "2.0"];
/// Completion rule label written into a schema-2.0 resource and manifest.
pub const CALENDAR_COMPLETION_RULE_VERSION: &str = "cas_latest_end_v1";
/// JSON compatibility profile identifiers (R02).
pub const JSON_PROFILE_SNAPSHOT: &str = "snapshot_canonical_v1";
pub const JSON_PROFILE_STAGE: &str = "stage_digest_v1";
pub const JSON_PROFILE_LEDGER: &str = "ledger_event_v1";
pub const JSON_PROFILE_READABLE: &str = "readable_artifact_v1";
