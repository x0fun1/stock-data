//! `stock-data collect yahoo` - the native Yahoo collector (R16).
//!
//! Python-compatible CLI: exclusive output creation, stdout JSON errors, exit 1 when the
//! requested collection failed. The provider is the native Yahoo gateway (no Python at
//! runtime); `--cache-dir` is required and forwarded to the provider policy.

use std::path::PathBuf;

use clap::{Args, Subcommand};
use serde_json::{Map, Value};

use sd_providers::collector::{self, CollectProvider};
use sd_providers::http::{HttpTransport, ReqwestTransport};
use sd_providers::transport::{JobContext, JobState, ProviderError};
use sd_providers::yahoo_gateway;

use crate::output;

/// Collector subcommands. Only the Yahoo collector exists in the legacy runtime.
#[derive(Debug, Subcommand)]
pub enum CollectCommand {
    /// Collect market DATA for a research request through the Yahoo routes.
    Yahoo(CollectYahooArgs),
}

/// `collect yahoo`
#[derive(Debug, Args)]
pub struct CollectYahooArgs {
    #[arg(long)]
    pub request: PathBuf,
    /// Domains to collect (only `market` is supported).
    #[arg(long, num_args = 1.., default_value = "market", value_parser = ["market"])]
    pub domains: Vec<String>,
    #[arg(long)]
    pub output: PathBuf,
    #[arg(long = "cache-dir")]
    pub cache_dir: PathBuf,
    /// Explicit verified calendar pack directory (defaults to the mounted Skill assets).
    #[arg(long = "calendar-dir")]
    pub calendar_dir: Option<PathBuf>,
    #[arg(long, default_value_t = 15.0)]
    pub timeout: f64,
    #[arg(long, default_value_t = 120.0)]
    pub deadline: f64,
    #[arg(long = "request-budget", default_value_t = 30)]
    pub request_budget: u32,
}

/// Native provider adapter over `yahoo_gateway::call`.
struct NativeCollectProvider<'a> {
    cache_dir: Option<PathBuf>,
    transport: &'a dyn HttpTransport,
    job: JobState,
}

impl CollectProvider for NativeCollectProvider<'_> {
    fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, ProviderError> {
        yahoo_gateway::call_with_job(
            name,
            params,
            self.cache_dir.clone(),
            self.transport,
            &self.job,
        )
        .map_err(|(code, message)| ProviderError::provider(format!("{code}: {message}")))
    }
}

fn error_payload(category: &str, exception_type: &str) -> Value {
    serde_json::json!({
        "status": "error",
        "error": {"category": category, "exception_type": exception_type},
    })
}

/// Execute the collect subcommand, returning the process exit code.
pub fn run(command: &CollectCommand) -> u8 {
    match command {
        CollectCommand::Yahoo(args) => run_yahoo(args),
    }
}

fn run_yahoo(args: &CollectYahooArgs) -> u8 {
    if args.output.exists() {
        output::stdout_json(&error_payload("InvalidResponse", "FileExistsError"));
        return 1;
    }
    let text = match std::fs::read_to_string(&args.request) {
        Ok(text) => text,
        Err(error) => {
            let mut payload = error_payload("InvalidResponse", "OSError");
            payload["error"]["message"] = Value::String(error.to_string());
            output::stdout_json(&payload);
            return 1;
        }
    };
    let request: Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(error) => {
            let mut payload = error_payload("InvalidResponse", "JSONDecodeError");
            payload["error"]["message"] = Value::String(error.to_string());
            output::stdout_json(&payload);
            return 1;
        }
    };
    if args.timeout <= 0.0 || args.deadline <= 0.0 || args.request_budget < 1 {
        output::stdout_json(&error_payload("InvalidParameters", "ValueError"));
        return 1;
    }
    let pack = match sd_storage::calendar_resource::load_pack(args.calendar_dir.as_deref()) {
        Ok(pack) => pack,
        Err(error) => {
            let mut payload = error_payload("InvalidParameters", "CalendarResourceError");
            payload["error"]["message"] = Value::String(error.to_string());
            output::stdout_json(&payload);
            return 1;
        }
    };
    let market = request
        .get("market")
        .and_then(Value::as_str)
        .unwrap_or("US");
    let schedule = pack.as_ref().and_then(|pack| pack.schedule_value(market));
    let transport = ReqwestTransport::new();
    let job = JobState::new(JobContext::new(
        args.timeout,
        args.deadline,
        args.request_budget as usize,
        sd_providers::transport::MAX_RESPONSE_BYTES,
    ));
    let mut provider = NativeCollectProvider {
        cache_dir: Some(args.cache_dir.clone()),
        transport: &transport,
        job,
    };
    match collector::collect(
        &request,
        &args.domains,
        &mut provider,
        schedule.as_ref(),
        None,
        args.timeout,
        args.deadline,
        args.request_budget,
    ) {
        Ok(responses) => {
            if let Err(message) = collector::write_responses(&responses, &args.output) {
                let mut payload = error_payload("InvalidResponse", "OSError");
                payload["error"]["message"] = Value::String(message);
                output::stdout_json(&payload);
                return 1;
            }
            let failed_all = responses
                .as_object()
                .map(|blocks| {
                    !blocks.is_empty()
                        && blocks.values().all(|block| {
                            block.get("status").and_then(Value::as_str) == Some("failed")
                        })
                })
                .unwrap_or(false);
            let market_failed = responses
                .get("market")
                .and_then(|block| block.get("status"))
                .and_then(Value::as_str)
                == Some("failed");
            if failed_all || market_failed {
                1
            } else {
                0
            }
        }
        Err(message) => {
            let category = if message.contains("domains") || message.contains("invalid") {
                "InvalidParameters"
            } else {
                "InvalidResponse"
            };
            let mut payload = error_payload(category, "ValueError");
            payload["error"]["message"] = Value::String(message);
            output::stdout_json(&payload);
            1
        }
    }
}
