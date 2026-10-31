//! Execution of the 13 `stock-data research` commands (`quant_research.py`).
//!
//! Every arm mirrors the legacy flow: same JSON documents, same output channels and the
//! same exit codes. Failures are rendered as the research error object on stderr with
//! exit 2. The `schema` arm lives in `research.rs`/`schema_generated.rs`.

use std::path::{Path, PathBuf};

use serde_json::Map;

use chrono::Utc;
use serde_json::{json, Value};

use sd_app::cleanup_gate::AppCleanupGate;
use sd_app::delivery::validate_final_response;
use sd_app::orchestrator::{
    freeze_result_at, load_frozen_result_at, run_analysis, AnalysisOptions,
};
use sd_core::clock::{Clock, SystemClock};
use sd_core::contracts::normalize_request;
use sd_core::error::SdError;
use sd_core::security::bounded_text;
use sd_news::pipeline::{analyze_news, prepare_news_input};
use sd_storage::cleanup::{cleanup_analysis, cleanup_owned_run, CleanupOptions};
use sd_storage::freeze::{freeze_snapshot, FreezeOptions};
use sd_storage::ledger::{
    audit as ledger_audit, record_prediction, settle as ledger_settle, RecordOptions,
};
use sd_storage::path_guard::safe_output_destination;
use sd_storage::retention::{
    gc_runtime, GcOptions, DEFAULT_CACHE_DAYS, DEFAULT_PENDING_DAYS, GC_THROTTLE_HOURS,
};
use sd_storage::run_workspace::{
    create_run_workspace, mark_run_failed, release_pipeline_lock, update_run_workspace,
};
use sd_storage::snapshot::load_snapshot;
use sd_storage::stock_data_adapter::write_receipt;

use crate::output;
use crate::research::ResearchCommand;

fn read_json(path: &Path) -> Result<Value, String> {
    let text =
        std::fs::read_to_string(path).map_err(|error| format!("FileNotFoundError: {error}"))?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    serde_json::from_str(text).map_err(|error| format!("ValueError: {error}"))
}

fn write_json_exclusive(path: &Path, value: &Value) -> Result<PathBuf, String> {
    sd_storage::jsonio::write_json(path, value, true).map_err(|error| error.message().to_string())
}

fn error_payload(error: &SdError) -> u8 {
    output::stderr_json(&output::research_error(error));
    2
}

fn sd(message: &str) -> SdError {
    SdError::validation(message.to_string())
}

fn now_text(clock: &dyn Clock) -> String {
    clock.utc_now()
}

fn collection_name(clock: &dyn Clock) -> String {
    format!(
        "collection_{}.json",
        now_text(clock).replace([':', '-', '.'], "")
    )
}

/// Snapshot inputs used by the News-only commands.
fn snapshot_inputs(snapshot_dir: &Path) -> Result<(Value, Value, Value), SdError> {
    let loaded = load_snapshot(snapshot_dir)?;
    let mut market = loaded
        .domains
        .get("market")
        .cloned()
        .unwrap_or_else(|| json!({"data": {}}));
    if let Some(root) = market.as_object_mut() {
        root.insert("_data_validation".into(), loaded.frozen_validation.clone());
    }
    let news = snapshot_dir
        .join("news.json")
        .is_file()
        .then(|| read_json(&snapshot_dir.join("news.json")).ok())
        .flatten()
        .unwrap_or(Value::Null);
    Ok((loaded.manifest, market, news))
}

fn analysis_result(location: &Path, analysis: &Value) -> Value {
    let adversarial = analysis
        .get("adversarial_audit")
        .cloned()
        .unwrap_or_else(|| json!({}));
    json!({
        "status": "success",
        "analysis_status": analysis.get("analysis_status").cloned().unwrap_or(Value::Null),
        "reporting_status": analysis.get("reporting_status").cloned().unwrap_or(Value::Null),
        "may_report_direction": adversarial.get("may_report_direction").cloned().unwrap_or(Value::Bool(false)),
        "report_dir": location.to_string_lossy(),
        "json": location.join("analysis.json").to_string_lossy(),
        "markdown": location.join("report.md").to_string_lossy(),
        "agent_summary": location.join("agent_summary.json").to_string_lossy(),
        "final_response": location.join("final_response.md").to_string_lossy(),
    })
}

fn read_optional_json(path: &Option<PathBuf>, label: &str) -> Result<Option<Value>, String> {
    match path {
        None => Ok(None),
        Some(path) => {
            let value = read_json(path)?;
            if !value.is_object() {
                return Err(format!("{label} must contain a JSON object"));
            }
            Ok(Some(value))
        }
    }
}

