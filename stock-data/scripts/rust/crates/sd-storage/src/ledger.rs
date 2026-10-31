//! Append-only forward prediction ledger (`ledger.py`, R09).
//!
//! A prediction is written once, when the analysis is frozen. A settlement is a second
//! event that references the prediction id; neither event is ever edited. Re-running
//! either operation is a no-op, so a retried analysis cannot double-count the same
//! forecast.
//!
//! Schema 2.0 separates three ideas that schema 1.0 conflated: a prediction is
//! *prospective* only when it was recorded before its label could have been observed;
//! `observed_up_rate` is the base rate of the realised labels, not a model hit rate; and
//! the scoring baseline is the causal expanding up-rate knowable at the prediction date.
//! Schema 3.1 adds the prompt-recording condition: a record written several sessions
//! later must not count as forward evidence even if the label had not matured.
//!
//! Schema 1.0/2.0/3.0 rows remain readable for audit but are reported separately and can
//! never be counted as forward evidence. Only the current schema can be written.
//!
//! The ledger lives outside the run directory on purpose: cleanup deletes run JSON, and a
//! forward record must survive that cleanup. All wall-clock reads are injected
//! (`recorded_at`, `settled_at`, `today`) so the caller — never this module — decides the
//! measurement instant; the CLI never supplies one, so a record cannot be backdated.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use sd_core::contracts::{python_repr, python_str};
use sd_core::float_sum::python_sum;
use sd_core::json_text::canonical_text;
use sd_core::policy::{
    ledger_group_key, model_identity, PROSPECTIVE_RECORDING_GRACE_HOURS, TARGET_VERSION,
};

/// Current writable schema.
pub const SCHEMA_VERSION: &str = "ledger-3.1";
/// Schemas that remain readable (never writable).
pub const READABLE_SCHEMAS: [&str; 4] = [SCHEMA_VERSION, "ledger-3.0", "ledger-2.0", "ledger-1.0"];
/// Prediction event name.
pub const PREDICTION: &str = "prediction";
/// Settlement event name.
pub const SETTLEMENT: &str = "settlement";
/// Settlement rule recorded on every settlement.
pub const SETTLEMENT_RULE: &str = "close_to_close_horizon_sessions";
/// Baseline rule recorded on every settlement/prediction.
pub const BASELINE_RULE: &str = "causal_expanding_up_rate_matured_labels";
/// The prompt-recording rule text stored with every prediction.
pub const PROSPECTIVE_RECORDING_RULE: &str = "recorded_at <= decision_close + 12h, which is inside the regular US/HK close-to-next-open gap; label maturity is checked separately";

/// Ledger line / operation error. The kind mirrors the Python exception class.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LedgerErrorKind {
    /// `ValueError` in the legacy implementation.
    Validation,
    /// `TypeError` in the legacy implementation.
    Type,
}

/// One ledger rejection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerError {
    /// Legacy exception class.
    pub kind: LedgerErrorKind,
    /// Legacy message (checkable by the self-check suite).
    pub message: String,
}

impl LedgerError {
    fn validation(message: impl Into<String>) -> Self {
        Self {
            kind: LedgerErrorKind::Validation,
            message: message.into(),
        }
    }

    fn type_error(message: impl Into<String>) -> Self {
        Self {
            kind: LedgerErrorKind::Type,
            message: message.into(),
        }
    }
}

impl fmt::Display for LedgerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for LedgerError {}

/// Convenience result type.
pub type LedgerResult<T> = Result<T, LedgerError>;

/// Options of [`record_prediction`]. The recording instant is a separate parameter
/// because the legacy module read `datetime.now()`; the CLI never supplies it.
#[derive(Default, Clone)]
pub struct RecordOptions<'a> {
    /// Why a non-prospective record is being imported (`historical`, ...).
    pub import_reason: Option<&'a str>,
    /// Frozen bars (with `date` and `close`) used for the causal baseline.
    pub bars: Option<&'a [Value]>,
    /// Frozen exchange schedule rows (`date`, `close_at`) for label-maturity timing.
    pub sessions: Option<&'a [Value]>,
    /// Load the bars/schedule from a frozen snapshot directory when `bars` is absent.
    pub snapshot_dir: Option<&'a Path>,
}

