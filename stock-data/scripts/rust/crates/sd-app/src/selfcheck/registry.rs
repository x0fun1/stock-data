//! Registry of migrated legacy checks.
//!
//! `implementation()` is the single switch that decides whether a check is real.
//! Registering an id without a function body is impossible by construction, so an
//! empty placeholder can never be reported as implemented.

use serde_json::{Map, Value};

use crate::selfcheck::cases;
use crate::selfcheck_generated::{
    CheckSpec, ACTIVE_CHECKS, ACTIVE_CHECK_COUNT, RETIRED_CHECKS, RETIRED_CHECK_COUNT,
};

/// A check body: returns `Err(reason)` on failure.
pub type CheckFn = fn() -> Result<(), String>;

/// Implementation status of one migrated check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckStatus {
    /// The Rust assertion exists and runs real assertions.
    Implemented,
    /// Not migrated yet. Never counted as passing.
    NotImplemented,
}

impl CheckStatus {
    /// Stable machine-readable label.
    pub fn as_str(self) -> &'static str {
        match self {
            CheckStatus::Implemented => "implemented",
            CheckStatus::NotImplemented => "not_implemented",
        }
    }
}

/// One registry row.
#[derive(Debug, Clone)]
pub struct CheckEntry {
    pub spec: CheckSpec,
    pub status: CheckStatus,
}

/// The check body for an id, when it has been migrated.
///
/// R02 migrated `F03` (credential redaction). Later packages add their ids here in
/// the same commit as the assertion, so the list is the authoritative coverage
/// statement.
pub fn implementation(id: &str) -> Option<CheckFn> {
    if id == "F03" {
        return Some(cases::legacy_f03);
    }
    for (name, body) in crate::selfcheck::legacy_r03::implementations()
        .into_iter()
        .chain(crate::selfcheck::legacy_r04::implementations())
        .chain(crate::selfcheck::legacy_r05::implementations())
        .chain(crate::selfcheck::legacy_r06::implementations())
        .chain(crate::selfcheck::legacy_r07::implementations())
        .chain(crate::selfcheck::legacy_r08::implementations())
        .chain(crate::selfcheck::legacy_r09::implementations())
        .chain(crate::selfcheck::legacy_r10::implementations())
        .chain(crate::selfcheck::legacy_r11::implementations())
        .chain(crate::selfcheck::legacy_r15::implementations())
        .chain(crate::selfcheck::legacy_r17::implementations())
    {
        if name == id {
            return Some(body);
        }
    }
    None
}

/// The registry: every active check with its current status.
pub fn registry() -> Vec<CheckEntry> {
    ACTIVE_CHECKS
        .iter()
        .map(|spec| CheckEntry {
            spec: *spec,
            status: if implementation(spec.id).is_some() {
                CheckStatus::Implemented
            } else {
                CheckStatus::NotImplemented
            },
        })
        .collect()
}

/// `selfcheck --list` document.
pub fn list_document() -> Value {
    let registry = registry();
    let implemented = registry
        .iter()
        .filter(|entry| entry.status == CheckStatus::Implemented)
        .count();
    let checks: Vec<Value> = registry
        .iter()
        .map(|entry| {
            let mut row = Map::new();
            row.insert("id".into(), Value::String(entry.spec.id.into()));
            row.insert("label".into(), Value::String(entry.spec.label.into()));
            row.insert(
                "primary_task".into(),
                Value::String(entry.spec.primary_task.into()),
            );
            row.insert(
                "python_function".into(),
                Value::String(entry.spec.python_function.into()),
            );
            row.insert("status".into(), Value::String(entry.status.as_str().into()));
            Value::Object(row)
        })
        .collect();
    let retired: Vec<Value> = RETIRED_CHECKS
        .iter()
        .map(|(name, reason)| {
            let mut row = Map::new();
            row.insert("name".into(), Value::String((*name).into()));
            row.insert("reason".into(), Value::String((*reason).into()));
            Value::Object(row)
        })
        .collect();
    let mut document = Map::new();
    document.insert("schema_version".into(), Value::String("1.0".into()));
    document.insert("active_checks".into(), Value::from(ACTIVE_CHECK_COUNT));
    document.insert("retired_checks".into(), Value::from(RETIRED_CHECK_COUNT));
    document.insert("implemented".into(), Value::from(implemented));
    document.insert(
        "not_implemented".into(),
        Value::from(registry.len() - implemented),
    );
    document.insert("checks".into(), Value::Array(checks));
    document.insert("retired".into(), Value::Array(retired));
    Value::Object(document)
}

