//! `stock-data selfcheck` - offline migration checks.
//!
//! `--list` publishes the honest registry (68 active entries with their current
//! implementation status, 24 retired entries with the original reason). A full run
//! returns non-zero while any entry is still `not_implemented`: pending work is
//! never counted as passing.

use clap::Args;

use crate::output;

/// Arguments for `selfcheck`.
#[derive(Debug, Args)]
pub struct SelfcheckArgs {
    /// Run offline checks (no market access, no research artifacts).
    #[arg(long)]
    pub offline: bool,
    /// List every registered check with its implementation status.
    #[arg(long)]
    pub list: bool,
    /// Restrict the run to specific check ids.
    #[arg(long = "check", action = clap::ArgAction::Append)]
    pub check: Vec<String>,
}

/// Execute the selfcheck subcommand, returning the process exit code.
pub fn run(args: &SelfcheckArgs) -> u8 {
    if args.list {
        output::stdout_json(&sd_app::selfcheck::list_document());
        return 0;
    }
    let outcome = sd_app::selfcheck::run(&args.check);
    output::stdout_json(&sd_app::selfcheck::outcome_document(&outcome));
    if outcome.passed {
        0
    } else {
        1
    }
}