fn run_request(args: &crate::research::RequestArgs) -> Result<u8, SdError> {
    let value = read_json(&args.input).map_err(|message| sd(&message))?;
    let normalized = normalize_request(&value)?;
    let document = normalized.to_json();
    write_json_exclusive(&args.output, &document).map_err(|message| sd(&message))?;
    output::stdout_json(&json!({"status": "success", "request": document}));
    Ok(0)
}

fn run_adapt(args: &crate::research::AdaptArgs) -> Result<u8, SdError> {
    let request_value = read_json(&args.request).map_err(|message| sd(&message))?;
    let response_value = read_json(&args.responses).map_err(|message| sd(&message))?;
    let domain_map = match response_value.get("domains") {
        Some(domains) => domains.clone(),
        None => response_value.clone(),
    };
    let Some(domains) = domain_map.as_object() else {
        return Err(sd("responses file must contain a domain map"));
    };
    let clock = SystemClock;
    let location = write_receipt(
        &request_value,
        domains,
        &args.output,
        args.captured_at_utc.as_deref(),
        &clock,
    )?;
    output::stdout_json(
        &json!({"status": "success", "collection_receipt": location.to_string_lossy()}),
    );
    Ok(0)
}

/// Verified exchange calendar for the request's market, when the Skill pack is available.
///
/// A corrupt or tampered pack is a hard error; an absent pack stays missing evidence and
/// downstream gates report `CALENDAR_UNVERIFIED` instead of inventing sessions.
fn library_calendar_for(
    payload: &Value,
    explicit_dir: Option<&Path>,
) -> Result<Option<sd_core::calendar::CalendarResource>, SdError> {
    let market = payload
        .get("request")
        .and_then(|request| request.get("market"))
        .and_then(Value::as_str)
        .unwrap_or("US");
    let pack = sd_storage::calendar_resource::load_pack(explicit_dir)?;
    Ok(pack.and_then(|pack| pack.resource(market).cloned()))
}

fn run_freeze(args: &crate::research::FreezeArgs) -> Result<u8, SdError> {
    let payload = read_json(&args.input).map_err(|message| sd(&message))?;
    let calendar = library_calendar_for(&payload, args.calendar_dir.as_deref())?;
    let clock = SystemClock;
    let location = freeze_snapshot(
        &payload,
        &FreezeOptions {
            output_root: &args.output_root,
            library_calendar: calendar.as_ref(),
            allow_library_calendar: true,
            on_snapshot_created: None,
        },
        &clock,
    )?;
    output::stdout_json(&json!({"status": "success", "snapshot_dir": location.to_string_lossy()}));
    Ok(0)
}

fn run_news_candidates(args: &crate::research::NewsCandidatesArgs) -> Result<u8, SdError> {
    let (manifest, market, news) =
        snapshot_inputs(&args.snapshot_dir).map_err(|error| sd(error.message()))?;
    let news_input =
        prepare_news_input(&manifest, &market, &news).map_err(|message| sd(&message))?;
    let result = analyze_news(&news_input, None, None).map_err(|message| sd(&message))?;
    let clock = SystemClock;
    let preview_path = args.snapshot_dir.join("news_preview.json");
    write_json_exclusive(&preview_path, &result).map_err(|message| sd(&message))?;
    // The adjacent receipt is what makes the expensive preview reusable.
    let name = "news_preview.json";
    let receipt = sd_news::pipeline::freeze_receipt(name, &result, &now_text(&clock));
    let receipt_path = args.snapshot_dir.join("news_preview.freeze.json");
    std::fs::write(
        &receipt_path,
        format!(
            "{}\n",
            serde_json::to_string_pretty(&receipt).map_err(|error| sd(&error.to_string()))?
        ),
    )
    .map_err(|error| sd(&error.to_string()))?;

    let rows = result
        .get("ai_lean_candidates")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let keys = [
        "news_id",
        "record_kind",
        "headline",
        "summary",
        "source",
        "published_at",
        "url",
        "ticker_relevance",
        "eligible_evidence",
        "exclusion_reasons",
    ];
    let mut exported: Vec<Value> = rows
        .iter()
        .map(|row| {
            let mut item = Map::new();
            for key in keys {
                item.insert(
                    key.to_string(),
                    row.get(key).cloned().unwrap_or(Value::Null),
                );
            }
            Value::Object(item)
        })
        .collect();
    if args.focus == "major" {
        exported.retain(|row| {
            row.get("eligible_evidence").and_then(Value::as_bool) == Some(true)
                && row.get("ticker_relevance").and_then(Value::as_str) == Some("direct")
        });
    }
    let document = json!({
        "schema_version": "1.1",
        "ticker": news_input.ticker,
        "asof_timestamp": news_input.asof_timestamp,
        "news_input_digest": result.get("news_input_digest").cloned().unwrap_or(Value::Null),
        "focus": args.focus,
        "candidate_count": exported.len(),
        "excluded_by_focus": rows.len() - exported.len(),
        "items": exported,
    });
    write_json_exclusive(&args.output, &document).map_err(|message| sd(&message))?;
    output::stdout_json(&json!({
        "status": "success",
        "candidate_count": exported.len(),
        "excluded_by_focus": rows.len() - exported.len(),
        "output": args.output.to_string_lossy(),
    }));
    Ok(0)
}

