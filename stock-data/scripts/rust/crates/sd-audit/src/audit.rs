//! Adversarial audit of the fixed-policy forecast (`quant_research/audit.py`).
//!
//! One job: decide whether the production probability may be shown as a direction.
//! It does not invent a confidence score, does not grade shadow paths and does not
//! blend probabilities. Every finding is a hard veto, a warning that bounds the claim,
//! or an informational note.
//!
//! This crate deliberately does not depend on `sd-research`: nothing here calls the
//! production estimator, so a mutated estimator is caught instead of reproduced.

use serde_json::{Map, Value};

use sd_core::contracts::py_str;
use sd_core::policy;
use sd_news::status::{news_assessed, news_status_label};

fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Null) | None => false,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn empty_object<'a>() -> &'a Map<String, Value> {
    static EMPTY: std::sync::OnceLock<Map<String, Value>> = std::sync::OnceLock::new();
    EMPTY.get_or_init(Map::new)
}

fn as_object(value: &Value) -> &Map<String, Value> {
    match value.as_object() {
        Some(map) => map,
        None => empty_object(),
    }
}

fn object<'a>(value: &'a Value, key: &str) -> &'a Map<String, Value> {
    nested(as_object(value), key)
}

fn nested<'a>(map: &'a Map<String, Value>, key: &str) -> &'a Map<String, Value> {
    match map.get(key).and_then(Value::as_object) {
        Some(inner) => inner,
        None => empty_object(),
    }
}

fn value_or_null(value: Option<&Value>) -> Value {
    value.cloned().unwrap_or(Value::Null)
}

fn integer_or_zero(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

fn probability_valid(value: &Value) -> bool {
    match value {
        Value::Number(number) => number
            .as_f64()
            .map(|value| (0.0..=1.0).contains(&value))
            .unwrap_or(false),
        _ => false,
    }
}

struct Findings {
    vetoes: Vec<String>,
    warnings: Vec<String>,
    reason_codes: Vec<String>,
}

impl Findings {
    fn veto(&mut self, code: &str, message: impl Into<String>) {
        self.reason_codes.push(code.to_string());
        self.vetoes.push(message.into());
    }

    fn warn(&mut self, code: &str, message: impl Into<String>) {
        self.reason_codes.push(code.to_string());
        self.warnings.push(message.into());
    }
}

/// Inputs of one audit run.
pub struct AuditInput<'a> {
    /// Frozen snapshot manifest.
    pub manifest: &'a Value,
    /// Researcher results (production plus shadow/diagnostic paths).
    pub researchers: &'a [Value],
    /// Consensus block produced from the researcher results.
    pub consensus: &'a Value,
    /// Optional News artifact observed after the audit is frozen.
    pub news: Option<&'a Value>,
    /// Prospective recording records used only for the evidence warnings.
    pub prospective_records: Option<&'a [Value]>,
}

