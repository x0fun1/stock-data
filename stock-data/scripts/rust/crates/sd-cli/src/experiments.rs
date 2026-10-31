//! `stock-data experiments <name>` - the four pre-registered experiment entry points.
//!
//! They stay isolated from the production prediction route (R11). R01 freezes the
//! argument surface so the parity matrix is complete from the start; execution
//! reports `NotImplementedError` until R11 ports it.

use std::path::PathBuf;

use clap::{Args, Subcommand};
/// Experiment entries, one per legacy script.
#[derive(Debug, Subcommand)]
pub enum ExperimentsCommand {
    /// Pre-registered A/B momentum comparison (`momentum_ab.py`).
    #[command(name = "momentum-ab")]
    MomentumAb(MomentumAbArgs),
    /// Selection statistics (`selection_ab.py`).
    #[command(name = "selection-ab")]
    SelectionAb(PoolExperimentArgs),
    /// Research B A/B protocol statistics (`research_b_ab.py`).
    #[command(name = "research-b-ab")]
    ResearchBAb(PoolExperimentArgs),
    /// Candidate listing across frozen snapshots (`research_candidates.py`).
    #[command(name = "research-candidates")]
    ResearchCandidates(ResearchCandidatesArgs),
}

/// `momentum-ab`
#[derive(Debug, Args)]
pub struct MomentumAbArgs {
    #[arg(long = "collections-dir")]
    pub collections_dir: String,
    #[arg(long = "output-json")]
    pub output_json: String,
    #[arg(long = "output-md")]
    pub output_md: String,
    #[arg(long = "pool-manifest")]
    pub pool_manifest: Option<String>,
}

/// `selection-ab` and `research-b-ab` share the same surface.
#[derive(Debug, Args)]
pub struct PoolExperimentArgs {
    #[arg(long)]
    pub pool: String,
    #[arg(long = "collections-dir")]
    pub collections_dir: String,
    #[arg(long = "output-dir")]
    pub output_dir: String,
    #[arg(long, default_value = "5D")]
    pub horizon: String,
    #[arg(long, default_value = "2026-10-18T17:00:00Z")]
    pub asof: String,
}

/// `research-candidates`
#[derive(Debug, Args)]
pub struct ResearchCandidatesArgs {
    #[arg(long = "snapshot-dir", action = clap::ArgAction::Append, required = true)]
    pub snapshot_dir: Vec<PathBuf>,
    #[arg(long)]
    pub output: Option<PathBuf>,
}

/// Execute an experiment subcommand, returning the process exit code.
pub fn run(command: &ExperimentsCommand) -> u8 {
    crate::experiments_impl::execute(command)
}