/// Shared implementation of `analyze` and the owned-run part of `pipeline`.
fn run_analysis_command(
    snapshot_dir: &Path,
    output_root: &Path,
    news_policy: Option<&Value>,
    news_ai_assessment: Option<&Value>,
    ledger: Option<&Path>,
    artifact_profile: &str,
) -> Result<Value, SdError> {
    let options = AnalysisOptions {
        news_policy,
        news_ai_assessment,
        ledger_path: ledger,
        artifact_profile,
        now: None,
    };
    let location =
        run_analysis(snapshot_dir, output_root, &options).map_err(|message| sd(&message))?;
    let analysis = read_json(&location.join("analysis.json")).map_err(|message| sd(&message))?;
    let mut result = analysis_result(&location, &analysis);
    if let Some(ledger) = ledger {
        let outcome = record_prediction(
            &analysis,
            ledger,
            Utc::now(),
            RecordOptions {
                import_reason: None,
                bars: None,
                sessions: None,
                snapshot_dir: Some(snapshot_dir),
            },
        );
        match outcome {
            Ok(record) => {
                if let Some(fields) = result.as_object_mut() {
                    fields.insert("ledger".into(), record);
                }
            }
            Err(error) => {
                if let Some(fields) = result.as_object_mut() {
                    fields.insert(
                        "ledger".into(),
                        json!({"status": "error", "error_type": "ValueError", "error": bounded_text(&Value::String(error.to_string()), 600)}),
                    );
                }
            }
        }
    }
    Ok(result)
}

fn run_analyze(args: &crate::research::AnalyzeArgs) -> Result<u8, SdError> {
    let policy = read_optional_json(&args.news_policy, "news policy file").map_err(|m| sd(&m))?;
    let ai = read_optional_json(&args.news_ai_assessment, "news AI assessment file")
        .map_err(|m| sd(&m))?;
    let result = run_analysis_command(
        &args.snapshot_dir,
        &args.output_root,
        policy.as_ref(),
        ai.as_ref(),
        args.ledger.as_deref(),
        &args.artifact_profile,
    )?;
    output::stdout_json(&result);
    Ok(0)
}

