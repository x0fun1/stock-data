//! Run the fixed-policy research pipeline and bind every stage to one snapshot
//! (`orchestrator.py`).
//!
//! This is the only place that writes an analysis directory: request/snapshot/data
//! bindings first, then the researchers, the independent audit (before News), the three
//! News states, the report/response artifacts and the stage receipts last. Every stage
//! receipt is bound to the same run id, snapshot id and policy version, and the delivery
//! gate re-reads the frozen files.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::Instant;

use chrono::{DateTime, NaiveDate, Utc};
use serde_json::{json, Map, Value};

use sd_core::policy::{
    is_sampled_ordinal, model_identity, session_ordinal, POLICY_VERSION, REQUIRED_STAGES,
};
use sd_news::pipeline::{
    freeze_receipt, prepare_news_input, result_sha256, run_news, verify_frozen_result,
};
use sd_news::status::{news_assessed, news_status_label};
use sd_research::result::researcher_result;
use sd_storage::jsonio::{read_json_file, write_json};
use sd_storage::path_guard::safe_output_destination;
use sd_storage::snapshot::{load_snapshot, LoadedSnapshot};

use crate::consensus::{
    build_consensus, forecast_assessment, redact_publishable_prediction, AssessmentInput,
    ConsensusInput,
};
use crate::report::{
    build_agent_summary, enforce_agent_summary_budget, render_final_response, render_markdown,
    reporting_status_for,
};
use crate::synthesis::build_synthesis;

/// One researcher path and its result role.
const RUNNERS: [(&str, &str); 4] = [
    ("quant", "production"),
    ("factor", "shadow"),
    ("ml", "shadow"),
    ("factor_backtest", "diagnostic"),
];

/// Options for [`run_analysis`].
pub struct AnalysisOptions<'a> {
    /// News policy block; when absent the frozen News result is reused or an
    /// `not_assessed` placeholder is written.
    pub news_policy: Option<&'a Value>,
    /// Host-supplied frozen AI headline judgement.
    pub news_ai_assessment: Option<&'a Value>,
    /// Existing ledger used to read prospective forward records.
    pub ledger_path: Option<&'a Path>,
    /// `audit` (full artifact) or `compact` (per-origin rows referenced, not duplicated).
    pub artifact_profile: &'a str,
    /// Injected clock for `created_at` (`utc_now` seam).
    pub now: Option<DateTime<Utc>>,
}

impl Default for AnalysisOptions<'_> {
    fn default() -> Self {
        Self {
            news_policy: None,
            news_ai_assessment: None,
            ledger_path: None,
            artifact_profile: "audit",
            now: None,
        }
    }
}

fn py_str(value: Option<&Value>) -> String {
    sd_core::contracts::py_str(value.unwrap_or(&Value::Null))
}

fn py_truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Null) | None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn round3(value: f64) -> Value {
    Value::from((value * 1000.0).round() / 1000.0)
}

fn utc_now_iso(now: Option<DateTime<Utc>>) -> String {
    let moment = now.unwrap_or_else(Utc::now);
    sd_news::normalize::isoformat_z(moment)
}

/// `orchestrator._stage`: append one bound stage receipt.
#[allow(clippy::too_many_arguments)]
fn stage(
    stages: &mut Vec<Value>,
    name: &str,
    status: &str,
    payload: Option<&Value>,
    reason: Option<&str>,
    run_id: &str,
    snapshot_id: &str,
) -> Result<(), String> {
    if !REQUIRED_STAGES.contains(&name) {
        return Err(format!("unknown stage: {name}"));
    }
    if stages
        .iter()
        .any(|item| item.get("stage").and_then(Value::as_str) == Some(name))
    {
        return Err(format!("duplicate stage: {name}"));
    }
    let mut receipt = Map::new();
    receipt.insert("stage".into(), Value::String(name.into()));
    receipt.insert("status".into(), Value::String(status.into()));
    receipt.insert(
        "reason".into(),
        reason
            .map(|value| Value::String(value.into()))
            .unwrap_or(Value::Null),
    );
    receipt.insert("run_id".into(), Value::String(run_id.into()));
    receipt.insert("snapshot_id".into(), Value::String(snapshot_id.into()));
    receipt.insert(
        "policy_version".into(),
        Value::String(POLICY_VERSION.into()),
    );
    receipt.insert(
        "payload_sha256".into(),
        payload
            .map(|payload| Value::String(sd_storage::ledger::canonical_sha256(payload)))
            .unwrap_or(Value::Null),
    );
    stages.push(Value::Object(receipt));
    Ok(())
}

