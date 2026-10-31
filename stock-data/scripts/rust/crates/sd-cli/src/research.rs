//! `stock-data research <command>` - the 13 research commands.
//!
//! R01 freezes the argument surface and implements `schema`; every execution path
//! that is not yet ported fails with `NotImplementedError` on stderr and exit 2
//! (the legacy `quant_research.py` channel) instead of printing a fake success.

use std::path::PathBuf;

use clap::{Args, Subcommand};
use serde_json::Value;

use sd_core::error::SdError;

use crate::output;
use crate::schema_generated;

/// Research subcommands, in the legacy order.
#[derive(Debug, Subcommand)]
pub enum ResearchCommand {
    /// Normalize a research request JSON file.
    Request(RequestArgs),
    /// Preserve gateway envelopes and write a normalized collection receipt.
    Adapt(AdaptArgs),
    /// Validate and freeze one captured stock-data payload.
    Freeze(FreezeArgs),
    /// Export every canonical headline for host AI review before analyze.
    #[command(name = "news-candidates")]
    NewsCandidates(NewsCandidatesArgs),
    /// Run isolated research paths against one frozen snapshot.
    Analyze(AnalyzeArgs),
    /// Compose request/adapt/freeze/analyze from already collected gateway DATA.
    Pipeline(PipelineArgs),
    /// Check a prepared reply against frozen Quant/News results before delivery.
    #[command(name = "validate-response")]
    ValidateResponse(ValidateResponseArgs),
    /// Append a frozen analysis to the forward ledger (idempotent).
    #[command(name = "ledger-record")]
    LedgerRecord(LedgerRecordArgs),
    /// Settle matured ledger predictions against a later frozen snapshot.
    #[command(name = "ledger-settle")]
    LedgerSettle(LedgerSettleArgs),
    /// Score realized ledger predictions.
    #[command(name = "ledger-audit")]
    LedgerAudit(LedgerAuditArgs),
    /// Preview or remove JSON owned by a run or explicitly named analysis.
    #[command(name = "cleanup-run")]
    CleanupRun(CleanupRunArgs),
    /// Sweep orphan scratch paths, finish abandoned runs, expire run directories and prune the provider cache.
    Gc(GcArgs),
    /// Print a machine-readable contract.
    Schema(SchemaArgs),
}

/// `request`
#[derive(Debug, Args)]
pub struct RequestArgs {
    #[arg(long)]
    pub input: PathBuf,
    #[arg(long)]
    pub output: PathBuf,
}

/// `adapt`
#[derive(Debug, Args)]
pub struct AdaptArgs {
    #[arg(long)]
    pub request: PathBuf,
    #[arg(long)]
    pub responses: PathBuf,
    #[arg(long, default_value = "collected.json")]
    pub output: PathBuf,
    #[arg(long = "captured-at-utc")]
    pub captured_at_utc: Option<String>,
}

/// `freeze`
#[derive(Debug, Args)]
pub struct FreezeArgs {
    #[arg(long)]
    pub input: PathBuf,
    #[arg(long = "output-root", default_value = "runtime/snapshots")]
    pub output_root: PathBuf,
    /// Explicit verified calendar pack directory (defaults to the mounted Skill assets).
    #[arg(long = "calendar-dir")]
    pub calendar_dir: Option<PathBuf>,
}

/// `news-candidates`
#[derive(Debug, Args)]
pub struct NewsCandidatesArgs {
    #[arg(long = "snapshot-dir")]
    pub snapshot_dir: PathBuf,
    #[arg(long)]
    pub output: PathBuf,
    #[arg(long, default_value = "all", value_parser = ["all", "major"])]
    pub focus: String,
}

/// `analyze`
#[derive(Debug, Args)]
pub struct AnalyzeArgs {
    #[arg(long = "snapshot-dir")]
    pub snapshot_dir: PathBuf,
    #[arg(long = "output-root", default_value = "runtime/research")]
    pub output_root: PathBuf,
    #[arg(long = "news-policy")]
    pub news_policy: Option<PathBuf>,
    #[arg(long = "news-ai-assessment")]
    pub news_ai_assessment: Option<PathBuf>,
    #[arg(long)]
    pub ledger: Option<PathBuf>,
    #[arg(long = "artifact-profile", default_value = "audit", value_parser = ["audit", "compact"])]
    pub artifact_profile: String,
}