fn run_pipeline(args: &crate::research::PipelineArgs) -> Result<u8, SdError> {
    let clock = SystemClock;
    let gc_report = if args.no_gc {
        None
    } else {
        let options = GcOptions {
            retain_days: args.retain_days,
            cache_days: DEFAULT_CACHE_DAYS,
            pending_days: DEFAULT_PENDING_DAYS,
            cache_dir: None,
            dry_run: false,
            throttle_hours: Some(GC_THROTTLE_HOURS),
        };
        Some(
            gc_runtime(&args.output_root, &options, &clock, &AppCleanupGate)
                .unwrap_or_else(|error| json!({"status": "error", "error": bounded_text(&Value::String(error), 600)})),
        )
    };
    let request_value = read_json(&args.request).map_err(|m| sd(&m))?;
    let normalized = normalize_request(&request_value)?.to_json();
    let response_value = read_json(&args.responses).map_err(|m| sd(&m))?;
    let domain_map = match response_value.get("domains") {
        Some(domains) => domains.clone(),
        None => response_value.clone(),
    };
    let Some(domains) = domain_map.as_object() else {
        return Err(sd("responses must contain a domain map"));
    };
    // The pipeline claims to run News, so an omitted policy file means the built-in
    // default policy ({}), exactly like the legacy CLI; `analyze` keeps News optional.
    let policy = Some(
        read_optional_json(&args.news_policy, "news policy file")
            .map_err(|m| sd(&m))?
            .unwrap_or_else(|| json!({})),
    );
    let ai = read_optional_json(&args.news_ai_assessment, "news AI assessment file")
        .map_err(|m| sd(&m))?;

    let mut owned_run: Option<PathBuf> = None;
    let result = (|| -> Result<Value, SdError> {
        let (snapshot_dir, output_root) = if args.cleanup_json {
            let (run_dir, _manifest) =
                create_run_workspace(&args.output_root, &clock, None).map_err(|m| sd(&m))?;
            // Track ownership before any subsequent write/freeze can fail.
            owned_run = Some(run_dir.clone());
            let name = format!(
                "collection_{}.json",
                run_dir.file_name().unwrap_or_default().to_string_lossy()
            );
            let receipt = safe_output_destination(&run_dir.join("collections"), &name)?;
            update_run_workspace(
                &run_dir,
                &json!({"collection_name": name})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            )
            .map_err(|m| sd(&m))?;
            write_receipt(
                &normalized,
                domains,
                &receipt,
                args.captured_at_utc.as_deref(),
                &clock,
            )?;
            let receipt_payload = read_json(&receipt).map_err(|m| sd(&m))?;
            let calendar = library_calendar_for(&receipt_payload, args.calendar_dir.as_deref())?;
            let path = freeze_snapshot(
                &receipt_payload,
                &FreezeOptions {
                    output_root: &run_dir.join("snapshots"),
                    library_calendar: calendar.as_ref(),
                    allow_library_calendar: true,
                    on_snapshot_created: None,
                },
                &clock,
            )?;
            update_run_workspace(
                &run_dir,
                &json!({"snapshot_id": path.file_name().unwrap_or_default().to_string_lossy()})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            )
            .map_err(|m| sd(&m))?;
            (path, run_dir.join("research"))
        } else {
            let receipt = safe_output_destination(
                &args.output_root.join("collections"),
                &collection_name(&clock),
            )?;
            write_receipt(
                &normalized,
                domains,
                &receipt,
                args.captured_at_utc.as_deref(),
                &clock,
            )?;
            let receipt_payload = read_json(&receipt).map_err(|m| sd(&m))?;
            let calendar = library_calendar_for(&receipt_payload, args.calendar_dir.as_deref())?;
            let path = freeze_snapshot(
                &receipt_payload,
                &FreezeOptions {
                    output_root: &args.output_root.join("snapshots"),
                    library_calendar: calendar.as_ref(),
                    allow_library_calendar: true,
                    on_snapshot_created: None,
                },
                &clock,
            )?;
            (path, args.output_root.join("research"))
        };
        let mut result = run_analysis_command(
            &snapshot_dir,
            &output_root,
            policy.as_ref(),
            ai.as_ref(),
            args.ledger.as_deref(),
            &args.artifact_profile,
        )?;
        if let Some(run_dir) = &owned_run {
            let analysis_id = result
                .get("report_dir")
                .and_then(Value::as_str)
                .and_then(|path| Path::new(path).file_name())
                .and_then(|name| name.to_str())
                .ok_or_else(|| sd("analysis result has no report directory identity"))?;
            update_run_workspace(
                run_dir,
                &json!({"state": "awaiting_cleanup", "analysis_id": analysis_id})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            )
            .map_err(|message| sd(&message))?;
            if let Some(fields) = result.as_object_mut() {
                fields.insert(
                    "run_dir".into(),
                    Value::String(run_dir.to_string_lossy().to_string()),
                );
                fields.insert("cleanup_required".into(), Value::Bool(true));
            }
            release_pipeline_lock(run_dir).map_err(|m| sd(&m))?;
        }
        Ok(result)
    })();
    match result {
        Ok(mut value) => {
            if let Some(gc) = gc_report {
                if let Some(fields) = value.as_object_mut() {
                    fields.insert("gc".into(), gc);
                }
            }
            output::stdout_json(&value);
            Ok(0)
        }
        Err(error) => {
            if let Some(run_dir) = owned_run {
                mark_run_failed(&run_dir, error.error_type());
            }
            Err(error)
        }
    }
}

fn run_validate_response(args: &crate::research::ValidateResponseArgs) -> Result<u8, SdError> {
    match validate_final_response(&args.analysis_dir, &args.response_file) {
        Ok(document) => {
            output::stdout_json(&document);
            Ok(0)
        }
        Err(message) => Err(sd(&message)),
    }
}

fn run_ledger_record(args: &crate::research::LedgerRecordArgs) -> Result<u8, SdError> {
    let analysis = read_json(&args.analysis_dir.join("analysis.json")).map_err(|m| sd(&m))?;
    let record = record_prediction(
        &analysis,
        &args.ledger,
        Utc::now(),
        RecordOptions {
            import_reason: args.import_reason.as_deref(),
            bars: None,
            sessions: None,
            snapshot_dir: args.snapshot_dir.as_deref(),
        },
    )
    .map_err(|error| sd(&error.to_string()))?;
    output::stdout_json(&record);
    Ok(0)
}