fn validate_stages(stages: &[Value]) -> Result<(), String> {
    let names: Vec<String> = stages
        .iter()
        .map(|item| py_str(item.get("stage")))
        .collect();
    let required: Vec<String> = REQUIRED_STAGES
        .iter()
        .map(|name| (*name).to_string())
        .collect();
    if names != required {
        return Err(format!(
            "stage sequence must be exactly {required:?}; got {names:?}"
        ));
    }
    const STATUSES: [&str; 7] = [
        "complete",
        "partial",
        "skipped",
        "not_applicable",
        "not_assessed",
        "blocked",
        "insufficient_data",
    ];
    for item in stages {
        let status = py_str(item.get("status"));
        if !STATUSES.contains(&status.as_str()) {
            return Err(format!("invalid stage status: {status}"));
        }
        if status != "complete" && !py_truthy(item.get("reason")) {
            return Err(format!(
                "stage {} must explain a non-complete status",
                py_str(item.get("stage"))
            ));
        }
    }
    Ok(())
}

/// `orchestrator._compact_payload`: replace per-origin rows with a pointer + count.
fn compact_payload(final_result: &Value) -> Value {
    let mut payload = final_result.clone();
    if let Some(researchers) = payload
        .get_mut("researchers")
        .and_then(Value::as_object_mut)
    {
        let names: Vec<String> = researchers.keys().cloned().collect();
        for name in names {
            let Some(result) = researchers.get_mut(&name).and_then(Value::as_object_mut) else {
                continue;
            };
            let Some(validation) = result.get_mut("validation").and_then(Value::as_object_mut)
            else {
                continue;
            };
            let Some(rows) = validation
                .get("oos_forecasts")
                .and_then(Value::as_array)
                .cloned()
            else {
                continue;
            };
            validation.insert("oos_forecast_count".into(), Value::from(rows.len()));
            validation.insert(
                "oos_forecasts_ref".into(),
                Value::String(format!("researchers/{name}.json")),
            );
            validation.insert(
                "oos_forecasts".into(),
                json!({
                    "omitted": rows.len(),
                    "reason": "compact artifact profile; the full rows are retained in the researcher artifact",
                }),
            );
        }
    }
    if let Some(root) = payload.as_object_mut() {
        root.insert("artifact_profile".into(), Value::String("compact".into()));
        root.insert(
            "artifact_profile_note".into(),
            Value::String(
                "per-origin replay rows are referenced, not duplicated; use researchers/<path>.json for the full ledger"
                    .into(),
            ),
        );
    }
    payload
}

/// `orchestrator._analysis_status`: the documented vocabulary, not the availability flag.
fn analysis_status(
    adversarial: &Value,
    researchers: &Map<String, Value>,
    news_status: &str,
) -> &'static str {
    if adversarial
        .get("may_report_probability")
        .and_then(Value::as_bool)
        != Some(true)
    {
        return "abstain";
    }
    let degraded = py_truthy(adversarial.get("warnings"))
        || researchers.values().any(|item| {
            !matches!(
                item.get("status").and_then(Value::as_str),
                Some("success" | "partial")
            )
        })
        || news_status != "complete";
    if degraded {
        "degraded"
    } else {
        "complete_with_limitations"
    }
}

fn decision_sampled(
    decision_session: Option<&str>,
    sessions: Option<&[NaiveDate]>,
) -> Option<bool> {
    let (decision_session, sessions) = (decision_session?, sessions?);
    let Ok(decision) = NaiveDate::parse_from_str(decision_session, "%Y-%m-%d") else {
        return None;
    };
    let ordinal = session_ordinal(sessions, decision)?;
    Some(is_sampled_ordinal(Some(ordinal)))
}