fn python_truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Null) | None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn number_value(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

fn opt_value(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

fn as_text(value: Option<&Value>) -> String {
    value.map(python_str).unwrap_or_else(|| "None".to_string())
}

fn mean_of(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        None
    } else {
        Some(python_sum(values.iter().copied()) / values.len() as f64)
    }
}

/// Python `round(value, digits)` (decimal rounding of the binary double).
pub fn py_round(value: f64, digits: usize) -> f64 {
    if !value.is_finite() {
        return value;
    }
    format!("{value:.digits$}").parse::<f64>().unwrap_or(value)
}

fn sha256_hex(text: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// `ledger._canonical_sha256`: canonical JSON digest.
pub fn canonical_sha256(payload: &Value) -> String {
    sha256_hex(&canonical_text(payload))
}

/// Parse a legacy timestamp string into a UTC instant (`ledger._parse_time`).
pub fn parse_ledger_time(value: Option<&Value>) -> Option<DateTime<Utc>> {
    let Some(Value::String(raw)) = value else {
        return None;
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let text = trimmed
        .strip_suffix('Z')
        .map(|prefix| format!("{prefix}+00:00"))
        .unwrap_or_else(|| trimmed.to_string());
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&text) {
        return Some(parsed.with_timezone(&Utc));
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M:%S%.f%z",
        "%Y-%m-%d %H:%M:%S%.f%z",
    ] {
        if let Ok(parsed) = DateTime::parse_from_str(&text, format) {
            return Some(parsed.with_timezone(&Utc));
        }
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(parsed) = chrono::NaiveDateTime::parse_from_str(&text, format) {
            return Some(Utc.from_utc_datetime(&parsed));
        }
    }
    if let Ok(day) = NaiveDate::parse_from_str(&text, "%Y-%m-%d") {
        if let Some(midnight) = day.and_hms_opt(0, 0, 0) {
            return Some(Utc.from_utc_datetime(&midnight));
        }
    }
    None
}

/// Python `datetime.isoformat().replace("+00:00", "Z")`.
pub fn iso(moment: DateTime<Utc>) -> String {
    if moment.timestamp_subsec_micros() == 0 {
        moment.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        moment.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

/// Numeric session count carried by a horizon label (`5D` -> 5).
pub fn horizon_sessions(horizon: Option<&Value>) -> i64 {
    let text = horizon
        .map(python_str)
        .unwrap_or_else(|| "None".to_string());
    let digits: String = text
        .chars()
        .filter(|character| character.is_ascii_digit())
        .collect();
    digits.parse::<i64>().map(|value| value.max(1)).unwrap_or(5)
}

/// Calendar-day span of a session horizon (`5` sessions -> 7 calendar days).
pub fn horizon_calendar_days(sessions: i64) -> i64 {
    ((sessions * 7) + 4) / 5
}

/// Stable prediction identity (`ledger._prediction_id`).
pub fn prediction_id(result: &Value) -> String {
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    let probability = consensus.get("prob_up").and_then(Value::as_f64);
    let probability_text = match probability {
        Some(value) => format!("{value:.12}"),
        None => "none".to_string(),
    };
    let identity = [
        as_text(snapshot.get("ticker")),
        as_text(snapshot.get("market")),
        as_text(snapshot.get("horizon")),
        as_text(snapshot.get("asof_timestamp")),
        as_text(snapshot.get("snapshot_content_sha256")),
        as_text(
            result
                .get("engine")
                .and_then(|engine| engine.get("version")),
        ),
        probability_text,
    ]
    .join("|");
    sha256_hex(&identity).chars().take(32).collect()
}

fn researcher_rows(result: &Value) -> Vec<(String, Value)> {
    let Some(researchers) = result.get("researchers") else {
        return Vec::new();
    };
    let mut rows: Vec<(String, Value)> = Vec::new();
    if let Some(map) = researchers.as_object() {
        for (name, row) in map {
            if row.is_object() {
                rows.push((name.clone(), row.clone()));
            }
        }
    } else if let Some(items) = researchers.as_array() {
        for (index, row) in items.iter().enumerate() {
            if row.is_object() {
                let name = row
                    .get("id")
                    .filter(|value| python_truthy(Some(value)))
                    .or_else(|| row.get("name").filter(|value| python_truthy(Some(value))))
                    .map(python_str)
                    .unwrap_or_else(|| index.to_string());
                rows.push((name, row.clone()));
            }
        }
    }
    rows.sort_by(|left, right| left.0.cmp(&right.0));
    rows
}

fn model_provenance(result: &Value) -> Value {
    let mut probabilities = Map::new();
    let mut sources = Map::new();
    let mut statuses = Map::new();
    let mut weights = Map::new();
    let mut fingerprints = Map::new();
    let mut versions = Map::new();
    let mut features = Map::new();
    let mut training_boundaries = Map::new();
    let mut calibration_states = Map::new();
    for (name, row) in researcher_rows(result) {
        probabilities.insert(name.clone(), opt_value(row.get("prob_up")));
        sources.insert(
            name.clone(),
            opt_value(
                row.get("probability_source")
                    .filter(|value| python_truthy(Some(value)))
                    .or_else(|| {
                        row.get("probability_basis")
                            .filter(|value| python_truthy(Some(value)))
                    }),
            ),
        );
        statuses.insert(name.clone(), opt_value(row.get("status")));
        if !matches!(row.get("weight"), None | Some(Value::Null)) {
            weights.insert(name.clone(), opt_value(row.get("weight")));
        }
        let validation = row.get("validation").cloned().unwrap_or(Value::Null);
        if validation.is_object() {
            fingerprints.insert(name.clone(), Value::String(canonical_sha256(&validation)));
            let hyperparameters = validation
                .get("model_hyperparameters")
                .cloned()
                .unwrap_or(Value::Null);
            if let Some(items) = hyperparameters.get("features").and_then(Value::as_array) {
                if !items.is_empty() {
                    features.insert(name.clone(), Value::Array(items.clone()));
                }
            }
            if let Some(boundary) = validation
                .get("current_prediction_model_training_boundary")
                .filter(|value| value.is_object())
            {
                training_boundaries.insert(name.clone(), boundary.clone());
            }
            let calibration = validation
                .get("current_prediction_calibration")
                .filter(|value| python_truthy(Some(value)))
                .or_else(|| validation.get("probability_calibration"))
                .cloned()
                .unwrap_or(Value::Null);
            if let Some(calibration_map) = calibration.as_object() {
                let mut state = Map::new();
                for key in [
                    "status",
                    "applied",
                    "calibrated",
                    "observations",
                    "fit_observations",
                    "label_end_session",
                    "reachable",
                ] {
                    if let Some(value) = calibration_map.get(key) {
                        state.insert(key.to_string(), value.clone());
                    }
                }
                calibration_states.insert(name.clone(), Value::Object(state));
            }
            if matches!(row.get("model_version"), None | Some(Value::Null))
                && !matches!(validation.get("model_version"), None | Some(Value::Null))
            {
                versions.insert(name.clone(), opt_value(validation.get("model_version")));
            }
        }
        if !matches!(row.get("model_version"), None | Some(Value::Null)) {
            versions.insert(name.clone(), opt_value(row.get("model_version")));
        }
    }
    let mut config = Map::new();
    config.insert(
        "engine_version".into(),
        opt_value(
            result
                .get("engine")
                .and_then(|engine| engine.get("version")),
        ),
    );
    config.insert(
        "horizon".into(),
        opt_value(
            result
                .get("snapshot")
                .and_then(|snapshot| snapshot.get("horizon")),
        ),
    );
    config.insert(
        "path_probabilities".into(),
        Value::Object(probabilities.clone()),
    );
    config.insert(
        "path_probability_sources".into(),
        Value::Object(sources.clone()),
    );
    config.insert("path_statuses".into(), Value::Object(statuses.clone()));
    config.insert("path_weights".into(), Value::Object(weights.clone()));
    let mut provenance = Map::new();
    provenance.insert(
        "prediction_config_sha256".into(),
        Value::String(canonical_sha256(&Value::Object(config))),
    );
    provenance.insert("path_probabilities".into(), Value::Object(probabilities));
    provenance.insert("path_probability_sources".into(), Value::Object(sources));
    provenance.insert("path_statuses".into(), Value::Object(statuses));
    provenance.insert("path_weights".into(), Value::Object(weights));
    provenance.insert("validation_sha256".into(), Value::Object(fingerprints));
    provenance.insert("model_versions".into(), Value::Object(versions));
    provenance.insert("feature_sets".into(), Value::Object(features));
    provenance.insert(
        "training_boundaries".into(),
        Value::Object(training_boundaries),
    );
    provenance.insert(
        "calibration_states".into(),
        Value::Object(calibration_states),
    );
    Value::Object(provenance)
}

fn forward_eligibility(
    recorded_at: DateTime<Utc>,
    decision_at: Option<DateTime<Utc>>,
    decision_session: Option<&Value>,
    horizon: i64,
    sessions: Option<&[Value]>,
) -> Value {
    let mut maturity: Option<DateTime<Utc>> = None;
    let mut maturity_basis = "unavailable";
    let rows: Vec<&Value> = sessions
        .unwrap_or(&[])
        .iter()
        .filter(|row| row.is_object())
        .collect();
    let ordered: Vec<String> = rows
        .iter()
        .map(|row| {
            row.get("date")
                .map(python_str)
                .unwrap_or_else(|| "None".into())
                .chars()
                .take(10)
                .collect()
        })
        .collect();
    if let Some(Value::String(decision)) = decision_session {
        if let Some(position) = ordered.iter().position(|date| date == decision) {
            let index = position as i64 + horizon;
            if index < rows.len() as i64 {
                let close_at = parse_ledger_time(rows[index as usize].get("close_at"));
                if let Some(close_at) = close_at {
                    maturity = Some(close_at);
                    maturity_basis = "exchange_calendar_target_session_close";
                }
            }
        }
    }
    if maturity.is_none() {
        if let Some(decision_at) = decision_at {
            maturity = Some(decision_at + Duration::days(horizon_calendar_days(horizon)));
            maturity_basis = "calendar_day_approximation";
        } else if let Some(Value::String(decision)) = decision_session {
            let session_end =
                parse_ledger_time(Some(&Value::String(format!("{decision}T23:59:59Z"))));
            if let Some(session_end) = session_end {
                maturity = Some(session_end + Duration::days(horizon_calendar_days(horizon) + 2));
                maturity_basis = "calendar_day_approximation";
            }
        }
    }
    let Some(maturity) = maturity else {
        let mut result = Map::new();
        result.insert("prospective_eligible".into(), Value::Bool(false));
        result.insert(
            "reason".into(),
            Value::String("decision_time_unavailable".into()),
        );
        result.insert("label_maturity_at_utc".into(), Value::Null);
        result.insert(
            "maturity_basis".into(),
            Value::String(maturity_basis.into()),
        );
        result.insert("recorded_at_utc".into(), Value::String(iso(recorded_at)));
        result.insert("recorded_before_label_maturity".into(), Value::Bool(false));
        result.insert("recorded_promptly".into(), Value::Bool(false));
        result.insert("prompt_deadline_at_utc".into(), Value::Null);
        result.insert(
            "rule".into(),
            Value::String(PROSPECTIVE_RECORDING_RULE.into()),
        );
        return Value::Object(result);
    };
    let deadline = decision_at
        .map(|decision_at| decision_at + Duration::hours(PROSPECTIVE_RECORDING_GRACE_HOURS));
    let before_maturity = recorded_at < maturity;
    let prompt = deadline
        .map(|deadline| recorded_at <= deadline)
        .unwrap_or(false);
    let reason = if !before_maturity {
        "recorded_after_label_maturity"
    } else if !prompt {
        "recorded_after_prompt_window"
    } else {
        "recorded_before_label_maturity_within_prompt_window"
    };
    let mut result = Map::new();
    result.insert(
        "prospective_eligible".into(),
        Value::Bool(before_maturity && prompt),
    );
    result.insert("reason".into(), Value::String(reason.into()));
    result.insert("label_maturity_at_utc".into(), Value::String(iso(maturity)));
    result.insert(
        "maturity_basis".into(),
        Value::String(maturity_basis.into()),
    );
    result.insert("recorded_at_utc".into(), Value::String(iso(recorded_at)));
    result.insert(
        "recorded_before_label_maturity".into(),
        Value::Bool(before_maturity),
    );
    result.insert("recorded_promptly".into(), Value::Bool(prompt));
    result.insert(
        "prompt_deadline_at_utc".into(),
        deadline
            .map(|value| Value::String(iso(value)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "recording_grace_hours".into(),
        Value::from(PROSPECTIVE_RECORDING_GRACE_HOURS),
    );
    let recording_lag = decision_at.map(|decision_at| {
        let micros = recorded_at
            .signed_duration_since(decision_at)
            .num_microseconds()
            .unwrap_or(0) as f64;
        py_round(micros / 3_600_000_000.0, 3)
    });
    result.insert(
        "recording_lag_hours".into(),
        recording_lag.map(Value::from).unwrap_or(Value::Null),
    );
    result.insert(
        "rule".into(),
        Value::String(PROSPECTIVE_RECORDING_RULE.into()),
    );
    Value::Object(result)
}

fn first_line(text: &str) -> String {
    text.lines().next().unwrap_or("").to_string()
}

/// Read and verify a ledger file, line by line.
pub fn read_events(path: &Path) -> LedgerResult<Vec<Value>> {
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(path)
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    let mut events: Vec<Value> = Vec::new();
    let mut previous_hash: Option<String> = None;
    for (number, line) in text.split('\n').enumerate() {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if line.trim().is_empty() {
            continue;
        }
        let number = number + 1;
        let event: Value = serde_json::from_str(line).map_err(|error| {
            LedgerError::validation(format!(
                "ledger line {number} is not valid JSON: {}",
                first_line(&error.to_string())
            ))
        })?;
        if !event.is_object() {
            return Err(LedgerError::type_error(format!(
                "ledger line {number} must be a JSON object"
            )));
        }
        let schema = event.get("schema_version");
        let schema_text = schema.map(python_str).unwrap_or_else(|| "None".to_string());
        if !READABLE_SCHEMAS.contains(&schema_text.as_str()) {
            return Err(LedgerError::validation(format!(
                "ledger line {number} has unsupported schema {}",
                schema
                    .map(python_repr)
                    .unwrap_or_else(|| "None".to_string())
            )));
        }
        let event_name = event.get("event");
        let event_text = event
            .get("event")
            .map(python_str)
            .unwrap_or_else(|| "None".to_string());
        if event_text != PREDICTION && event_text != SETTLEMENT {
            return Err(LedgerError::validation(format!(
                "ledger line {number} has unknown event {}",
                event_name
                    .map(python_repr)
                    .unwrap_or_else(|| "None".to_string())
            )));
        }
        let recorded_hash = event
            .get("event_hash")
            .filter(|value| !value.is_null())
            .cloned();
        if let Some(recorded_hash) = recorded_hash {
            let previous = event.get("prev_hash").filter(|value| !value.is_null());
            let previous_matches = match (previous, &previous_hash) {
                (None, None) => true,
                (Some(value), Some(expected)) => python_str(value) == *expected,
                _ => false,
            };
            if !previous_matches {
                return Err(LedgerError::validation(format!(
                    "ledger line {number} breaks the hash chain: prev_hash does not match the previous event"
                )));
            }
            let mut stripped = event.as_object().cloned().unwrap_or_default();
            stripped.remove("event_hash");
            let expected = canonical_sha256(&Value::Object(stripped));
            if expected != python_str(&recorded_hash) {
                return Err(LedgerError::validation(format!(
                    "ledger line {number} failed its own hash check: the event was modified"
                )));
            }
            previous_hash = Some(python_str(&recorded_hash));
        } else {
            previous_hash = None;
        }
        events.push(event);
    }
    Ok(events)
}

fn last_chain_hash(path: &Path) -> Option<String> {
    let mut handle = std::fs::File::open(path).ok()?;
    let size = handle.seek(SeekFrom::End(0)).ok()?;
    if size == 0 {
        return None;
    }
    let block = size.min(65536);
    handle.seek(SeekFrom::Start(size - block)).ok()?;
    let mut buffer = vec![0u8; block as usize];
    handle.read_exact(&mut buffer).ok()?;
    let tail = String::from_utf8_lossy(&buffer);
    let mut lines: Vec<&str> = tail.split('\n').collect();
    if block < size && !lines.is_empty() {
        // The first line of a partial tail is very likely truncated mid-record.
        lines.remove(0);
    }
    for line in lines.iter().rev() {
        let trimmed = line.strip_suffix('\r').unwrap_or(line);
        if trimmed.trim().is_empty() {
            continue;
        }
        return match serde_json::from_str::<Value>(trimmed) {
            Ok(value) => value
                .get("event_hash")
                .filter(|item| !item.is_null())
                .map(python_str),
            Err(_) => None,
        };
    }
    None
}

/// Append one event, continuing the hash chain (`ledger._append`).
pub fn append_event(path: &Path, event: &Value) -> LedgerResult<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| LedgerError::validation(error.to_string()))?;
    }
    let previous_hash = last_chain_hash(path);
    let mut chained = event.as_object().cloned().unwrap_or_default();
    chained.insert(
        "prev_hash".into(),
        previous_hash.map(Value::String).unwrap_or(Value::Null),
    );
    let digest = canonical_sha256(&Value::Object(chained.clone()));
    chained.insert("event_hash".into(), Value::String(digest));
    let line = canonical_text(&Value::Object(chained));
    let mut prefix = String::new();
    if let Ok(metadata) = std::fs::metadata(path) {
        if metadata.len() > 0 {
            let mut handle = std::fs::File::open(path)
                .map_err(|error| LedgerError::validation(error.to_string()))?;
            let mut last = [0u8; 1];
            if handle.seek(SeekFrom::End(-1)).is_ok()
                && handle.read_exact(&mut last).is_ok()
                && last[0] != b'\n'
            {
                prefix.push('\n');
            }
        }
    }
    let mut handle = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    // The legacy module appends through a text-mode handle, so the platform newline is
    // part of the byte contract (CRLF on Windows, LF elsewhere); the chain hash never
    // covers the newline itself.
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    handle
        .write_all(format!("{prefix}{line}{newline}").as_bytes())
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    handle
        .flush()
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    handle
        .sync_all()
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    Ok(())
}

/// Expanding up-rate over labels matured by each decision index.
pub fn causal_base_rates(bars: &[Value], horizon: i64) -> Vec<Option<f64>> {
    let mut closes: Vec<f64> = Vec::with_capacity(bars.len());
    for row in bars {
        let close = row
            .get("close")
            .and_then(|value| match value {
                Value::Number(number) => number.as_f64(),
                Value::String(text) => text.parse::<f64>().ok(),
                _ => None,
            })
            .unwrap_or(f64::NAN);
        closes.push(close);
    }
    let mut up_flags: Vec<i64> = vec![0];
    let steps = (closes.len() as i64 - horizon).max(0);
    for index in 0..steps {
        let index = index as usize;
        let end = index + horizon as usize;
        let added = if closes[index].is_finite()
            && closes[end].is_finite()
            && closes[end] > closes[index]
        {
            1
        } else {
            0
        };
        let previous = up_flags[up_flags.len() - 1];
        up_flags.push(previous + added);
    }
    let mut rates: Vec<Option<f64>> = Vec::with_capacity(closes.len());
    for index in 0..closes.len() {
        let count = index as i64 - horizon + 1;
        if count <= 0 || count >= up_flags.len() as i64 {
            rates.push(None);
            continue;
        }
        rates.push(Some(up_flags[count as usize] as f64 / count as f64));
    }
    rates
}

fn replace_or_push(rows: &mut Vec<(String, Value)>, key: String, value: Value) {
    if let Some(existing) = rows.iter_mut().find(|(existing, _)| existing == &key) {
        existing.1 = value;
    } else {
        rows.push((key, value));
    }
}

/// Append one frozen prediction; the same identity is recorded only once.
pub fn record_prediction(
    result: &Value,
    ledger_path: &Path,
    recorded_at: DateTime<Utc>,
    options: RecordOptions<'_>,
) -> LedgerResult<Value> {
    let snapshot = result.get("snapshot").cloned().unwrap_or(Value::Null);
    let consensus = result.get("consensus").cloned().unwrap_or(Value::Null);
    if !snapshot.is_object()
        || !python_truthy(snapshot.get("ticker"))
        || !python_truthy(snapshot.get("horizon"))
    {
        return Err(LedgerError::validation(
            "result does not contain a frozen snapshot",
        ));
    }
    if snapshot.get("horizon").and_then(Value::as_str) != Some("5D") {
        return Err(LedgerError::validation(
            "the fixed policy ledger only accepts 5D forecasts",
        ));
    }
    if let Some(probability) = consensus.get("prob_up") {
        let valid = number_value(Some(probability))
            .map(|value| value.is_finite() && (0.0..=1.0).contains(&value))
            .unwrap_or(false);
        if !probability.is_null() && !valid {
            return Err(LedgerError::validation(
                "consensus.prob_up must be a finite probability or null",
            ));
        }
    }
    let prediction_id = prediction_id(result);
    let events = read_events(ledger_path)?;
    if events.iter().any(|event| {
        event.get("event").and_then(Value::as_str) == Some(PREDICTION)
            && event.get("prediction_id").and_then(Value::as_str) == Some(prediction_id.as_str())
    }) {
        let mut outcome = Map::new();
        outcome.insert("status".into(), Value::String("duplicate".into()));
        outcome.insert("prediction_id".into(), Value::String(prediction_id));
        outcome.insert(
            "ledger".into(),
            Value::String(ledger_path.to_string_lossy().to_string()),
        );
        return Ok(Value::Object(outcome));
    }
    let ticker = snapshot
        .get("ticker")
        .map(python_str)
        .unwrap_or_else(|| "None".into());
    let market = snapshot
        .get("market")
        .map(python_str)
        .unwrap_or_else(|| "None".into());
    let group_key = ledger_group_key(&ticker, &market, "quant")
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    let timing = consensus
        .get("timing")
        .filter(|value| python_truthy(Some(value)))
        .or_else(|| {
            result
                .get("timing")
                .filter(|value| python_truthy(Some(value)))
        })
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let decision_session = timing
        .get("decision_session")
        .cloned()
        .unwrap_or(Value::Null);
    let same_decision = events.iter().any(|event| {
        event.get("event").and_then(Value::as_str) == Some(PREDICTION)
            && event.get("group_key").and_then(Value::as_str) == Some(group_key.as_str())
            && event.get("decision_session") == Some(&decision_session)
    });
    if same_decision {
        let mut outcome = Map::new();
        outcome.insert("status".into(), Value::String("duplicate".into()));
        outcome.insert(
            "reason".into(),
            Value::String("same_group_and_decision_session".into()),
        );
        outcome.insert(
            "ledger".into(),
            Value::String(ledger_path.to_string_lossy().to_string()),
        );
        return Ok(Value::Object(outcome));
    }
    let mut bars: Vec<Value> = options.bars.map(<[Value]>::to_vec).unwrap_or_default();
    let mut sessions: Vec<Value> = options.sessions.map(<[Value]>::to_vec).unwrap_or_default();
    if bars.is_empty() {
        if let Some(snapshot_dir) = options.snapshot_dir {
            let loaded = crate::snapshot::load_snapshot(snapshot_dir)
                .map_err(|error| LedgerError::validation(error.to_string()))?;
            bars = loaded
                .market
                .bars
                .iter()
                .map(|bar| {
                    serde_json::json!({
                        "date": bar.date,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    })
                })
                .collect();
            if sessions.is_empty() {
                if let Some(calendar) = &loaded.market.calendar {
                    sessions = calendar
                        .sessions
                        .iter()
                        .map(|session| {
                            serde_json::json!({"date": session.date, "close_at": session.close_at})
                        })
                        .collect();
                }
            }
        }
    }
    let decision_at = parse_ledger_time(timing.get("decision_at"))
        .or_else(|| parse_ledger_time(snapshot.get("asof_timestamp")));
    let horizon = horizon_sessions(snapshot.get("horizon"));
    let mut baseline_probability = Value::Null;
    let mut baseline_history_labels = Value::from(0);
    if !bars.is_empty() {
        if let Value::String(decision) = decision_session.clone() {
            if let Some(decision_index) = bars
                .iter()
                .position(|row| row.get("date").and_then(Value::as_str) == Some(decision.as_str()))
            {
                let rates = causal_base_rates(&bars, horizon);
                if let Some(value) = rates.get(decision_index).and_then(|value| *value) {
                    baseline_probability = Value::from(value);
                    baseline_history_labels = Value::from(decision_index as i64 - horizon + 1);
                }
            }
        }
    }
    // The legacy implementation rebinds `sessions` to a dict once bars are present, so
    // the exchange-schedule maturity branch is unreachable in that case; mirror that.
    let forward_sessions: Vec<Value> = if !bars.is_empty() && decision_session.is_string() {
        Vec::new()
    } else {
        sessions.clone()
    };
    let forward = forward_eligibility(
        recorded_at,
        decision_at,
        Some(&decision_session),
        horizon,
        Some(&forward_sessions),
    );
    let prospective_eligible = forward
        .get("prospective_eligible")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mut import_reason = options.import_reason.map(ToString::to_string);
    if !prospective_eligible && import_reason.is_none() {
        let reason = forward
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        import_reason = Some(
            match reason.as_str() {
                "recorded_after_label_maturity" => {
                    "historical_snapshot_recorded_after_label_maturity"
                }
                "recorded_after_prompt_window" => {
                    "recorded_after_prompt_window_not_forward_evidence"
                }
                "decision_time_unavailable" => "decision_time_unavailable",
                _ => "not_prospective",
            }
            .to_string(),
        );
    }
    let sampling = consensus
        .get("sampling")
        .filter(|value| python_truthy(Some(value)))
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let identity =
        model_identity("quant").map_err(|error| LedgerError::validation(error.to_string()))?;
    let production = consensus
        .get("production")
        .filter(|value| python_truthy(Some(value)))
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let probability = consensus.get("prob_up").cloned().unwrap_or(Value::Null);
    let status = if probability.is_null() {
        "abstained"
    } else {
        "pending"
    };
    let sampling_status = match sampling
        .get("status")
        .filter(|value| python_truthy(Some(value)))
    {
        Some(value) => value.clone(),
        None => {
            if !matches!(consensus.get("decision_sampled"), None | Some(Value::Null)) {
                Value::String("anchored".into())
            } else {
                Value::String("unanchored_calendar".into())
            }
        }
    };
    let mut record = Map::new();
    record.insert(
        "schema_version".into(),
        Value::String(SCHEMA_VERSION.into()),
    );
    record.insert("event".into(), Value::String(PREDICTION.into()));
    record.insert("prediction_id".into(), Value::String(prediction_id.clone()));
    record.insert("recorded_at_utc".into(), Value::String(iso(recorded_at)));
    record.insert("status".into(), Value::String(status.into()));
    record.insert("ticker".into(), opt_value(snapshot.get("ticker")));
    record.insert("market".into(), opt_value(snapshot.get("market")));
    record.insert("horizon".into(), opt_value(snapshot.get("horizon")));
    record.insert("horizon_sessions".into(), Value::from(horizon));
    record.insert(
        "asof_timestamp".into(),
        opt_value(snapshot.get("asof_timestamp")),
    );
    record.insert("snapshot_id".into(), opt_value(snapshot.get("snapshot_id")));
    record.insert(
        "snapshot_content_sha256".into(),
        opt_value(snapshot.get("snapshot_content_sha256")),
    );
    record.insert("decision_session".into(), decision_session.clone());
    record.insert("decision_at".into(), opt_value(timing.get("decision_at")));
    record.insert(
        "label_basis".into(),
        timing
            .get("label_basis")
            .cloned()
            .unwrap_or_else(|| Value::String("close_to_close".into())),
    );
    record.insert(
        "entry_basis".into(),
        timing
            .get("entry_basis")
            .cloned()
            .unwrap_or_else(|| Value::String("not_modelled".into())),
    );
    record.insert("prob_up".into(), probability.clone());
    record.insert(
        "raw_probability".into(),
        opt_value(consensus.get("raw_probability")),
    );
    record.insert("calibrated_probability".into(), Value::Null);
    record.insert(
        "calibration_status".into(),
        Value::String("disabled_by_policy".into()),
    );
    record.insert(
        "probability_basis".into(),
        match consensus
            .get("probability_label")
            .filter(|value| python_truthy(Some(value)))
        {
            Some(value) => value.clone(),
            None => Value::String("uncalibrated_model_estimate".into()),
        },
    );
    record.insert("direction".into(), opt_value(consensus.get("direction")));
    record.insert("confidence".into(), Value::Null);
    record.insert("confidence_score".into(), Value::Null);
    record.insert("confidence_score_deprecated".into(), Value::Bool(true));
    record.insert("predictive_evidence_grade".into(), Value::Null);
    record.insert(
        "eligibility".into(),
        opt_value(consensus.get("eligibility")),
    );
    record.insert(
        "policy_version".into(),
        Value::String(sd_core::policy::POLICY_VERSION.into()),
    );
    record.insert(
        "model_version".into(),
        opt_value(identity.get("model_version")),
    );
    record.insert(
        "feature_version".into(),
        opt_value(identity.get("feature_version")),
    );
    record.insert(
        "target_version".into(),
        Value::String(TARGET_VERSION.into()),
    );
    record.insert("group_key".into(), Value::String(group_key));
    record.insert(
        "probability_transform".into(),
        opt_value(production.get("probability_transform")),
    );
    record.insert(
        "causal_base_rate".into(),
        opt_value(production.get("causal_base_rate")),
    );
    record.insert(
        "decision_sampled".into(),
        opt_value(consensus.get("decision_sampled")),
    );
    record.insert("sampling_status".into(), sampling_status);
    record.insert(
        "production_probability_eligible".into(),
        opt_value(consensus.get("production_probability_eligible")),
    );
    record.insert(
        "engine_version".into(),
        opt_value(
            result
                .get("engine")
                .and_then(|engine| engine.get("version")),
        ),
    );
    record.insert("forward_eligibility".into(), forward);
    record.insert(
        "prospective_eligible".into(),
        Value::Bool(prospective_eligible),
    );
    record.insert(
        "import_reason".into(),
        if prospective_eligible {
            Value::Null
        } else {
            import_reason.map(Value::String).unwrap_or(Value::Null)
        },
    );
    record.insert("model_provenance".into(), model_provenance(result));
    record.insert("baseline_probability".into(), baseline_probability.clone());
    record.insert(
        "baseline_method".into(),
        if baseline_probability.is_null() {
            Value::Null
        } else {
            Value::String(BASELINE_RULE.into())
        },
    );
    record.insert("baseline_history_labels".into(), baseline_history_labels);
    record.insert(
        "baseline_frozen_at".into(),
        if baseline_probability.is_null() {
            Value::Null
        } else {
            Value::String("prediction".into())
        },
    );
    append_event(ledger_path, &Value::Object(record))?;
    let mut outcome = Map::new();
    outcome.insert("status".into(), Value::String("recorded".into()));
    outcome.insert("prediction_id".into(), Value::String(prediction_id));
    outcome.insert(
        "prospective_eligible".into(),
        Value::Bool(prospective_eligible),
    );
    outcome.insert(
        "ledger".into(),
        Value::String(ledger_path.to_string_lossy().to_string()),
    );
    Ok(Value::Object(outcome))
}

fn float_close(row: Option<&Value>) -> Option<f64> {
    let row = row?;
    match row.get("close")? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }
}

/// Return settlement events for matured predictions, plus skip reasons.
#[allow(clippy::too_many_arguments)]
pub fn settle_pending(
    events: &[Value],
    bars: &[Value],
    ticker: &str,
    market: &str,
    horizon: Option<&str>,
    source_snapshot_id: Option<&str>,
    source_snapshot_sha256: Option<&str>,
    settled_at: DateTime<Utc>,
) -> LedgerResult<(Vec<Value>, Vec<Value>)> {
    if ticker.trim().is_empty() {
        return Err(LedgerError::validation(
            "settle_pending requires the ticker that the bars belong to",
        ));
    }
    if market.trim().is_empty() {
        return Err(LedgerError::validation(
            "settle_pending requires the market that the bars belong to",
        ));
    }
    let mut predictions: Vec<(String, Value)> = Vec::new();
    let mut settled: HashSet<String> = HashSet::new();
    for event in events {
        let Some(prediction_id) = event.get("prediction_id").and_then(Value::as_str) else {
            continue;
        };
        match event.get("event").and_then(Value::as_str) {
            Some(PREDICTION) => {
                replace_or_push(&mut predictions, prediction_id.to_string(), event.clone())
            }
            Some(SETTLEMENT) => {
                settled.insert(prediction_id.to_string());
            }
            _ => {}
        }
    }
    let mut sessions: HashMap<String, usize> = HashMap::new();
    for (index, row) in bars.iter().enumerate() {
        if let Some(date) = row.get("date").and_then(Value::as_str) {
            sessions.entry(date.to_string()).or_insert(index);
        }
    }
    let mut appended: Vec<Value> = Vec::new();
    let mut skipped: Vec<Value> = Vec::new();
    let mut base_rates: HashMap<i64, Vec<Option<f64>>> = HashMap::new();
    for (prediction_id, prediction) in predictions {
        if settled.contains(&prediction_id) {
            continue;
        }
        if prediction.get("ticker").map(python_str).as_deref() != Some(ticker) {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "ticker_mismatch",
                "prediction_ticker": opt_value(prediction.get("ticker")),
                "bars_ticker": ticker,
            }));
            continue;
        }
        if prediction.get("market").map(python_str).as_deref() != Some(market) {
            let reason = if python_truthy(prediction.get("market")) {
                "market_mismatch"
            } else {
                "prediction_market_missing"
            };
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": reason,
                "prediction_market": opt_value(prediction.get("market")),
                "bars_market": market,
            }));
            continue;
        }
        if let Some(horizon) = horizon {
            if prediction.get("horizon").and_then(Value::as_str) != Some(horizon) {
                continue;
            }
        }
        let label_basis = prediction.get("label_basis");
        if !matches!(label_basis, None | Some(Value::Null))
            && label_basis.and_then(Value::as_str) != Some("close_to_close")
        {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "unsupported_label_basis",
            }));
            continue;
        }
        let decision = prediction.get("decision_session").and_then(Value::as_str);
        let index = decision.and_then(|decision| sessions.get(decision).copied());
        let mut horizon_sessions = prediction
            .get("horizon_sessions")
            .and_then(Value::as_i64)
            .filter(|_| !matches!(prediction.get("horizon_sessions"), Some(Value::Bool(_))));
        if horizon_sessions.is_none() {
            // Ledger-1.0 rows have no explicit session count; derive it from the label.
            horizon_sessions = Some(self::horizon_sessions(prediction.get("horizon")));
        }
        let Some(index) = index else {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "decision_session_not_in_snapshot",
            }));
            continue;
        };
        let Some(horizon_sessions) = horizon_sessions.filter(|value| *value >= 1) else {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "invalid_horizon_sessions",
            }));
            continue;
        };
        let label_index = index as i64 + horizon_sessions;
        if label_index >= bars.len() as i64 {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "label_not_matured",
            }));
            continue;
        }
        let start = float_close(bars.get(index));
        let end = float_close(bars.get(label_index as usize));
        let (Some(start), Some(end)) = (start, end) else {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "invalid_close",
            }));
            continue;
        };
        if !(start.is_finite() && end.is_finite() && start > 0.0 && end > 0.0) {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "invalid_close",
            }));
            continue;
        }
        let Some(probability) = prediction.get("prob_up").and_then(Value::as_f64) else {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "prediction_abstained",
            }));
            continue;
        };
        if matches!(prediction.get("prob_up"), Some(Value::Bool(_))) {
            skipped.push(serde_json::json!({
                "prediction_id": prediction_id,
                "reason": "prediction_abstained",
            }));
            continue;
        }
        let realized = end / start - 1.0;
        let hit = if realized > 0.0 { 1 } else { 0 };
        let mut baseline = prediction
            .get("baseline_probability")
            .filter(|value| !value.is_null())
            .cloned()
            .unwrap_or(Value::Null);
        let mut baseline_frozen_at = Value::String("prediction".into());
        let mut baseline_history_labels = prediction
            .get("baseline_history_labels")
            .cloned()
            .unwrap_or(Value::from(0));
        if baseline.is_null() {
            let rates = base_rates
                .entry(horizon_sessions)
                .or_insert_with(|| causal_base_rates(bars, horizon_sessions));
            baseline = rates
                .get(index)
                .and_then(|value| *value)
                .map(Value::from)
                .unwrap_or(Value::Null);
            baseline_frozen_at = Value::String("settlement".into());
            baseline_history_labels = if baseline.is_null() {
                Value::from(0)
            } else {
                Value::from(index as i64 - horizon_sessions + 1)
            };
        }
        let baseline_number = baseline.as_f64();
        let mut settlement = Map::new();
        settlement.insert(
            "schema_version".into(),
            Value::String(SCHEMA_VERSION.into()),
        );
        settlement.insert("event".into(), Value::String(SETTLEMENT.into()));
        settlement.insert("prediction_id".into(), Value::String(prediction_id.clone()));
        settlement.insert("settled_at_utc".into(), Value::String(iso(settled_at)));
        settlement.insert(
            "settlement_rule".into(),
            Value::String(SETTLEMENT_RULE.into()),
        );
        settlement.insert("bars_ticker".into(), Value::String(ticker.into()));
        settlement.insert("bars_market".into(), Value::String(market.into()));
        settlement.insert(
            "bars_first_session".into(),
            opt_value(bars.first().and_then(|row| row.get("date"))),
        );
        settlement.insert(
            "bars_last_session".into(),
            opt_value(bars.last().and_then(|row| row.get("date"))),
        );
        settlement.insert("bars_session_count".into(), Value::from(bars.len()));
        settlement.insert(
            "decision_session".into(),
            opt_value(prediction.get("decision_session")),
        );
        settlement.insert(
            "label_session".into(),
            opt_value(bars[label_index as usize].get("date")),
        );
        settlement.insert("entry_close".into(), Value::from(start));
        settlement.insert("exit_close".into(), Value::from(end));
        settlement.insert("realized_return".into(), Value::from(realized));
        settlement.insert("hit".into(), Value::from(hit));
        settlement.insert(
            "brier".into(),
            Value::from((probability - hit as f64) * (probability - hit as f64)),
        );
        settlement.insert("baseline_probability".into(), baseline);
        settlement.insert(
            "baseline_brier".into(),
            baseline_number
                .map(|baseline| Value::from((baseline - hit as f64) * (baseline - hit as f64)))
                .unwrap_or(Value::Null),
        );
        settlement.insert(
            "baseline_method".into(),
            Value::String(BASELINE_RULE.into()),
        );
        settlement.insert("baseline_history_labels".into(), baseline_history_labels);
        settlement.insert("baseline_frozen_at".into(), baseline_frozen_at);
        settlement.insert(
            "probability_source".into(),
            Value::String("recorded_prob_up".into()),
        );
        settlement.insert(
            "source_snapshot_id".into(),
            source_snapshot_id
                .map(|value| Value::String(value.into()))
                .unwrap_or(Value::Null),
        );
        settlement.insert(
            "source_snapshot_sha256".into(),
            source_snapshot_sha256
                .map(|value| Value::String(value.into()))
                .unwrap_or(Value::Null),
        );
        appended.push(Value::Object(settlement));
    }
    Ok((appended, skipped))
}