/// Audit the production probability against the frozen evidence.
pub fn audit(input: &AuditInput<'_>) -> Value {
    let mut findings = Findings {
        vetoes: Vec::new(),
        warnings: Vec::new(),
        reason_codes: Vec::new(),
    };
    let mut checks = Map::new();
    let request = object(input.manifest, "request");
    let snapshot_id = input
        .manifest
        .get("snapshot_id")
        .cloned()
        .unwrap_or(Value::Null);
    let policy_block = object(input.manifest, "policy");
    let gate = object(input.manifest, "data_validation");

    if request.get("intent").and_then(Value::as_str) != Some("forecast") {
        findings.veto(
            "DESCRIPTIVE_REQUEST",
            "Request intent does not authorize a future-direction probability.",
        );
    }
    if !truthy(input.manifest.get("snapshot_id"))
        || !truthy(input.manifest.get("source_payload_sha256"))
    {
        findings.veto(
            "SNAPSHOT_IDENTITY_INVALID",
            "Snapshot identity or source payload digest is missing.",
        );
    }
    for result in input.researchers {
        if result.get("snapshot_id") != Some(&snapshot_id) {
            findings.veto(
                "SNAPSHOT_IDENTITY_INVALID",
                format!(
                    "{} used a different snapshot identity.",
                    py_str(result.get("researcher_id").unwrap_or(&Value::Null))
                ),
            );
        }
    }
    checks.insert("snapshot_identity".into(), Value::String("checked".into()));

    let policy_version = policy_block
        .get("policy_version")
        .cloned()
        .unwrap_or(Value::Null);
    let coverage_status = policy_block
        .get("coverage_status")
        .cloned()
        .unwrap_or(Value::Null);
    let calendar_verified = truthy(policy_block.get("calendar_verified"));
    let policy_ok = policy_version.as_str() == Some(policy::POLICY_VERSION)
        && coverage_status.as_str() == Some("complete")
        && calendar_verified;
    let mut policy_check = Map::new();
    for (key, value) in [
        (
            "policy_version",
            value_or_null(policy_block.get("policy_version")),
        ),
        (
            "coverage_status",
            value_or_null(policy_block.get("coverage_status")),
        ),
        (
            "declared_coverage_status",
            value_or_null(policy_block.get("declared_coverage_status")),
        ),
        (
            "coverage_basis",
            value_or_null(policy_block.get("coverage_basis")),
        ),
        (
            "coverage_recomputed",
            value_or_null(policy_block.get("coverage_recomputed")),
        ),
        (
            "calendar_verified",
            value_or_null(policy_block.get("calendar_verified")),
        ),
        (
            "expected_sessions",
            value_or_null(policy_block.get("expected_sessions")),
        ),
        (
            "observed_sessions",
            value_or_null(policy_block.get("observed_sessions")),
        ),
        (
            "missing_sessions",
            value_or_null(policy_block.get("missing_sessions")),
        ),
    ] {
        policy_check.insert(key.into(), value);
    }
    checks.insert("policy".into(), Value::Object(policy_check));
    if policy_version.as_str() != Some(policy::POLICY_VERSION) {
        findings.veto(
            "POLICY_VERSION_MISMATCH",
            "Snapshot policy version does not match the active fixed-window policy.",
        );
    }
    if coverage_status.as_str() != Some("complete") {
        findings.veto(
            "INSUFFICIENT_HISTORY_COVERAGE",
            format!(
                "52-week coverage is {} (basis {}); expected {} sessions, observed {}, missing {}.",
                match coverage_status.as_str() {
                    Some(value) => value.to_string(),
                    None if coverage_status.is_null() => "missing".to_string(),
                    None => py_str(&coverage_status),
                },
                policy_block
                    .get("coverage_basis")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .unwrap_or("declared_only"),
                py_str(
                    policy_block
                        .get("expected_sessions")
                        .unwrap_or(&Value::Null)
                ),
                py_str(
                    policy_block
                        .get("observed_sessions")
                        .unwrap_or(&Value::Null)
                ),
                py_str(policy_block.get("missing_sessions").unwrap_or(&Value::Null)),
            ),
        );
    }
    if !calendar_verified {
        findings.veto(
            "CALENDAR_UNVERIFIED",
            "Exchange calendar is unverified; a formal 5-session probability cannot be published.",
        );
    }
    if !truthy(policy_block.get("target_session")) {
        findings.veto(
            "TARGET_SESSION_UNRESOLVED",
            "The 5th-session target could not be resolved from the frozen calendar; a formal probability cannot be published against an undetermined close.",
        );
    }
    let price_basis = nested(gate, "policy")
        .get("price_basis")
        .cloned()
        .filter(|value| !value.is_null())
        .or_else(|| policy_block.get("price_basis").cloned())
        .unwrap_or(Value::Null);
    checks.insert("price_basis".into(), price_basis.clone());
    if price_basis.as_str() != Some(policy::PRICE_BASIS) {
        findings.veto(
            "PRICE_BASIS_UNVERIFIED",
            "Price basis is not the declared split-adjusted close.",
        );
    }

    let assessment = object(input.consensus, "assessment");
    let current_integrity = truthy(assessment.get("current_prediction_integrity_pass"));
    let oos_integrity = truthy(assessment.get("oos_integrity_pass"));
    checks.insert(
        "current_prediction_integrity_pass".into(),
        Value::Bool(current_integrity),
    );
    checks.insert("oos_integrity_pass".into(), Value::Bool(oos_integrity));
    if !current_integrity {
        findings.veto(
            "PREDICTION_INTEGRITY_FAILED",
            "Current probability could not be reproduced from frozen bars and fixed parameters.",
        );
    }
    if !oos_integrity {
        findings.veto(
            "OOS_INTEGRITY_FAILED",
            "At least one OOS probability record failed the integrity audit.",
        );
    }
    let consensus_probability = input
        .consensus
        .get("prob_up")
        .cloned()
        .unwrap_or(Value::Null);
    if !consensus_probability.is_null()
        && !truthy(input.consensus.get("production_probability_eligible"))
    {
        findings.veto(
            "PRODUCTION_PROBABILITY_LEAKED",
            "A production probability is present although A is not eligible.",
        );
    }
    if input
        .consensus
        .get("aggregation_method")
        .and_then(Value::as_str)
        != Some("single_path")
        || integer_or_zero(input.consensus.get("production_path_count")) != 1
    {
        findings.veto(
            "AGGREGATION_CONTRACT",
            "Consensus must use exactly one production path.",
        );
    }
    if !input
        .consensus
        .get("confidence_score")
        .map(Value::is_null)
        .unwrap_or(true)
    {
        findings.veto(
            "CONFIDENCE_SCORE_PRESENT",
            "A 0-100 aggregate confidence score must not be published.",
        );
    }
    for item in input
        .consensus
        .get("shadow_predictions")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        if truthy(item.get("production_probability")) {
            findings.veto(
                "SHADOW_PATH_PROMOTED",
                format!(
                    "{} is shadow-only but is marked as production.",
                    py_str(item.get("path").unwrap_or(&Value::Null))
                ),
            );
        }
        let probability = item.get("probability").cloned().unwrap_or(Value::Null);
        if !probability.is_null() && !probability_valid(&probability) {
            findings.veto(
                "SHADOW_PROBABILITY_INVALID",
                format!(
                    "{} shadow probability is outside [0,1].",
                    py_str(item.get("path").unwrap_or(&Value::Null))
                ),
            );
        }
    }

    for result in input.researchers {
        let role = result
            .get("result_role")
            .and_then(Value::as_str)
            .unwrap_or("forecast");
        let path = py_str(result.get("researcher_id").unwrap_or(&Value::Null));
        let probability = result.get("prob_up").cloned().unwrap_or(Value::Null);
        if role == "diagnostic" && !probability.is_null() {
            findings.veto(
                "DIAGNOSTIC_EMITTED_PROBABILITY",
                format!("{path} is diagnostic-only and must not emit a probability."),
            );
        }
        if !probability.is_null() {
            if !probability_valid(&probability) {
                findings.veto(
                    "PREDICTION_FAILED",
                    format!("{path} produced an invalid probability value."),
                );
            }
            if !truthy(result.get("probability_source")) {
                findings.veto(
                    "PREDICTION_FAILED",
                    format!("{path} probability has no provenance."),
                );
            }
        }
        if role == "forecast" {
            let leakage = nested(object(result, "validation"), "leakage_audit");
            if leakage.get("future_returns_used_only_as_labels") == Some(&Value::Bool(false)) {
                findings.veto(
                    "LEAKAGE_DETECTED",
                    format!("{path} declared future returns outside the label role."),
                );
            }
            if leakage.get("features_use_data_through_decision_close_only")
                == Some(&Value::Bool(false))
            {
                findings.veto(
                    "LEAKAGE_DETECTED",
                    format!("{path} declared features beyond the decision close."),
                );
            }
        }
    }

    let quant_validation = input
        .researchers
        .iter()
        .find(|result| result.get("researcher_id").and_then(Value::as_str) == Some("quant"))
        .map(|result| object(result, "validation").clone())
        .map(Value::Object)
        .unwrap_or(Value::Null);
    let evidence_audit = nested(assessment, "oos_evidence_audit");
    let effective = integer_or_zero(evidence_audit.get("effective_valid_samples"));
    let nonoverlap = integer_or_zero(evidence_audit.get("nonoverlap_valid_count"));
    checks.insert("oos_nonoverlap".into(), Value::from(nonoverlap));
    checks.insert("oos_effective_samples".into(), Value::from(effective));
    if effective < i64::from(policy::MIN_REPLAY_EFFECTIVE_SAMPLES) {
        findings.warn(
            "OOS_SUPPORT_INSUFFICIENT",
            format!(
                "Only {effective} effective replay samples ({nonoverlap} non-overlapping rows); {} effective samples are required for publication.",
                policy::MIN_REPLAY_EFFECTIVE_SAMPLES
            ),
        );
    }
    let empty_rows: &[Value] = &[];
    let prospective: Vec<&Value> = input
        .prospective_records
        .unwrap_or(empty_rows)
        .iter()
        .filter(|row| {
            row.get("sampled") == Some(&Value::Bool(true))
                && row.get("status").and_then(Value::as_str) == Some("matured")
        })
        .collect();
    let unanchored: Vec<&Value> = input
        .prospective_records
        .unwrap_or(empty_rows)
        .iter()
        .filter(|row| {
            row.get("status").and_then(Value::as_str) == Some("matured")
                && row.get("sampling_status").and_then(Value::as_str) == Some("unanchored_calendar")
        })
        .collect();
    checks.insert(
        "prospective_nonoverlap".into(),
        Value::from(prospective.len()),
    );
    checks.insert(
        "prospective_matured_unanchored".into(),
        Value::from(unanchored.len()),
    );
    checks.insert(
        "prospective_counting_rule".into(),
        Value::String(
            "only records written within the prompt-recording window on an anchored fixed phase count as non-overlapping forward evidence; unanchored or late records are reported separately"
                .into(),
        ),
    );
    if !truthy(assessment.get("directional_edge_supported")) {
        findings.warn(
            "DIRECTIONAL_EDGE_UNPROVEN",
            format!(
                "No supported directional edge; {}/{} prospective non-overlapping outcomes.",
                prospective.len(),
                policy::MIN_PROSPECTIVE_NONOVERLAP
            ),
        );
    }
    let dividends_excluded = object(&quant_validation, "price_basis")
        .get("dividends_reinvested")
        .map(|value| value == &Value::Bool(false))
        .unwrap_or(false);
    if !dividends_excluded {
        findings.warn(
            "PRICE_BASIS_UNVERIFIED",
            "Price series does not explicitly declare dividends as excluded.",
        );
    }
    if object(input.manifest, "data_quality")
        .get("overall")
        .and_then(Value::as_str)
        == Some("partial")
    {
        findings.warn(
            "DATA_WARNING",
            "Snapshot has non-fatal data-quality limitations; coverage and price basis are reported separately.",
        );
    }
    let sampling = input
        .consensus
        .get("sampling")
        .cloned()
        .unwrap_or(Value::Null);
    checks.insert(
        "sampling".into(),
        if truthy(Some(&sampling)) {
            sampling
        } else {
            Value::Object(Map::new())
        },
    );

    let eligible = truthy(input.consensus.get("production_probability_eligible"))
        && findings.vetoes.is_empty();
    let may_report_probability = eligible && !consensus_probability.is_null();
    checks.insert(
        "news_relationship".into(),
        Value::String("independent_of_quant_probability".into()),
    );
    let mut reason_codes: Vec<String> = findings.reason_codes.clone();
    reason_codes.sort();
    reason_codes.dedup();
    let mut production_eligibility = Map::new();
    production_eligibility.insert(
        "computable".into(),
        Value::Bool(truthy(input.consensus.get("computable"))),
    );
    production_eligibility.insert(
        "current_prediction_integrity_pass".into(),
        Value::Bool(truthy(
            input.consensus.get("current_prediction_integrity_pass"),
        )),
    );
    production_eligibility.insert(
        "oos_integrity_pass".into(),
        Value::Bool(truthy(input.consensus.get("oos_integrity_pass"))),
    );
    production_eligibility.insert(
        "production_probability_eligible".into(),
        Value::Bool(truthy(
            input.consensus.get("production_probability_eligible"),
        )),
    );
    production_eligibility.insert(
        "directional_edge_supported".into(),
        Value::Bool(truthy(input.consensus.get("directional_edge_supported"))),
    );
    production_eligibility.insert("trading_signal_eligible".into(), Value::Bool(false));
    let mut bias_audit = Map::new();
    bias_audit.insert(
        "survivorship".into(),
        Value::String("not_assessed_no_delisting_universe".into()),
    );
    bias_audit.insert(
        "look_ahead".into(),
        Value::String("transform replay verifies labels matured by each decision close".into()),
    );
    bias_audit.insert(
        "data_snooping".into(),
        Value::String(
            "single predeclared model; no path or weight selection on this window".into(),
        ),
    );
    bias_audit.insert(
        "costs".into(),
        Value::String("gross close-to-close; no execution, spread, slippage or fees".into()),
    );
    bias_audit.insert(
        "provider_revision".into(),
        value_or_null(policy_block.get("provider_revision_status")),
    );
    let mut result = Map::new();
    result.insert(
        "status".into(),
        Value::String(
            if may_report_probability {
                "approved"
            } else {
                "vetoed"
            }
            .into(),
        ),
    );
    result.insert(
        "verification_scope".into(),
        Value::String(
            "fixed 52-week / 5-session policy: transform replay, coverage, price basis, path roles, OOS integrity"
                .into(),
        ),
    );
    result.insert(
        "vetoes".into(),
        Value::Array(
            findings
                .vetoes
                .iter()
                .map(|item| Value::String(item.clone()))
                .collect(),
        ),
    );
    result.insert(
        "warnings".into(),
        Value::Array(
            findings
                .warnings
                .iter()
                .map(|item| Value::String(item.clone()))
                .collect(),
        ),
    );
    result.insert(
        "reason_codes".into(),
        Value::Array(
            reason_codes
                .iter()
                .map(|code| Value::String(code.clone()))
                .collect(),
        ),
    );
    result.insert(
        "may_report_probability".into(),
        Value::Bool(may_report_probability),
    );
    result.insert(
        "may_report_direction".into(),
        Value::Bool(may_report_probability),
    );
    result.insert(
        "production_eligibility".into(),
        Value::Object(production_eligibility),
    );
    result.insert("checks".into(), Value::Object(checks));
    result.insert("bias_audit".into(), Value::Object(bias_audit));
    result.insert("confidence_score".into(), Value::Null);
    result.insert("confidence_score_deprecated".into(), Value::Bool(true));
    result.insert(
        "evidence_summary".into(),
        value_or_null(input.consensus.get("evidence")),
    );
    result.insert("policy_ok".into(), Value::Bool(policy_ok));
    let mut result = Value::Object(result);
    if let Some(news) = input.news {
        attach_news_observation(&mut result, Some(news));
    }
    result
}