fn market_value(loaded: &LoadedSnapshot) -> Value {
    let mut market = loaded
        .domains
        .get("market")
        .cloned()
        .unwrap_or_else(|| json!({"data": {}}));
    if let Some(root) = market.as_object_mut() {
        root.insert("_data_validation".into(), loaded.frozen_validation.clone());
    }
    market
}

fn bars_value(market: &Value, diagnostic: bool) -> Vec<Value> {
    match sd_core::market_data::market_bars(market, diagnostic) {
        Ok(bars) => bars
            .iter()
            .map(|bar| {
                json!({
                    "date": bar.date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                })
            })
            .collect(),
        Err(_) => Vec::new(),
    }
}

fn read_json(path: &Path) -> Result<Value, String> {
    read_json_file(path).map_err(|error| error.message().to_string())
}

/// `news.freeze_result`: readable JSON plus the adjacent SHA-256 receipt.
pub fn freeze_result_at(path: &Path, result: &Value, frozen_at: &str) -> Result<Value, String> {
    write_json(path, result, false).map_err(|error| error.message().to_string())?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    let receipt = freeze_receipt(&name, result, frozen_at);
    let receipt_path = path.with_file_name(format!(
        "{}.freeze.json",
        path.file_stem()
            .map(|stem| stem.to_string_lossy().to_string())
            .unwrap_or_default()
    ));
    std::fs::write(
        &receipt_path,
        format!(
            "{}\n",
            serde_json::to_string_pretty(&receipt).map_err(|error| error.to_string())?
        ),
    )
    .map_err(|error| error.to_string())?;
    Ok(receipt)
}

/// `news.load_frozen_result`: load a stage result only when its receipt matches.
pub fn load_frozen_result_at(path: &Path) -> Result<(Value, Value), String> {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    let receipt_path = path.with_file_name(format!(
        "{}.freeze.json",
        path.file_stem()
            .map(|stem| stem.to_string_lossy().to_string())
            .unwrap_or_default()
    ));
    if !path.is_file() || !receipt_path.is_file() {
        return Err(format!("frozen result and receipt are required: {name}"));
    }
    let result = read_json(path)?;
    let freeze = read_json(&receipt_path)?;
    verify_frozen_result(&name, &result, &freeze)?;
    Ok((result, freeze))
}

fn news_domain(snapshot_dir: &Path) -> Value {
    let path = snapshot_dir.join("news.json");
    if path.is_file() {
        read_json(&path).unwrap_or(Value::Null)
    } else {
        Value::Null
    }
}

fn run_researcher(
    path: &str,
    _snapshot_dir: &Path,
    manifest: &Value,
    market: &Value,
) -> Result<Value, String> {
    match path {
        "quant" => sd_research::quant::run(manifest, market),
        "factor" => sd_research::factor::run(manifest, market),
        "ml" => sd_research::ml::run(manifest, market),
        "factor_backtest" => sd_research::factor_backtest::run(manifest, market),
        other => Err(format!("unknown researcher path {other}")),
    }
}