/// Outcome of a selfcheck run.
#[derive(Debug, Clone)]
pub struct RunOutcome {
    /// Every selected check passed, the selection was non-empty and every requested id
    /// was known and executed.
    pub passed: bool,
    /// Requested ids (deduplicated) that should have run.
    pub requested: usize,
    /// Implemented checks that were executed.
    pub executed: usize,
    /// Implemented checks that failed, with their reason.
    pub failed: Vec<(String, String)>,
    /// Selected checks without an implementation yet.
    pub not_implemented: Vec<String>,
    /// Requested ids that are not part of the active registry.
    pub unknown: Vec<String>,
}

/// Run the selected offline checks.
///
/// Unknown ids are rejected *before* executing anything. An empty selection can never be
/// reported as success, and the executed count must equal the requested count. A panic in
/// one check body is recorded as an explicit failure (with the panic message) instead of
/// aborting into a state that could be mistaken for a pass.
pub fn run(filter: &[String]) -> RunOutcome {
    let registry = registry();
    let mut requested_ids: Vec<String> = Vec::new();
    for id in filter {
        if !requested_ids.iter().any(|known| known == id) {
            requested_ids.push(id.clone());
        }
    }
    let known: Vec<&str> = registry.iter().map(|entry| entry.spec.id).collect();
    let unknown: Vec<String> = requested_ids
        .iter()
        .filter(|id| !known.iter().any(|name| name == &id.as_str()))
        .cloned()
        .collect();
    let expected = if requested_ids.is_empty() {
        registry.len()
    } else {
        requested_ids.len()
    };
    if !unknown.is_empty() {
        return RunOutcome {
            passed: false,
            requested: expected,
            executed: 0,
            failed: Vec::new(),
            not_implemented: Vec::new(),
            unknown,
        };
    }
    let selected: Vec<&CheckEntry> = if requested_ids.is_empty() {
        registry.iter().collect()
    } else {
        registry
            .iter()
            .filter(|entry| requested_ids.iter().any(|id| id == entry.spec.id))
            .collect()
    };
    let mut executed = 0usize;
    let mut failed: Vec<(String, String)> = Vec::new();
    let mut not_implemented: Vec<String> = Vec::new();
    for entry in &selected {
        match implementation(entry.spec.id) {
            Some(body) => {
                executed += 1;
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(body)) {
                    Ok(Ok(())) => {}
                    Ok(Err(reason)) => failed.push((entry.spec.id.to_string(), reason)),
                    Err(payload) => {
                        let detail = payload
                            .downcast_ref::<&str>()
                            .map(|value| (*value).to_string())
                            .or_else(|| payload.downcast_ref::<String>().cloned())
                            .unwrap_or_else(|| "unknown panic payload".to_string());
                        failed.push((entry.spec.id.to_string(), format!("panic: {detail}")));
                    }
                }
            }
            None => not_implemented.push(entry.spec.id.to_string()),
        }
    }
    let passed = !selected.is_empty()
        && failed.is_empty()
        && not_implemented.is_empty()
        && executed == expected
        && expected == selected.len();
    RunOutcome {
        passed,
        requested: expected,
        executed,
        failed,
        not_implemented,
        unknown: Vec::new(),
    }
}

/// Outcome as a JSON report (`selfcheck --offline`).
pub fn outcome_document(outcome: &RunOutcome) -> Value {
    let mut document = Map::new();
    document.insert("schema_version".into(), Value::String("1.0".into()));
    document.insert(
        "status".into(),
        Value::String(if outcome.passed { "success" } else { "error" }.into()),
    );
    document.insert("requested".into(), Value::from(outcome.requested));
    document.insert("executed".into(), Value::from(outcome.executed));
    document.insert(
        "unknown".into(),
        Value::Array(
            outcome
                .unknown
                .iter()
                .map(|id| Value::String(id.clone()))
                .collect(),
        ),
    );
    document.insert(
        "failed".into(),
        Value::Array(
            outcome
                .failed
                .iter()
                .map(|(id, reason)| {
                    let mut row = Map::new();
                    row.insert("id".into(), Value::String(id.clone()));
                    row.insert("reason".into(), Value::String(reason.clone()));
                    Value::Object(row)
                })
                .collect(),
        ),
    );
    document.insert(
        "not_implemented".into(),
        Value::Array(
            outcome
                .not_implemented
                .iter()
                .map(|id| Value::String(id.clone()))
                .collect(),
        ),
    );
    Value::Object(document)
}
