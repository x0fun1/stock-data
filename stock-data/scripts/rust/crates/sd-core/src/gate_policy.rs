//! Central prediction/reporting/strategy gate policy.
//!
//! Port of `quant_research/gate_policy.py`. Data quality and strategy evidence may
//! lower the reporting state without erasing a numerical forecast; only codes with
//! `can_veto_prediction` block one. Unknown codes fail closed as fatal diagnostics.
//!
//! Every code also carries a `failure_mode` so a reader can tell *what kind* of
//! failure it is: `correctness`, `accounting`, `data`, `reporting`, `presentation`.

use serde_json::{Map, Value};

/// Catalog entry: (code, severity, scope, can_veto_prediction, failure_mode override).
pub static REASON_CATALOG: &[(&str, &str, &str, bool, Option<&str>)] = &[
    ("INSUFFICIENT_OHLCV", "fatal", "prediction", true, None),
    ("SEVERE_MISSING_BARS", "fatal", "prediction", true, None),
    ("INVALID_TIME_ORDER", "fatal", "prediction", true, None),
    (
        "UNRECOVERABLE_DUPLICATE_BARS",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("INVALID_PRICE_VALUES", "fatal", "prediction", true, None),
    ("LATEST_PRICE_CONFLICT", "fatal", "prediction", true, None),
    (
        "LATEST_OBSERVATION_UNRESOLVED",
        "fatal",
        "prediction",
        true,
        None,
    ),
    (
        "CORPORATE_ACTION_CONTAMINATION",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("LEAKAGE_DETECTED", "fatal", "prediction", true, None),
    ("PREDICTION_FAILED", "fatal", "prediction", true, None),
    ("SOURCE_IDENTITY_UNVERIFIED", "fatal", "data", true, None),
    ("SOURCE_IDENTITY_CONFLICT", "fatal", "data", true, None),
    ("FREQUENCY_UNVERIFIED", "fatal", "data", true, None),
    ("TEMPORAL_PROVENANCE_MISSING", "fatal", "data", true, None),
    ("MARKET_UNITS_UNVERIFIED", "degraded", "data", false, None),
    ("MARKET_UNITS_CONFLICT", "fatal", "data", true, None),
    ("STALE_MARKET_DATA", "fatal", "data", true, None),
    ("DESCRIPTIVE_REQUEST", "fatal", "reporting", true, None),
    ("SNAPSHOT_IDENTITY_INVALID", "fatal", "data", true, None),
    ("CALENDAR_OFFICIAL_MISSING", "degraded", "data", false, None),
    ("CALENDAR_FALLBACK_USED", "degraded", "data", false, None),
    ("CALENDAR_SOURCE_INVALID", "degraded", "data", false, None),
    ("ADJUSTMENT_UNKNOWN", "degraded", "data", false, None),
    (
        "ADJUSTMENT_EVIDENCE_UNAVAILABLE",
        "degraded",
        "data",
        false,
        None,
    ),
    (
        "ADJUSTMENT_EVIDENCE_CONFLICT",
        "degraded",
        "data",
        false,
        None,
    ),
    ("CORPORATE_ACTION_UNKNOWN", "degraded", "data", false, None),
    (
        "LATEST_CLOSE_SINGLE_PROVIDER",
        "warning",
        "data",
        false,
        None,
    ),
    ("LATEST_CLOSE_CROSS_VERIFIED", "info", "data", false, None),
    ("ASSET_TYPE_UNVERIFIED", "degraded", "data", false, None),
    (
        "EXTREME_PRICE_MOVE_UNRECONCILED",
        "degraded",
        "data",
        false,
        None,
    ),
    ("OOS_INSUFFICIENT", "degraded", "validation", false, None),
    (
        "OOS_EVIDENCE_INVALID",
        "degraded",
        "validation",
        false,
        None,
    ),
    ("DATA_QUALITY_LOW", "degraded", "data", false, None),
    ("OOS_MARKER_MISSING", "degraded", "validation", false, None),
    (
        "OOS_SELF_REPORT_MISMATCH",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "SURVIVORSHIP_NOT_ASSESSED",
        "warning",
        "strategy",
        false,
        None,
    ),
    ("CALIBRATION_MISSING", "degraded", "validation", false, None),
    (
        "FACTOR_BACKTEST_INSUFFICIENT",
        "warning",
        "validation",
        false,
        None,
    ),
    (
        "LOW_FACTOR_AGREEMENT",
        "degraded",
        "validation",
        false,
        None,
    ),
    ("PBO_NOT_ASSESSED", "warning", "strategy", false, None),
    ("DSR_NOT_ASSESSED", "warning", "strategy", false, None),
    (
        "TRANSACTION_COST_NOT_ASSESSED",
        "warning",
        "strategy",
        false,
        None,
    ),
    (
        "PORTFOLIO_BACKTEST_MISSING",
        "warning",
        "strategy",
        false,
        None,
    ),
    (
        "LEAKAGE_AUDIT_INCOMPLETE",
        "degraded",
        "validation",
        false,
        None,
    ),
    ("PATH_FAILED", "warning", "validation", false, None),
    ("DATA_WARNING", "warning", "data", false, None),
    ("PATH_WARNING", "warning", "validation", false, None),
    ("NEWS_NOT_ASSESSED", "warning", "news", false, None),
    ("POLICY_VERSION_MISMATCH", "fatal", "prediction", true, None),
    ("FIXED_WINDOW_CONFLICT", "fatal", "prediction", true, None),
    (
        "INSUFFICIENT_HISTORY_COVERAGE",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("PRICE_BASIS_UNVERIFIED", "fatal", "prediction", true, None),
    ("OHLCV_OUT_OF_WINDOW", "fatal", "prediction", true, None),
    (
        "DECISION_SESSION_UNRESOLVED",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("CALENDAR_UNVERIFIED", "degraded", "data", false, None),
    ("POINT_IN_TIME_UNVERIFIED", "degraded", "data", false, None),
    (
        "OOS_SUPPORT_INSUFFICIENT",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "NO_VALID_OOS_PROBABILITY",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "OOS_INTEGRITY_FAILED",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "PROSPECTIVE_SUPPORT_INSUFFICIENT",
        "warning",
        "validation",
        false,
        None,
    ),
    (
        "DIRECTIONAL_EDGE_UNPROVEN",
        "info",
        "validation",
        false,
        None,
    ),
    (
        "CALIBRATION_DISABLED_BY_POLICY",
        "info",
        "validation",
        false,
        None,
    ),
    (
        "PRODUCTION_PATH_UNAVAILABLE",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "PREDICTION_INTEGRITY_FAILED",
        "fatal",
        "prediction",
        true,
        None,
    ),
    (
        "PRODUCTION_PROBABILITY_LEAKED",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("AGGREGATION_CONTRACT", "fatal", "prediction", true, None),
    (
        "CONFIDENCE_SCORE_PRESENT",
        "fatal",
        "prediction",
        true,
        None,
    ),
    ("SHADOW_PATH_PROMOTED", "fatal", "prediction", true, None),
    (
        "SHADOW_PROBABILITY_INVALID",
        "fatal",
        "prediction",
        true,
        None,
    ),
    (
        "DIAGNOSTIC_EMITTED_PROBABILITY",
        "fatal",
        "prediction",
        true,
        None,
    ),
    (
        "QUALIFICATION_CONTRACT_VIOLATION",
        "fatal",
        "prediction",
        true,
        None,
    ),
    (
        "TARGET_SESSION_UNRESOLVED",
        "fatal",
        "prediction",
        true,
        Some("correctness"),
    ),
    (
        "REPLAY_SUPPORT_INSUFFICIENT",
        "degraded",
        "validation",
        false,
        None,
    ),
    ("INTERVAL_BELOW_ZERO", "degraded", "validation", false, None),
    ("INTERVAL_CROSSES_ZERO", "info", "validation", false, None),
    (
        "INTERVAL_UNESTIMABLE",
        "degraded",
        "validation",
        false,
        None,
    ),
    (
        "SAMPLING_PHASE_UNANCHORED",
        "info",
        "validation",
        false,
        Some("accounting"),
    ),
    (
        "EVIDENCE_FLOOR_POWER_LOW",
        "info",
        "validation",
        false,
        Some("accounting"),
    ),
    (
        "LABEL_BASIS_DIVIDENDS_EXCLUDED",
        "info",
        "data",
        false,
        Some("data"),
    ),
    (
        "NEWS_PARTIAL_EVIDENCE",
        "warning",
        "news",
        false,
        Some("reporting"),
    ),
];

/// Scope-level failure-mode defaults; per-code overrides live in the catalog.
pub fn failure_mode_for_scope(scope: &str) -> &'static str {
    match scope {
        "prediction" => "correctness",
        "data" => "data",
        "validation" | "strategy" => "accounting",
        "news" | "reporting" => "reporting",
        _ => "correctness",
    }
}

/// Policy metadata for one reason code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReasonPolicy {
    pub severity: &'static str,
    pub scope: &'static str,
    pub can_veto_prediction: bool,
    pub failure_mode: &'static str,
}

/// Policy for a code; unknown codes fail closed as fatal diagnostics.
pub fn reason_policy(code: &str) -> ReasonPolicy {
    match REASON_CATALOG.iter().find(|entry| entry.0 == code) {
        Some((_, severity, scope, can_veto, failure_mode)) => ReasonPolicy {
            severity,
            scope,
            can_veto_prediction: *can_veto,
            failure_mode: failure_mode.unwrap_or_else(|| failure_mode_for_scope(scope)),
        },
        None => ReasonPolicy {
            severity: "fatal",
            scope: "unknown",
            can_veto_prediction: true,
            failure_mode: failure_mode_for_scope("unknown"),
        },
    }
}

/// Serialisable policy metadata (`reason_policy`).
pub fn reason_policy_value(code: &str) -> Value {
    let policy = reason_policy(code);
    let mut map = Map::new();
    map.insert("severity".into(), Value::String(policy.severity.into()));
    map.insert("scope".into(), Value::String(policy.scope.into()));
    map.insert(
        "can_veto_prediction".into(),
        Value::Bool(policy.can_veto_prediction),
    );
    map.insert(
        "failure_mode".into(),
        Value::String(policy.failure_mode.into()),
    );
    Value::Object(map)
}

/// Classification of an ordered reason-code list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Classification {
    pub status: &'static str,
    pub reason_codes: Vec<String>,
    pub blockers: Vec<String>,
    pub warnings: Vec<String>,
    pub informational: Vec<String>,
    pub prediction_eligible: bool,
}

/// Classify reason codes, preserving first-occurrence order.
pub fn classify_reasons<I, S>(codes: I) -> Classification
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut unique: Vec<String> = Vec::new();
    for code in codes {
        let code = code.as_ref().to_string();
        if !unique.contains(&code) {
            unique.push(code);
        }
    }
    let mut blockers = Vec::new();
    let mut nonfatal = Vec::new();
    for code in &unique {
        if reason_policy(code).can_veto_prediction {
            blockers.push(code.clone());
        } else {
            nonfatal.push(code.clone());
        }
    }
    let mut warnings = Vec::new();
    let mut informational = Vec::new();
    for code in &nonfatal {
        match reason_policy(code).severity {
            "warning" | "degraded" => warnings.push(code.clone()),
            "info" => informational.push(code.clone()),
            _ => {}
        }
    }
    let requires_degraded = nonfatal.iter().any(|code| {
        matches!(reason_policy(code).severity, "warning" | "degraded")
            && reason_policy(code).scope != "strategy"
    });
    let status = if !blockers.is_empty() {
        "veto"
    } else if requires_degraded {
        "degraded"
    } else {
        "pass"
    };
    Classification {
        status,
        prediction_eligible: blockers.is_empty(),
        reason_codes: unique,
        blockers,
        warnings,
        informational,
    }
}

impl Classification {
    /// Legacy dict shape and key order (`classify_reasons`).
    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert("status".into(), Value::String(self.status.into()));
        map.insert("reason_codes".into(), strings(&self.reason_codes));
        map.insert("blockers".into(), strings(&self.blockers));
        map.insert("warnings".into(), strings(&self.warnings));
        map.insert("informational".into(), strings(&self.informational));
        map.insert(
            "prediction_eligible".into(),
            Value::Bool(self.prediction_eligible),
        );
        Value::Object(map)
    }
}