/// Settle pending predictions against a frozen snapshot and append the settlements.
pub fn settle(
    ledger_path: &Path,
    snapshot_dir: &Path,
    ticker: Option<&str>,
    horizon: Option<&str>,
    settled_at: DateTime<Utc>,
) -> LedgerResult<Value> {
    let loaded = crate::snapshot::load_snapshot(snapshot_dir)
        .map_err(|error| LedgerError::validation(error.to_string()))?;
    let manifest = &loaded.manifest;
    let snapshot_ticker = manifest
        .get("ticker")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let snapshot_market = manifest
        .get("market")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if snapshot_ticker.is_empty() {
        return Err(LedgerError::validation(
            "snapshot manifest has no ticker; refusing to settle without an identity",
        ));
    }
    if snapshot_market.is_empty() {
        return Err(LedgerError::validation(
            "snapshot manifest has no market; refusing to settle without an identity",
        ));
    }
    if let Some(ticker) = ticker {
        if ticker != snapshot_ticker {
            return Err(LedgerError::validation(format!(
                "refusing to settle {ticker} with a snapshot for {snapshot_ticker}; the snapshot identity is authoritative"
            )));
        }
    }
    let bars: Vec<Value> = loaded
        .market
        .bars
        .iter()
        .map(|bar| {
            serde_json::json!({
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            })
        })
        .collect();
    let events = read_events(ledger_path)?;
    let snapshot_id = manifest.get("snapshot_id").and_then(Value::as_str);
    let snapshot_sha = manifest
        .get("snapshot_content_sha256")
        .and_then(Value::as_str);
    let (appended, skipped) = settle_pending(
        &events,
        &bars,
        &snapshot_ticker,
        &snapshot_market,
        horizon,
        snapshot_id,
        snapshot_sha,
        settled_at,
    )?;
    for event in &appended {
        append_event(ledger_path, event)?;
    }
    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String(if appended.is_empty() {
            "no_new_settlements".into()
        } else {
            "settled".into()
        }),
    );
    result.insert("settled".into(), Value::from(appended.len()));
    result.insert("skipped".into(), Value::from(skipped.len()));
    result.insert("skip_reasons".into(), Value::Array(skipped));
    result.insert(
        "source_snapshot_id".into(),
        snapshot_id
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    result.insert("bars_ticker".into(), Value::String(snapshot_ticker));
    result.insert("bars_market".into(), Value::String(snapshot_market));
    Ok(Value::Object(result))
}

