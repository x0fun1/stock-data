//! Native `stock-data` command line entry point.
//!
//! The binary replaces the Python entry points for users; the Python runtime stays
//! available during development as a differential reference only. Exit codes and
//! output channels match the legacy programs (see [`output`]).

mod collect;
mod experiments;
mod experiments_impl;
mod gateway;
mod output;
mod research;
mod research_impl;
mod schema_generated;
mod selfcheck_cmd;

use clap::{Parser, Subcommand};
use std::process::ExitCode;

/// Native runtime for the stock-data Skill: gateway, collection, research, news
/// delivery validation, forward ledger, cleanup and offline selfcheck.
#[derive(Debug, Parser)]
#[command(
    name = "stock-data",
    version = env!("CARGO_PKG_VERSION"),
    about = "Native stock-data Skill runtime (no Python required at run time)",
    long_about = "Native stock-data Skill runtime.\n\n\
        Gateway errors are JSON on stdout with exit 1 (legacy global_stock_data.py); \
        research errors are JSON on stderr with exit 2 (legacy quant_research.py)."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

/// Top-level commands.
#[derive(Debug, Subcommand)]
enum Command {
    /// Local function gateway (56 registered functions).
    Gateway(gateway::GatewayArgs),
    /// Collect provider DATA (native Yahoo collector).
    Collect {
        #[command(subcommand)]
        command: collect::CollectCommand,
    },
    /// Research commands: request/adapt/freeze/.../schema.
    Research {
        #[command(subcommand)]
        command: research::ResearchCommand,
    },
    /// Pre-registered experiment entry points.
    Experiments {
        #[command(subcommand)]
        command: experiments::ExperimentsCommand,
    },
    /// Offline migration selfcheck.
    Selfcheck(selfcheck_cmd::SelfcheckArgs),
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let code = match &cli.command {
        Command::Gateway(args) => gateway::run(args),
        Command::Collect { command } => collect::run(command),
        Command::Research { command } => research::run(command),
        Command::Experiments { command } => experiments::run(command),
        Command::Selfcheck(args) => selfcheck_cmd::run(args),
    };
    ExitCode::from(code)
}