fn strings(values: &[String]) -> Value {
    Value::Array(
        values
            .iter()
            .map(|value| Value::String(value.clone()))
            .collect(),
    )
}

/// Resolve the final reporting state; strategy-only warnings cannot veto.
pub fn reporting_status(
    prediction_available: bool,
    veto_codes: &[String],
    warning_codes: &[String],
) -> &'static str {
    let mut all: Vec<String> = veto_codes.to_vec();
    all.extend(warning_codes.iter().cloned());
    let classified = classify_reasons(all);
    if !prediction_available && classified.blockers.is_empty() {
        return "veto";
    }
    classified.status
}

/// Derive the analysis status from stage outcomes only.
///
/// The report embeds this value and the validator re-derives it from the frozen
/// stages, so an edited status field cannot claim a state the stages never reached.
pub fn analysis_status(
    may_report_direction: bool,
    reporting_state: &str,
    researcher_statuses: &[String],
    news_status: &str,
) -> &'static str {
    if !may_report_direction {
        return "abstain";
    }
    let researcher_ok = researcher_statuses
        .iter()
        .all(|status| matches!(status.as_str(), "success" | "partial"));
    if reporting_state == "degraded" || !researcher_ok || news_status != "complete" {
        return "degraded";
    }
    "complete_with_limitations"
}