/// Record the News observation on an already-frozen audit result.
///
/// The audit runs before News interpretation, so a Quant veto can never depend on a
/// News artifact. This function adds the News-only codes afterwards and never changes
/// `may_report_probability`.
pub fn attach_news_observation(result: &mut Value, news: Option<&Value>) {
    let Some(news) = news else {
        return;
    };
    let status = news.get("status").and_then(Value::as_str);
    let warnings_value = news.get("warnings").and_then(Value::as_array);
    let warning_count = warnings_value.map(Vec::len).unwrap_or(0);
    let Some(object) = result.as_object_mut() else {
        return;
    };
    let mut codes: Vec<String> = object
        .get("reason_codes")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    let warnings = object.get_mut("warnings").and_then(Value::as_array_mut);
    let mut warning_list: Vec<String> = warnings
        .as_ref()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    if !news_assessed(status) {
        codes.push("NEWS_NOT_ASSESSED".into());
        let message = "News evidence is unavailable or not assessed; it neither supports nor blocks the quant probability.";
        if !warning_list.iter().any(|item| item == message) {
            warning_list.push(message.to_string());
        }
    } else if news_status_label(status) == "partial" {
        codes.push("NEWS_PARTIAL_EVIDENCE".into());
        let message = format!(
            "News was assessed with {warning_count} disclosed warning(s); the delivered line reports the count and must not be described as unassessed."
        );
        if !warning_list.iter().any(|item| item == &message) {
            warning_list.push(message);
        }
    }
    codes.sort();
    codes.dedup();
    object.insert(
        "reason_codes".into(),
        Value::Array(codes.into_iter().map(Value::String).collect()),
    );
    object.insert(
        "warnings".into(),
        Value::Array(warning_list.into_iter().map(Value::String).collect()),
    );
    let checks = object
        .entry("checks")
        .or_insert_with(|| Value::Object(Map::new()));
    if let Value::Object(checks) = checks {
        checks.insert("news_warning_count".into(), Value::from(warning_count));
        checks.insert(
            "news_state".into(),
            Value::String(news_status_label(status).into()),
        );
    }
}