fn run_ledger_settle(args: &crate::research::LedgerSettleArgs) -> Result<u8, SdError> {
    let result = ledger_settle(
        &args.ledger,
        &args.snapshot_dir,
        args.ticker.as_deref(),
        args.horizon.as_deref(),
        Utc::now(),
    )
    .map_err(|error| sd(&error.to_string()))?;
    output::stdout_json(&result);
    Ok(0)
}

fn run_ledger_audit(args: &crate::research::LedgerAuditArgs) -> Result<u8, SdError> {
    let result = ledger_audit(&args.ledger, Utc::now().date_naive())
        .map_err(|error| sd(&error.to_string()))?;
    output::stdout_json(&result);
    Ok(0)
}

fn run_cleanup(args: &crate::research::CleanupRunArgs) -> Result<u8, SdError> {
    let clock = SystemClock;
    let options = CleanupOptions {
        dry_run: args.dry_run,
        force_incomplete: args.force_incomplete,
        delivery_unverified: args.assume_delivered,
    };
    let result = if let Some(run_dir) = &args.run_dir {
        cleanup_owned_run(
            run_dir,
            args.analysis_dir.as_deref(),
            args.response_file.as_deref(),
            &options,
            &clock,
            &AppCleanupGate,
        )
    } else if let Some(analysis_dir) = &args.analysis_dir {
        cleanup_analysis(
            analysis_dir,
            args.response_file.as_deref(),
            &options,
            &clock,
            &AppCleanupGate,
        )
    } else {
        return Err(sd("provide --run-dir or --analysis-dir"));
    }
    .map_err(|error| sd(&error.to_string()))?;
    output::stdout_json(&result);
    let status = result
        .get("cleanup_status")
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(
        if matches!(status, "complete" | "already_cleaned" | "dry_run") {
            0
        } else {
            2
        },
    )
}

fn run_gc(args: &crate::research::GcArgs) -> Result<u8, SdError> {
    let clock = SystemClock;
    let options = GcOptions {
        retain_days: args.retain_days,
        cache_days: args.cache_days,
        pending_days: args.pending_days,
        cache_dir: args.cache_dir.clone(),
        dry_run: args.dry_run,
        throttle_hours: if args.no_throttle {
            None
        } else {
            Some(GC_THROTTLE_HOURS)
        },
    };
    let result = gc_runtime(&args.output_root, &options, &clock, &AppCleanupGate)
        .map_err(|error| sd(&error))?;
    output::stdout_json(&result);
    let status = result.get("status").and_then(Value::as_str).unwrap_or("");
    Ok(if matches!(status, "success" | "throttled") {
        0
    } else {
        2
    })
}

/// Execute one research command (except `schema`), returning the process exit code.
pub fn execute(command: &ResearchCommand) -> u8 {
    let outcome = match command {
        ResearchCommand::Request(args) => run_request(args),
        ResearchCommand::Adapt(args) => run_adapt(args),
        ResearchCommand::Freeze(args) => run_freeze(args),
        ResearchCommand::NewsCandidates(args) => run_news_candidates(args),
        ResearchCommand::Analyze(args) => run_analyze(args),
        ResearchCommand::Pipeline(args) => run_pipeline(args),
        ResearchCommand::ValidateResponse(args) => run_validate_response(args),
        ResearchCommand::LedgerRecord(args) => run_ledger_record(args),
        ResearchCommand::LedgerSettle(args) => run_ledger_settle(args),
        ResearchCommand::LedgerAudit(args) => run_ledger_audit(args),
        ResearchCommand::CleanupRun(args) => run_cleanup(args),
        ResearchCommand::Gc(args) => run_gc(args),
        ResearchCommand::Schema(_) => return 0,
    };
    match outcome {
        Ok(code) => code,
        Err(error) => error_payload(&error),
    }
}

/// Unused helper kept for symmetry with the legacy CLI's path rendering.
pub fn _report_dir_display(path: &Path) -> String {
    path.to_string_lossy().to_string()
}

#[allow(dead_code)]
fn _keep_frozen_helpers(path: &Path) -> Result<(), String> {
    let _ = load_frozen_result_at(path);
    Ok(())
}

#[allow(dead_code)]
fn _keep_freeze_helper(path: &Path) -> Result<(), String> {
    let _ = freeze_result_at(path, &json!({}), "1970-01-01T00:00:00Z");
    Ok(())
}