/// Grade used when an independent edge is supported.
pub const POSITIVE_SKILL_GRADE: &str = "positive_skill_with_estimated_interval_excluding_zero";

/// Split probability reportability, direction reportability and trading signal.
pub fn build_report_eligibility(consensus: &Value, adversarial: &Value) -> Value {
    let probability = consensus
        .get("reportable_probability")
        .or_else(|| consensus.get("prob_up"));
    let probability_ok = matches!(probability, Some(Value::Number(_)));
    let direction_ok = adversarial
        .get("may_report_direction")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && probability_ok;
    let edge_state = consensus
        .get("directional_edge_state")
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .unwrap_or("insufficient_evidence");
    let independent = consensus
        .get("directional_edge_supported")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let grade = if independent {
        POSITIVE_SKILL_GRADE.to_string()
    } else if edge_state != "insufficient_evidence" {
        format!("replay_only_{edge_state}")
    } else {
        "insufficient_oos_evidence".to_string()
    };
    let mut reasons: Vec<String> = vec![
        "entry_basis_not_modelled".to_string(),
        "transaction_costs_not_modelled".to_string(),
    ];
    if grade != POSITIVE_SKILL_GRADE {
        reasons.push("no_independent_edge_vs_expanding_base_rate".to_string());
    }
    if !probability_ok {
        reasons.push("probability_not_reportable".to_string());
    }
    if !direction_ok {
        reasons.push("direction_not_reportable".to_string());
    }
    let mut map = Map::new();
    map.insert("probability_report".into(), Value::Bool(probability_ok));
    map.insert("direction_report".into(), Value::Bool(direction_ok));
    map.insert("trading_signal".into(), Value::Bool(false));
    map.insert("trading_signal_reasons".into(), strings(&reasons));
    map.insert("predictive_evidence_grade".into(), Value::String(grade));
    map.insert(
        "directional_edge_state".into(),
        Value::String(edge_state.to_string()),
    );
    map.insert(
        "independent_edge_supported".into(),
        Value::Bool(independent),
    );
    Value::Object(map)
}