/// Run the fixed-policy research pipeline for one frozen snapshot.
pub fn run_analysis(
    snapshot_dir: &Path,
    output_root: &Path,
    options: &AnalysisOptions<'_>,
) -> Result<PathBuf, String> {
    if options.artifact_profile != "audit" && options.artifact_profile != "compact" {
        return Err("artifact_profile must be audit or compact".into());
    }
    let started = Instant::now();
    let loaded = load_snapshot(snapshot_dir).map_err(|error| error.message().to_string())?;
    let manifest = loaded.manifest.clone();
    let market = market_value(&loaded);
    let snapshot_id = py_str(manifest.get("snapshot_id"));
    let run_id = manifest
        .get("run_id")
        .filter(|value| !value.is_null())
        .map(|value| py_str(Some(value)))
        .unwrap_or_else(|| snapshot_id.clone());
    let ticker = py_str(manifest.get("ticker"));
    let market_name = py_str(manifest.get("market"));
    let horizon = py_str(manifest.get("horizon"));
    let policy = manifest.get("policy").cloned().unwrap_or_else(|| json!({}));
    let data_validation = manifest
        .get("data_validation")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let data_quality = manifest
        .get("data_quality")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let report_manifest = json!({
        "ticker": ticker,
        "market": market_name,
        "horizon": horizon,
        "asof_timestamp": manifest.get("asof_timestamp").cloned().unwrap_or(Value::Null),
        "snapshot_id": snapshot_id,
        "policy": policy,
        "target_session": policy.get("target_session").cloned().unwrap_or(Value::Null),
        "data_validation": data_validation,
        "data_quality": data_quality,
        "warnings": data_quality.get("issues").cloned().unwrap_or(Value::Null),
    });
    let mut run = Map::new();
    run.insert(
        "schema_version".into(),
        Value::String("analysis-3.0".into()),
    );
    run.insert("run_id".into(), Value::String(run_id.clone()));
    run.insert("snapshot_id".into(), Value::String(snapshot_id.clone()));
    run.insert(
        "policy_version".into(),
        Value::String(POLICY_VERSION.into()),
    );
    run.insert("created_at".into(), Value::String(utc_now_iso(options.now)));
    run.insert(
        "package_version".into(),
        Value::String(sd_core::versions::RUNTIME_VERSION.into()),
    );
    let destination = safe_output_destination(output_root, &run_id)
        .map_err(|error| error.message().to_string())?;
    let snapshot_binding = json!({
        "snapshot_id": snapshot_id,
        "content_sha256": manifest.get("snapshot_content_sha256").cloned().unwrap_or(Value::Null),
    });
    std::fs::create_dir_all(&destination).map_err(|error| error.to_string())?;
    let write = |name: &str, value: &Value| -> Result<(), String> {
        write_json(&destination.join(name), value, false)
            .map(|_| ())
            .map_err(|error| error.message().to_string())
    };
    let mut stages: Vec<Value> = Vec::new();
    let request_block = manifest.get("request").cloned().unwrap_or(Value::Null);
    stage(
        &mut stages,
        "request",
        "complete",
        Some(&request_block),
        None,
        &run_id,
        &snapshot_id,
    )?;
    write(
        "request.json",
        &manifest
            .get("request")
            .cloned()
            .unwrap_or_else(|| json!({})),
    )?;
    let source_payload = manifest
        .get("source_payload_sha256")
        .cloned()
        .unwrap_or(Value::Null);
    stage(
        &mut stages,
        "collection",
        "complete",
        Some(&source_payload),
        None,
        &run_id,
        &snapshot_id,
    )?;
    write(
        "snapshot.json",
        &json!({
            "schema_version": "analysis-snapshot-binding-1.0",
            "snapshot_id": snapshot_id,
            "source_payload_sha256": manifest.get("source_payload_sha256").cloned().unwrap_or(Value::Null),
            "policy": policy,
            "binding": snapshot_binding,
        }),
    )?;
    let data_complete = data_validation
        .get("forecast_eligible")
        .and_then(Value::as_bool)
        == Some(true);
    let data_status = if data_complete { "complete" } else { "blocked" };
    stage(
        &mut stages,
        "data",
        data_status,
        Some(&data_validation),
        if data_complete {
            None
        } else {
            Some("snapshot data validation did not pass")
        },
        &run_id,
        &snapshot_id,
    )?;
    write(
        "data.json",
        &json!({"data_quality": data_quality, "data_validation": data_validation, "policy": policy}),
    )?;

    let bars: Vec<Value> = if data_complete {
        bars_value(&market, false)
    } else {
        bars_value(&market, true)
    };
    let sessions: Option<Vec<NaiveDate>> = sd_core::market_data::calendar_session_list(&market);
    let mut results: Map<String, Value> = Map::new();
    if data_complete {
        for (path, role) in RUNNERS {
            let mut result = match run_researcher(path, snapshot_dir, &manifest, &market) {
                Ok(result) => result,
                Err(error) => researcher_result(
                    path,
                    &ticker,
                    &horizon,
                    &snapshot_id,
                    "failed",
                    role,
                    None,
                    None,
                    json!({"positive": [], "negative": []}),
                    json!({}),
                    Vec::new(),
                    vec![Value::String(format!("Research path failed: {error}"))],
                    None,
                    None,
                )
                .unwrap_or_else(|message| json!({"researcher_id": path, "status": "failed", "warning": message})),
            };
            if let Some(map) = result.as_object_mut() {
                map.insert("result_role".into(), Value::String(role.into()));
                map.insert(
                    "model_version".into(),
                    model_identity(path)
                        .ok()
                        .and_then(|identity| identity.get("model_version").cloned())
                        .unwrap_or(Value::Null),
                );
                map.insert(
                    "policy_version".into(),
                    Value::String(POLICY_VERSION.into()),
                );
            }
            results.insert(path.to_string(), result);
        }
    }
    let quant_status = results
        .get("quant")
        .and_then(|item| item.get("status"))
        .and_then(Value::as_str)
        .unwrap_or("blocked")
        .to_string();
    let quant_stage = if quant_status == "success" {
        "complete"
    } else if quant_status == "insufficient_data" {
        "insufficient_data"
    } else {
        "blocked"
    };
    let mut quant_payload = Map::new();
    for (path, item) in &results {
        quant_payload.insert(
            path.clone(),
            json!({"status": item.get("status").cloned().unwrap_or(Value::Null),
                   "prob_up": item.get("prob_up").cloned().unwrap_or(Value::Null)}),
        );
    }
    stage(
        &mut stages,
        "quant",
        quant_stage,
        Some(&Value::Object(quant_payload)),
        if quant_status == "success" {
            None
        } else {
            Some("production path A did not produce a probability")
        },
        &run_id,
        &snapshot_id,
    )?;

    let mut prospective: Vec<Value> = Vec::new();
    if let Some(ledger_path) = options.ledger_path {
        prospective =
            sd_storage::ledger::prospective_records(ledger_path, Some(&ticker), Some(&market_name))
                .unwrap_or_default();
    }
    let mut assessments: Map<String, Value> = Map::new();
    if let Some(quant) = results.get("quant") {
        assessments.insert(
            "quant".into(),
            forecast_assessment(&AssessmentInput {
                result: quant,
                manifest: &manifest,
                oos_evidence_audit: None,
                bars: Some(&bars),
                sessions: sessions.as_deref(),
                prospective_records: &prospective,
            }),
        );
    }
    let researchers_vec: Vec<Value> = results.values().cloned().collect();
    let mut consensus = build_consensus(&ConsensusInput {
        researchers: &researchers_vec,
        manifest: &manifest,
        assessments: Some(&assessments),
        bars: Some(&bars),
        sessions: sessions.as_deref(),
        prospective_records: &prospective,
    });
    let decision_session = market
        .get("data")
        .and_then(|data| data.get("policy_window"))
        .and_then(|window| window.get("decision_session"))
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let sampled = decision_sampled(decision_session.as_deref(), sessions.as_deref());
    if let Some(root) = consensus.as_object_mut() {
        root.insert(
            "decision_sampled".into(),
            sampled.map(Value::Bool).unwrap_or(Value::Null),
        );
        root.insert(
            "sampling".into(),
            json!({
                "status": if sampled.is_none() { "unanchored_calendar" } else { "anchored" },
                "decision_sampled": sampled.map(Value::Bool).unwrap_or(Value::Null),
                "decision_session": decision_session,
                "phase_available": sampled.is_some(),
                "counts_as_nonoverlap": sampled == Some(true),
                "role": "throttle_only_not_qualification",
                "note": "the phase never gates eligibility; it decides whether a forward record may count as non-overlapping evidence. An unanchored phase is reported as unavailable, not as 'not sampled'",
            }),
        );
    }
    let mut timing = sd_research::feasibility::build_timing(&manifest, &bars);
    if let Some(root) = timing.as_object_mut() {
        root.insert(
            "target_session".into(),
            policy.get("target_session").cloned().unwrap_or(Value::Null),
        );
    }
    if let Some(root) = consensus.as_object_mut() {
        root.insert("timing".into(), timing);
    }
    let mut adversarial = sd_audit::audit::audit(&sd_audit::audit::AuditInput {
        manifest: &manifest,
        researchers: &researchers_vec,
        consensus: &consensus,
        news: None,
        prospective_records: Some(&prospective),
    });

    let mut news_result: Option<Value> = None;
    let frozen_path = snapshot_dir.join("news_result.json");
    if frozen_path.is_file() {
        news_result = load_frozen_result_at(&frozen_path)
            .ok()
            .map(|(result, _)| result);
    }
    if news_result.is_none() {
        if let Some(policy_block) = options.news_policy {
            let news_payload = news_domain(snapshot_dir);
            let prepared = prepare_news_input(&manifest, &market, &news_payload)?;
            let preview = if snapshot_dir.join("news_preview.json").is_file() {
                load_frozen_result_at(&snapshot_dir.join("news_preview.json"))
                    .ok()
                    .map(|(result, _)| result)
            } else {
                None
            };
            let produced = run_news(
                &prepared,
                Some(policy_block),
                options.news_ai_assessment,
                preview.as_ref(),
            )?;
            freeze_result_at(&frozen_path, &produced, &utc_now_iso(options.now))?;
            news_result = Some(produced);
        }
    }
    let news_result = news_result.unwrap_or_else(|| {
        json!({
            "status": "not_assessed",
            "snapshot_id": snapshot_id,
            "ticker": ticker,
            "market": market_name,
            "horizon": manifest.get("horizon").cloned().unwrap_or(Value::Null),
            "asof_timestamp": manifest.get("asof_timestamp").cloned().unwrap_or(Value::Null),
        })
    });
    sd_audit::audit::attach_news_observation(&mut adversarial, Some(&news_result));
    let may_report = adversarial
        .get("may_report_probability")
        .and_then(Value::as_bool)
        == Some(true);
    if !may_report {
        redact_publishable_prediction(&mut consensus, &adversarial);
        let mut codes: BTreeSet<String> = consensus
            .get("reason_codes")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .map(|row| py_str(Some(row)))
                    .collect::<BTreeSet<String>>()
            })
            .unwrap_or_default();
        let audit_codes: Vec<String> = adversarial
            .get("reason_codes")
            .and_then(Value::as_array)
            .map(|rows| rows.iter().map(|row| py_str(Some(row))).collect())
            .unwrap_or_default();
        for code in audit_codes {
            codes.insert(code);
        }
        if let Some(root) = consensus.as_object_mut() {
            root.insert(
                "reason_codes".into(),
                Value::Array(codes.into_iter().map(Value::String).collect()),
            );
        }
    }
    stage(
        &mut stages,
        "audit",
        "complete",
        Some(&adversarial),
        None,
        &run_id,
        &snapshot_id,
    )?;
    write("audit.json", &adversarial)?;
    std::fs::create_dir_all(destination.join("researchers")).map_err(|error| error.to_string())?;
    for (path, item) in &results {
        write_json(
            &destination.join("researchers").join(format!("{path}.json")),
            item,
            false,
        )
        .map_err(|error| error.message().to_string())?;
    }
    freeze_result_at(
        &destination.join("news_result.json"),
        &news_result,
        &utc_now_iso(options.now),
    )?;

    let news_status = news_result
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("not_assessed")
        .to_string();
    let assessed = news_assessed(Some(&news_status));
    let severity = news_result
        .get("warning_severity_counts")
        .cloned()
        .unwrap_or(Value::Null);
    let evidence_warnings = severity
        .get("evidence_affecting")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let presentation_warnings = severity
        .get("presentation")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let news_stage_status = if assessed && news_status_label(Some(&news_status)) == "partial" {
        "partial"
    } else if assessed {
        "complete"
    } else {
        "not_assessed"
    };
    let news_reason = if news_stage_status == "complete" {
        None
    } else if news_stage_status == "partial" {
        Some(format!(
            "news assessed with {evidence_warnings} evidence-affecting and {presentation_warnings} presentation warning(s)"
        ))
    } else {
        Some("news evidence unavailable or not assessed".to_string())
    };
    stage(
        &mut stages,
        "news",
        news_stage_status,
        Some(&json!({
            "status": news_status,
            "warning_count": news_result.get("warnings").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            "warning_severity": {"evidence_affecting": evidence_warnings, "presentation": presentation_warnings},
        })),
        news_reason.as_deref(),
        &run_id,
        &snapshot_id,
    )?;
    stage(
        &mut stages,
        "cross-validation",
        "not_applicable",
        None,
        Some("news and Quant are reported independently; news never re-scores the production probability"),
        &run_id,
        &snapshot_id,
    )?;
    stage(
        &mut stages,
        "risk",
        "not_assessed",
        None,
        Some("returns are gross close-to-close observations; execution, spread, slippage and fees are not modelled"),
        &run_id,
        &snapshot_id,
    )?;

    let mut synthesis_input = run.clone();
    synthesis_input.insert("snapshot".into(), report_manifest.clone());
    synthesis_input.insert("consensus".into(), consensus.clone());
    synthesis_input.insert("adversarial_audit".into(), adversarial.clone());
    synthesis_input.insert("researchers".into(), Value::Object(results.clone()));
    let synthesis = build_synthesis(&Value::Object(synthesis_input), &news_result)?;
    let elapsed_seconds = round3(started.elapsed().as_secs_f64());
    let mut final_result = Map::new();
    for (key, value) in &run {
        final_result.insert(key.clone(), value.clone());
    }
    final_result.insert("analysis_id".into(), Value::String(run_id.clone()));
    final_result.insert("elapsed_seconds".into(), elapsed_seconds);
    final_result.insert("snapshot".into(), report_manifest);
    final_result.insert("researchers".into(), Value::Object(results.clone()));
    final_result.insert("consensus".into(), consensus.clone());
    final_result.insert("adversarial_audit".into(), adversarial.clone());
    final_result.insert("news_result".into(), news_result.clone());
    final_result.insert("synthesis".into(), synthesis);
    final_result.insert(
        "analysis_status".into(),
        Value::String(analysis_status(&adversarial, &results, &news_status).into()),
    );
    let reporting_status = reporting_status_for(&json!({
        "consensus": consensus,
        "adversarial_audit": adversarial,
    }));
    final_result.insert(
        "reporting_status".into(),
        Value::String(reporting_status.into()),
    );
    stage(
        &mut stages,
        "final",
        "complete",
        Some(&json!({
            "consensus": consensus,
            "audit": adversarial,
            "synthesis": final_result.get("synthesis").cloned().unwrap_or(Value::Null),
        })),
        None,
        &run_id,
        &snapshot_id,
    )?;
    validate_stages(&stages)?;
    final_result.insert("stages".into(), Value::Array(stages.clone()));

    let mut final_value = Value::Object(final_result);
    let agent_summary = enforce_agent_summary_budget(
        build_agent_summary(&final_value),
        crate::report::AGENT_SUMMARY_BUDGET,
    );
    let markdown = render_markdown(&final_value);
    let response = render_final_response(&final_value);
    write("agent_summary.json", &agent_summary)?;
    std::fs::write(destination.join("report.md"), &markdown).map_err(|error| error.to_string())?;
    let mut serialized = if options.artifact_profile == "compact" {
        compact_payload(&final_value)
    } else {
        final_value.clone()
    };
    write("analysis.json", &serialized)?;
    std::fs::write(destination.join("final_response.md"), &response)
        .map_err(|error| error.to_string())?;
    if let Some(root) = final_value.as_object_mut() {
        let artifacts = json!({
            "analysis": destination.join("analysis.json").to_string_lossy().to_string(),
            "markdown": destination.join("report.md").to_string_lossy().to_string(),
            "agent_summary": destination.join("agent_summary.json").to_string_lossy().to_string(),
            "final_response": destination.join("final_response.md").to_string_lossy().to_string(),
        });
        root.insert("artifacts".into(), artifacts);
    }
    serialized = if options.artifact_profile == "compact" {
        compact_payload(&final_value)
    } else {
        final_value.clone()
    };
    write("analysis.json", &serialized)?;
    let _ = result_sha256(&final_value);
    Ok(destination)
}