/// `pipeline`
#[derive(Debug, Args)]
pub struct PipelineArgs {
    #[arg(long)]
    pub request: PathBuf,
    #[arg(long)]
    pub responses: PathBuf,
    #[arg(long = "output-root", default_value = "runtime")]
    pub output_root: PathBuf,
    #[arg(long = "captured-at-utc")]
    pub captured_at_utc: Option<String>,
    /// Explicit verified calendar pack directory (defaults to the mounted Skill assets).
    #[arg(long = "calendar-dir")]
    pub calendar_dir: Option<PathBuf>,
    #[arg(long = "news-policy")]
    pub news_policy: Option<PathBuf>,
    #[arg(long = "news-ai-assessment")]
    pub news_ai_assessment: Option<PathBuf>,
    #[arg(long)]
    pub ledger: Option<PathBuf>,
    #[arg(long = "artifact-profile", default_value = "audit", value_parser = ["audit", "compact"])]
    pub artifact_profile: String,
    /// Isolate this pipeline run for explicit post-validation JSON cleanup.
    #[arg(long = "cleanup-json", conflicts_with = "keep_json")]
    pub cleanup_json: bool,
    /// Retain JSON in the legacy shared output layout (default).
    #[arg(long = "keep-json")]
    pub keep_json: bool,
    /// Skip the throttled orphan/retention sweep that every pipeline start runs first.
    #[arg(long = "no-gc")]
    pub no_gc: bool,
    #[arg(long = "retain-days", default_value_t = 30)]
    pub retain_days: i64,
}

/// `validate-response`
#[derive(Debug, Args)]
pub struct ValidateResponseArgs {
    #[arg(long = "analysis-dir")]
    pub analysis_dir: PathBuf,
    #[arg(long = "response-file")]
    pub response_file: PathBuf,
}

/// `ledger-record`
#[derive(Debug, Args)]
pub struct LedgerRecordArgs {
    #[arg(long = "analysis-dir")]
    pub analysis_dir: PathBuf,
    #[arg(long)]
    pub ledger: PathBuf,
    #[arg(long = "snapshot-dir")]
    pub snapshot_dir: Option<PathBuf>,
    #[arg(long = "import-reason")]
    pub import_reason: Option<String>,
}

/// `ledger-settle`
#[derive(Debug, Args)]
pub struct LedgerSettleArgs {
    #[arg(long)]
    pub ledger: PathBuf,
    #[arg(long = "snapshot-dir")]
    pub snapshot_dir: PathBuf,
    #[arg(long)]
    pub ticker: Option<String>,
    #[arg(long)]
    pub horizon: Option<String>,
}

/// `ledger-audit`
#[derive(Debug, Args)]
pub struct LedgerAuditArgs {
    #[arg(long)]
    pub ledger: PathBuf,
}

/// `cleanup-run`
#[derive(Debug, Args)]
pub struct CleanupRunArgs {
    #[arg(long = "run-dir")]
    pub run_dir: Option<PathBuf>,
    #[arg(long = "analysis-dir")]
    pub analysis_dir: Option<PathBuf>,
    #[arg(long = "response-file")]
    pub response_file: Option<PathBuf>,
    #[arg(long = "dry-run")]
    pub dry_run: bool,
    #[arg(long = "force-incomplete")]
    pub force_incomplete: bool,
    #[arg(long = "assume-delivered")]
    pub assume_delivered: bool,
}

/// `gc`
#[derive(Debug, Args)]
pub struct GcArgs {
    #[arg(long = "output-root", default_value = "runtime")]
    pub output_root: PathBuf,
    #[arg(long = "retain-days", default_value_t = 30)]
    pub retain_days: i64,
    #[arg(long = "pending-days", default_value_t = 1)]
    pub pending_days: i64,
    #[arg(long = "cache-dir")]
    pub cache_dir: Option<PathBuf>,
    #[arg(long = "cache-days", default_value_t = 7)]
    pub cache_days: i64,
    #[arg(long = "dry-run")]
    pub dry_run: bool,
    #[arg(long = "no-throttle")]
    pub no_throttle: bool,
}

/// `schema`
#[derive(Debug, Args)]
pub struct SchemaArgs {
    #[arg(long, value_parser = ["request", "collection", "gateway-responses", "researcher", "final", "news"])]
    pub kind: String,
}

/// Execute a research subcommand, returning the process exit code.
pub fn run(command: &ResearchCommand) -> u8 {
    match command {
        ResearchCommand::Schema(args) => run_schema(&args.kind),
        other => crate::research_impl::execute(other),
    }
}

fn run_schema(kind: &str) -> u8 {
    let Some(document) = schema_generated::schema_document(kind) else {
        let error = SdError::validation(format!("unknown schema kind: {kind}"));
        output::stderr_json(&output::research_error(&error));
        return 2;
    };
    let value: Value = match serde_json::from_str(document) {
        Ok(value) => value,
        Err(error) => {
            let error = SdError::validation(format!("embedded schema is corrupt: {error}"));
            output::stderr_json(&output::research_error(&error));
            return 2;
        }
    };
    // `json.dumps(..., ensure_ascii=True, indent=2)`.
    println!("{}", sd_core::json_text::python_pretty_ascii(&value));
    0
}