fn python_round4(value: f64) -> f64 {
    // Python's round(x, 4) is half-to-even; the rates here are k/n for small n, and
    // a tie at the 4th decimal is converted with the same tie rule.
    let scaled = value * 10_000.0;
    let floor = scaled.floor();
    let fraction = scaled - floor;
    let rounded = if (fraction - 0.5).abs() < f64::EPSILON {
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        scaled.round()
    };
    rounded / 10_000.0
}

/// Aggregate per-analysis gate rows (compatibility helper; the runtime reads the
/// frozen consensus/audit blocks directly).
pub fn aggregate_gate_telemetry(records: &[Value]) -> Value {
    let total = records.len();
    let mut map = Map::new();
    if total == 0 {
        map.insert("sample_count".into(), Value::from(0));
        map.insert("prediction_availability_rate".into(), Value::Null);
        map.insert("abstain_rate".into(), Value::Null);
        map.insert("degraded_rate".into(), Value::Null);
        map.insert("pass_rate".into(), Value::Null);
        map.insert("scope".into(), Value::String("no samples".into()));
        return Value::Object(map);
    }
    let available = records
        .iter()
        .filter(|row| {
            row.get("prediction_available")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        })
        .count();
    let degraded = records
        .iter()
        .filter(|row| row.get("reporting_status").and_then(Value::as_str) == Some("degraded"))
        .count();
    let passed = records
        .iter()
        .filter(|row| row.get("reporting_status").and_then(Value::as_str) == Some("pass"))
        .count();
    let rate = |count: usize| Value::from(python_round4(count as f64 / total as f64));
    map.insert("sample_count".into(), Value::from(total));
    map.insert("prediction_availability_rate".into(), rate(available));
    map.insert("abstain_rate".into(), rate(total - available));
    map.insert("degraded_rate".into(), rate(degraded));
    map.insert("pass_rate".into(), rate(passed));
    map.insert(
        "scope".into(),
        Value::String(
            "per-analysis records supplied by caller; not a cross-ticker reliability claim unless sample_count covers that batch"
                .into(),
        ),
    );
    Value::Object(map)
}