/// Aggregate settlement metrics for realised (`prediction`, `settlement`) pairs.
pub fn metrics(rows: &[(Value, Value)]) -> Value {
    let mut result = Map::new();
    result.insert("samples".into(), Value::from(rows.len()));
    if rows.is_empty() {
        for key in [
            "brier",
            "causal_baseline_brier",
            "causal_baseline_samples",
            "brier_skill_vs_causal_baseline",
            "observed_up_rate",
            "ex_post_climatology",
            "ex_post_climatology_brier",
            "direction_accuracy",
            "direction_samples",
            "direction_ties",
            "always_up_accuracy",
            "direction_accuracy_delta_vs_always_up",
            "balanced_accuracy",
            "mean_probability",
            "mean_realized_return",
            "calibrated_predictions",
            "uncalibrated_predictions",
        ] {
            let value = if matches!(
                key,
                "causal_baseline_samples"
                    | "direction_samples"
                    | "direction_ties"
                    | "calibrated_predictions"
                    | "uncalibrated_predictions"
            ) {
                Value::from(0)
            } else {
                Value::Null
            };
            result.insert(key.into(), value);
        }
        return Value::Object(result);
    }
    let outcomes: Vec<i64> = rows
        .iter()
        .map(|(_, settlement)| settlement.get("hit").and_then(Value::as_i64).unwrap_or(0))
        .collect();
    let probabilities: Vec<f64> = rows
        .iter()
        .map(|(prediction, _)| {
            prediction
                .get("prob_up")
                .and_then(Value::as_f64)
                .unwrap_or(0.0)
        })
        .collect();
    let count = rows.len() as f64;
    let brier = python_sum(
        probabilities
            .iter()
            .zip(outcomes.iter())
            .map(|(probability, outcome)| (*probability - *outcome as f64).powi(2)),
    ) / count;
    let observed_up = python_sum(outcomes.iter().map(|outcome| *outcome as f64)) / count;
    let climatology_brier = python_sum(
        outcomes
            .iter()
            .map(|outcome| (observed_up - *outcome as f64).powi(2)),
    ) / count;
    let baseline_pairs: Vec<(f64, i64)> = rows
        .iter()
        .filter_map(|(_, settlement)| {
            let baseline = settlement
                .get("baseline_probability")
                .and_then(Value::as_f64)?;
            Some((
                baseline,
                settlement.get("hit").and_then(Value::as_i64).unwrap_or(0),
            ))
        })
        .collect();
    let baseline_brier = mean_of(
        &baseline_pairs
            .iter()
            .map(|(baseline, outcome)| (*baseline - *outcome as f64).powi(2))
            .collect::<Vec<f64>>(),
    );
    let direction_rows: Vec<(bool, i64)> = probabilities
        .iter()
        .zip(outcomes.iter())
        .filter(|(probability, _)| **probability != 0.5)
        .map(|(probability, outcome)| (*probability > 0.5, *outcome))
        .collect();
    let direction_accuracy = if direction_rows.is_empty() {
        None
    } else {
        let correct = direction_rows
            .iter()
            .filter(|(predicted_up, outcome)| i64::from(*predicted_up) == *outcome)
            .count() as f64;
        Some(correct / direction_rows.len() as f64)
    };
    let always_up = if direction_rows.is_empty() {
        None
    } else {
        Some(
            direction_rows
                .iter()
                .map(|(_, outcome)| *outcome as f64)
                .sum::<f64>()
                / direction_rows.len() as f64,
        )
    };
    let mut recalls: Vec<f64> = Vec::new();
    for label in [0i64, 1] {
        let subset: Vec<&(bool, i64)> = direction_rows
            .iter()
            .filter(|(_, outcome)| *outcome == label)
            .collect();
        if !subset.is_empty() {
            let correct = subset
                .iter()
                .filter(|(predicted_up, outcome)| i64::from(*predicted_up) == *outcome)
                .count() as f64;
            recalls.push(correct / subset.len() as f64);
        }
    }
    let balanced = if recalls.len() == 2 {
        Some(recalls.iter().sum::<f64>() / 2.0)
    } else {
        None
    };
    let mean_probability = python_sum(probabilities.iter().copied()) / count;
    let mean_realized = python_sum(rows.iter().map(|(_, settlement)| {
        settlement
            .get("realized_return")
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
    })) / count;
    let calibrated = rows
        .iter()
        .filter(|(prediction, _)| {
            !matches!(
                prediction.get("calibrated_probability"),
                None | Some(Value::Null)
            )
        })
        .count();
    result.insert("brier".into(), Value::from(py_round(brier, 6)));
    result.insert(
        "causal_baseline_brier".into(),
        baseline_brier
            .map(|value| Value::from(py_round(value, 6)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "causal_baseline_samples".into(),
        Value::from(baseline_pairs.len()),
    );
    result.insert(
        "brier_skill_vs_causal_baseline".into(),
        baseline_brier
            .filter(|value| *value != 0.0)
            .map(|value| Value::from(py_round(1.0 - brier / value, 6)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "observed_up_rate".into(),
        Value::from(py_round(observed_up, 6)),
    );
    result.insert(
        "ex_post_climatology".into(),
        Value::from(py_round(observed_up, 6)),
    );
    result.insert(
        "ex_post_climatology_brier".into(),
        Value::from(py_round(climatology_brier, 6)),
    );
    result.insert(
        "direction_accuracy".into(),
        direction_accuracy
            .map(|value| Value::from(py_round(value, 6)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "direction_samples".into(),
        Value::from(direction_rows.len()),
    );
    result.insert(
        "direction_ties".into(),
        Value::from(rows.len() - direction_rows.len()),
    );
    result.insert(
        "always_up_accuracy".into(),
        always_up
            .map(|value| Value::from(py_round(value, 6)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "direction_accuracy_delta_vs_always_up".into(),
        match (direction_accuracy, always_up) {
            (Some(direction), Some(up)) => Value::from(py_round(direction - up, 6)),
            _ => Value::Null,
        },
    );
    result.insert(
        "balanced_accuracy".into(),
        balanced
            .map(|value| Value::from(py_round(value, 6)))
            .unwrap_or(Value::Null),
    );
    result.insert(
        "mean_probability".into(),
        Value::from(py_round(mean_probability, 6)),
    );
    result.insert(
        "mean_realized_return".into(),
        Value::from(py_round(mean_realized, 6)),
    );
    result.insert("calibrated_predictions".into(), Value::from(calibrated));
    result.insert(
        "uncalibrated_predictions".into(),
        Value::from(rows.len() - calibrated),
    );
    Value::Object(result)
}

fn aggregate_by_key(rows: &[(Value, Value)], key: &str) -> Value {
    let mut values: Vec<String> = rows
        .iter()
        .map(|(prediction, _)| as_text(prediction.get(key)))
        .collect();
    values.sort();
    values.dedup();
    let mut result = Map::new();
    for value in values {
        let selected: Vec<(Value, Value)> = rows
            .iter()
            .filter(|(prediction, _)| as_text(prediction.get(key)) == value)
            .cloned()
            .collect();
        result.insert(value, metrics(&selected));
    }
    Value::Object(result)
}

/// Audit the ledger and return the report block (`ledger.audit`).
///
/// `today` replaces `date.today()` so the 364-day evaluation window is deterministic.
pub fn audit(ledger_path: &Path, today: NaiveDate) -> LedgerResult<Value> {
    let events = read_events(ledger_path)?;
    let mut predictions: Vec<(String, Value)> = Vec::new();
    let mut settlements: Vec<Value> = Vec::new();
    for event in &events {
        let Some(prediction_id) = event.get("prediction_id").and_then(Value::as_str) else {
            continue;
        };
        match event.get("event").and_then(Value::as_str) {
            Some(PREDICTION) => {
                replace_or_push(&mut predictions, prediction_id.to_string(), event.clone())
            }
            Some(SETTLEMENT) => settlements.push(event.clone()),
            _ => {}
        }
    }
    let prediction_by_id: HashMap<String, Value> = predictions.iter().cloned().collect();
    let mut by_prediction: Vec<(String, Vec<Value>)> = Vec::new();
    for event in &settlements {
        let Some(prediction_id) = event.get("prediction_id").and_then(Value::as_str) else {
            continue;
        };
        match by_prediction
            .iter_mut()
            .find(|(existing, _)| existing == prediction_id)
        {
            Some((_, rows)) => rows.push(event.clone()),
            None => by_prediction.push((prediction_id.to_string(), vec![event.clone()])),
        }
    }
    let mut duplicates: Vec<String> = by_prediction
        .iter()
        .filter(|(_, rows)| rows.len() > 1)
        .map(|(prediction_id, _)| prediction_id.clone())
        .collect();
    duplicates.sort();
    let mut realized: Vec<(Value, Value)> = Vec::new();
    for (prediction_id, rows) in &by_prediction {
        let Some(prediction) = prediction_by_id.get(prediction_id) else {
            continue;
        };
        realized.push((prediction.clone(), rows[0].clone()));
    }
    let is_current = |prediction: &Value| {
        prediction.get("schema_version").and_then(Value::as_str) == Some(SCHEMA_VERSION)
    };
    let prospective: Vec<(Value, Value)> = realized
        .iter()
        .filter(|(prediction, _)| {
            is_current(prediction)
                && prediction.get("prospective_eligible") == Some(&Value::Bool(true))
        })
        .cloned()
        .collect();
    let prompt_proven = prospective
        .iter()
        .filter(|(prediction, _)| {
            prediction
                .get("forward_eligibility")
                .and_then(|block| block.get("recorded_promptly"))
                == Some(&Value::Bool(true))
        })
        .count();
    let retrospective: Vec<(Value, Value)> = realized
        .iter()
        .filter(|(prediction, _)| {
            is_current(prediction)
                && prediction.get("prospective_eligible") != Some(&Value::Bool(true))
        })
        .cloned()
        .collect();
    let legacy: Vec<(Value, Value)> = realized
        .iter()
        .filter(|(prediction, _)| !is_current(prediction))
        .cloned()
        .collect();
    let settled_ids: HashSet<String> = by_prediction.iter().map(|(id, _)| id.clone()).collect();
    let pending: Vec<Value> = predictions
        .iter()
        .filter(|(prediction_id, prediction)| {
            !settled_ids.contains(prediction_id)
                && prediction.get("status").and_then(Value::as_str) != Some("abstained")
        })
        .map(|(_, prediction)| prediction.clone())
        .collect();
    let pending_prospective: Vec<Value> = pending
        .iter()
        .filter(|prediction| prediction.get("prospective_eligible") == Some(&Value::Bool(true)))
        .cloned()
        .collect();
    let abstained: Vec<Value> = predictions
        .iter()
        .filter(|(_, prediction)| {
            prediction.get("status").and_then(Value::as_str) == Some("abstained")
        })
        .map(|(_, prediction)| prediction.clone())
        .collect();
    let identity_conflicts: Vec<String> = {
        let mut conflicts: Vec<String> = by_prediction
            .iter()
            .filter(|(prediction_id, rows)| {
                let Some(prediction) = prediction_by_id.get(prediction_id) else {
                    return false;
                };
                let ticker = prediction.get("ticker").cloned().unwrap_or(Value::Null);
                let market = prediction.get("market").cloned().unwrap_or(Value::Null);
                rows.iter().any(|settlement| {
                    let bars_ticker = settlement
                        .get("bars_ticker")
                        .cloned()
                        .unwrap_or(Value::Null);
                    let bars_market = settlement
                        .get("bars_market")
                        .cloned()
                        .unwrap_or(Value::Null);
                    !(bars_ticker.is_null() || bars_ticker == ticker)
                        || !(bars_market.is_null() || bars_market == market)
                })
            })
            .map(|(prediction_id, _)| prediction_id.clone())
            .collect();
        conflicts.sort();
        conflicts
    };
    let mut seen_dates: HashMap<String, i64> = HashMap::new();
    for (_, prediction) in &predictions {
        let key = format!(
            "{}|{}|{}",
            as_text(prediction.get("ticker")),
            as_text(prediction.get("horizon")),
            as_text(prediction.get("asof_timestamp"))
        );
        *seen_dates.entry(key).or_insert(0) += 1;
    }
    let mut duplicate_dates: Vec<(String, i64)> = seen_dates
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .collect();
    duplicate_dates.sort();
    let settled_forward = prospective.len();
    let coverage = if settled_forward + pending_prospective.len() == 0 {
        None
    } else {
        Some(settled_forward as f64 / (settled_forward + pending_prospective.len()) as f64)
    };
    let cutoff = (today - Duration::days(364)).to_string();
    let recent: Vec<(Value, Value)> = prospective
        .iter()
        .filter(|(prediction, _)| {
            let session = prediction
                .get("decision_session")
                .filter(|value| python_truthy(Some(value)))
                .map(python_str)
                .unwrap_or_default();
            session.as_str() >= cutoff.as_str()
        })
        .cloned()
        .collect();
    let mut report = Map::new();
    report.insert(
        "schema_version".into(),
        Value::String(SCHEMA_VERSION.into()),
    );
    report.insert(
        "ledger".into(),
        Value::String(ledger_path.to_string_lossy().to_string()),
    );
    report.insert("unique_predictions".into(), Value::from(predictions.len()));
    report.insert("settled_predictions".into(), Value::from(realized.len()));
    report.insert(
        "prospective_settled_predictions".into(),
        Value::from(settled_forward),
    );
    report.insert(
        "prospective_prompt_recording_verified".into(),
        Value::from(prompt_proven),
    );
    report.insert(
        "retrospective_settled_predictions".into(),
        Value::from(retrospective.len()),
    );
    report.insert(
        "legacy_schema_settled_predictions".into(),
        Value::from(legacy.len()),
    );
    report.insert("pending_predictions".into(), Value::from(pending.len()));
    report.insert(
        "pending_prospective_predictions".into(),
        Value::from(pending_prospective.len()),
    );
    report.insert("abstained_predictions".into(), Value::from(abstained.len()));
    report.insert(
        "settlement_coverage_prospective".into(),
        coverage
            .map(|value| Value::from(py_round(value, 6)))
            .unwrap_or(Value::Null),
    );
    report.insert(
        "duplicate_settlements".into(),
        Value::Array(duplicates.into_iter().map(Value::String).collect()),
    );
    report.insert(
        "identity_conflicts".into(),
        Value::Array(identity_conflicts.into_iter().map(Value::String).collect()),
    );
    report.insert(
        "duplicate_prediction_dates".into(),
        Value::Array(
            duplicate_dates
                .into_iter()
                .map(|(key, count)| Value::Array(vec![Value::String(key), Value::from(count)]))
                .collect(),
        ),
    );
    report.insert(
        "metrics_basis".into(),
        Value::String("prospective_eligible_predictions_only".into()),
    );
    report.insert("evaluation_window_days".into(), Value::from(364));
    report.insert("evaluation_window_start".into(), Value::String(cutoff));
    report.insert("recent_prospective".into(), metrics(&recent));
    report.insert("overall".into(), metrics(&prospective));
    report.insert("prospective".into(), metrics(&prospective));
    report.insert("retrospective".into(), metrics(&retrospective));
    report.insert("legacy_schema".into(), metrics(&legacy));
    report.insert("all_settled_descriptive".into(), metrics(&realized));
    report.insert(
        "by_horizon".into(),
        aggregate_by_key(&prospective, "horizon"),
    );
    report.insert(
        "by_engine_version".into(),
        aggregate_by_key(&prospective, "engine_version"),
    );
    report.insert(
        "caveats".into(),
        Value::Array(
            [
                "observed_up_rate is the realised label base rate, not a model hit rate",
                "ex_post_climatology is descriptive and must not be used as a deployable baseline",
                "only prospective_eligible predictions enter the forward score",
                "prospective also requires a provable prompt recording timestamp (ledger-3.1); earlier rows are reported separately",
                "legacy ledger-1.0/2.0/3.0 rows cannot prove prompt recording and are reported separately",
            ]
            .into_iter()
            .map(|value| Value::String(value.into()))
            .collect(),
        ),
    );
    Ok(Value::Object(report))
}

/// Matured, prospective, same-identity records for the directional-edge check.
pub fn prospective_records(
    ledger_path: &Path,
    ticker: Option<&str>,
    market: Option<&str>,
) -> LedgerResult<Vec<Value>> {
    let events = read_events(ledger_path)?;
    let mut predictions: Vec<(String, Value)> = Vec::new();
    let mut settlements: Vec<(String, Value)> = Vec::new();
    let mut seen_settlements: HashSet<String> = HashSet::new();
    for event in &events {
        let Some(prediction_id) = event.get("prediction_id").and_then(Value::as_str) else {
            continue;
        };
        match event.get("event").and_then(Value::as_str) {
            Some(PREDICTION) => {
                replace_or_push(&mut predictions, prediction_id.to_string(), event.clone())
            }
            Some(SETTLEMENT) => {
                if seen_settlements.insert(prediction_id.to_string()) {
                    settlements.push((prediction_id.to_string(), event.clone()));
                } else if let Some(existing) = settlements
                    .iter_mut()
                    .find(|(existing, _)| existing == prediction_id)
                {
                    existing.1 = event.clone();
                }
            }
            _ => {}
        }
    }
    let identity =
        model_identity("quant").map_err(|error| LedgerError::validation(error.to_string()))?;
    let policy_version = sd_core::policy::POLICY_VERSION;
    let target_version = TARGET_VERSION;
    let model_version = identity
        .get("model_version")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let feature_version = identity
        .get("feature_version")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let mut rows: Vec<Value> = Vec::new();
    for (prediction_id, settlement) in settlements {
        let Some((_, prediction)) = predictions
            .iter()
            .find(|(existing, _)| existing == &prediction_id)
        else {
            continue;
        };
        if prediction.get("prospective_eligible") != Some(&Value::Bool(true)) {
            continue;
        }
        if prediction.get("policy_version").and_then(Value::as_str) != Some(policy_version)
            || prediction.get("target_version").and_then(Value::as_str) != Some(target_version)
            || prediction.get("model_version").and_then(Value::as_str)
                != Some(model_version.as_str())
            || prediction.get("feature_version").and_then(Value::as_str)
                != Some(feature_version.as_str())
        {
            continue;
        }
        if let Some(ticker) = ticker {
            let candidate = prediction
                .get("ticker")
                .map(python_str)
                .unwrap_or_default()
                .to_uppercase();
            if candidate != ticker.to_uppercase() {
                continue;
            }
        }
        if let Some(market) = market {
            let candidate = prediction
                .get("market")
                .map(python_str)
                .unwrap_or_default()
                .to_uppercase();
            if candidate != market.to_uppercase() {
                continue;
            }
        }
        let transform = prediction
            .get("probability_transform")
            .filter(|value| python_truthy(Some(value)))
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let forward = prediction
            .get("forward_eligibility")
            .cloned()
            .unwrap_or(Value::Object(Map::new()));
        let recording_prompt_proven = forward.get("recorded_promptly") == Some(&Value::Bool(true));
        let mut sampling_status = prediction.get("sampling_status").cloned();
        if sampling_status
            .as_ref()
            .map(|value| value.is_null())
            .unwrap_or(true)
        {
            sampling_status = Some(
                if !matches!(prediction.get("decision_sampled"), None | Some(Value::Null)) {
                    Value::String("anchored".into())
                } else {
                    Value::String("unknown_legacy".into())
                },
            );
        }
        let sampled = prediction.get("decision_sampled") == Some(&Value::Bool(true));
        let evidence_scope = if recording_prompt_proven && sampled {
            "prospective_prompt_recording_anchored"
        } else if recording_prompt_proven {
            "prospective_prompt_recording_unanchored"
        } else {
            "legacy_unverified_recording_time"
        };
        // Python's `dict.get("base_rate_at_origin", fallback)` uses the fallback only
        // when the key is absent; an explicit null must stay null.
        let causal_base_rate = match transform.get("base_rate_at_origin") {
            Some(value) => value.clone(),
            None => opt_value(prediction.get("causal_base_rate")),
        };
        let mut row = Map::new();
        row.insert(
            "decision_session".into(),
            opt_value(prediction.get("decision_session")),
        );
        row.insert("probability".into(), opt_value(prediction.get("prob_up")));
        row.insert("target".into(), opt_value(settlement.get("hit")));
        row.insert("probability_transform".into(), transform);
        row.insert("causal_base_rate".into(), causal_base_rate);
        row.insert(
            "sampled".into(),
            Value::Bool(if recording_prompt_proven {
                sampled
            } else {
                false
            }),
        );
        row.insert(
            "sampling_status".into(),
            sampling_status.unwrap_or(Value::Null),
        );
        row.insert(
            "recording_prompt_proven".into(),
            Value::Bool(recording_prompt_proven),
        );
        row.insert("status".into(), Value::String("matured".into()));
        row.insert(
            "evidence_scope".into(),
            Value::String(evidence_scope.into()),
        );
        row.insert(
            "settled_at".into(),
            opt_value(settlement.get("recorded_at")),
        );
        rows.push(Value::Object(row));
    }
    Ok(rows)
}
