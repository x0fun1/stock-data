//! Bounded Yahoo gateway provider (`yfinance_provider.py`).
//!
//! The native route layer is injected through [`YahooNative`]; a native capability failure
//! falls back to the seven legacy routes through [`YahooFallback`] unless the failure is a
//! terminal policy error. A process-wide job circuit latches the first terminal transport
//! failure so no later call (or compatibility fallback) can silently continue.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use crate::yahoo_compat::{
    fallback as compat_fallback, GatewayRaise, LegacyError, LegacyGateway, ReservePolicy,
};

/// Capability names served by the provider.
pub const NAMES: [&str; 13] = [
    "yahoo_history",
    "yahoo_history_batch",
    "yahoo_quote",
    "yahoo_profile",
    "yahoo_financials",
    "yahoo_search",
    "yahoo_statistics",
    "yahoo_analysis",
    "yahoo_holders",
    "yahoo_options",
    "yahoo_earnings",
    "yahoo_funds",
    "yahoo_screen",
];

const POLICY_ERROR_CODES: [&str; 5] = [
    "BudgetExceeded",
    "DeadlineExceeded",
    "JobCircuitOpen",
    "AccessDenied",
    "RateLimited",
];

const TERMINAL_FALLBACK: [&str; 4] = [
    "RateLimited",
    "AccessDenied",
    "BudgetExceeded",
    "DeadlineExceeded",
];

static JOB_CIRCUIT: Mutex<Option<(String, String)>> = Mutex::new(None);

/// Latch the first terminal transport failure of this process.
pub fn latch_job_circuit(code: &str, message: &str) {
    let mut latch = JOB_CIRCUIT.lock().expect("job circuit lock");
    if latch.is_none() {
        *latch = Some((code.to_string(), message.to_string()));
    }
}

/// Current process-wide job-circuit reason, if any.
pub fn job_circuit_block() -> Option<(String, String)> {
    JOB_CIRCUIT.lock().expect("job circuit lock").clone()
}

/// Clear the process-wide latch (tests and new CLI jobs).
pub fn reset_job_circuit() {
    *JOB_CIRCUIT.lock().expect("job circuit lock") = None;
}

/// One sanitized provider failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayError {
    /// Stable code (`InvalidParameters`, `DataUnavailable`, ...).
    pub code: String,
    /// Redacted message.
    pub message: String,
}

impl GatewayError {
    /// Build one error.
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    /// Invalid-parameter error.
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new("InvalidParameters", message)
    }

    fn unsupported(message: impl Into<String>) -> Self {
        Self::new("UnsupportedCapability", message)
    }
}

/// Shared pretransport budget and streaming limits.
#[derive(Debug, Clone)]
pub struct TransportPolicy {
    /// Single request timeout (seconds).
    pub timeout: f64,
    /// Job deadline (seconds).
    pub deadline: f64,
    /// Maximum requests in the job.
    pub max_requests: usize,
    /// Maximum decoded response bytes.
    pub max_bytes: usize,
    /// Job start instant.
    pub started: Instant,
    /// Requests already accounted.
    pub count: usize,
    /// Latched terminal policy failure.
    pub blocked: Option<(String, String)>,
    /// Sanitized transport records.
    pub records: Vec<Value>,
    /// Sanitized non-terminal failures.
    pub failures: Vec<(String, String)>,
    /// Authentication failures per host/path.
    pub auth_failures: BTreeMap<(String, String), usize>,
    /// Clock used for the deadline (monotonic seconds).
    pub clock: fn() -> f64,
    /// Shared CLI job budget; when present this policy never keeps a second counter.
    pub job: Option<crate::transport::JobState>,
}

fn monotonic_seconds() -> f64 {
    // Instant cannot be converted to a process clock portably; the wall clock offset is
    // irrelevant because only differences are compared.
    use std::sync::OnceLock;
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

impl Default for TransportPolicy {
    fn default() -> Self {
        Self::new(15.0, 120.0, 30, 20 * 1024 * 1024)
    }
}

impl TransportPolicy {
    /// Validate limits and create a policy.
    pub fn new(timeout: f64, deadline: f64, max_requests: usize, max_bytes: usize) -> Self {
        Self {
            timeout,
            deadline,
            max_requests,
            max_bytes,
            started: Instant::now(),
            count: 0,
            blocked: None,
            records: Vec::new(),
            failures: Vec::new(),
            auth_failures: BTreeMap::new(),
            clock: monotonic_seconds,
            job: None,
        }
    }

    /// Bind the policy to the one shared job budget of this CLI invocation.
    pub fn from_job(job: crate::transport::JobState) -> Self {
        Self {
            timeout: 15.0,
            deadline: 120.0,
            max_requests: 30,
            max_bytes: 20 * 1024 * 1024,
            started: Instant::now(),
            count: 0,
            blocked: None,
            records: Vec::new(),
            failures: Vec::new(),
            auth_failures: BTreeMap::new(),
            clock: monotonic_seconds,
            job: Some(job),
        }
    }

    fn job_error(error: crate::transport::ProviderError) -> GatewayError {
        GatewayError::new(error.kind.error_type(), error.message)
    }

    fn elapsed(&self) -> f64 {
        (self.clock)() - (self.clock)()
    }

    /// `before`: account one transport request and return the effective timeout.
    ///
    /// With a shared job the wire transport already consumes the budget, so this only
    /// checks availability and returns the clamped timeout; the provider policy must not
    /// keep a second counter.
    pub fn before(&mut self, _url: &str) -> Result<f64, GatewayError> {
        if let Some((code, message)) = &self.blocked {
            return Err(GatewayError::new(code.clone(), message.clone()));
        }
        if let Some(job) = &self.job {
            match job.effective_timeout(self.timeout) {
                Ok(timeout) => return Ok(timeout),
                Err(error) => {
                    let mapped = Self::job_error(error);
                    self.blocked = Some((mapped.code.clone(), mapped.message.clone()));
                    return Err(mapped);
                }
            }
        }
        let remaining = self.deadline - self.elapsed();
        if remaining <= 0.0 || self.count >= self.max_requests {
            self.blocked = Some((
                "BudgetExceeded".to_string(),
                "Yahoo job deadline or request budget exhausted".to_string(),
            ));
            return Err(GatewayError::new(
                "BudgetExceeded",
                "Yahoo job deadline or request budget exhausted",
            ));
        }
        self.count += 1;
        Ok(self.timeout.min(remaining))
    }

    /// `reserve`: check the shared job can still issue a compatibility request.
    pub fn reserve_call(&mut self) -> Result<(), GatewayError> {
        if let Some((code, message)) = &self.blocked {
            return Err(GatewayError::new(code.clone(), message.clone()));
        }
        if let Some(job) = &self.job {
            if let Err(error) = job.ensure_available() {
                let mapped = Self::job_error(error);
                self.blocked = Some((mapped.code.clone(), mapped.message.clone()));
                return Err(mapped);
            }
            return Ok(());
        }
        if self.count >= self.max_requests {
            self.blocked = Some((
                "BudgetExceeded".to_string(),
                "Yahoo job deadline or request budget exhausted".to_string(),
            ));
            return Err(GatewayError::new(
                "BudgetExceeded",
                "Yahoo job deadline or request budget exhausted",
            ));
        }
        self.count += 1;
        Ok(())
    }
}

impl ReservePolicy for TransportPolicy {
    fn reserve(&mut self) -> Result<(), LegacyError> {
        match self.reserve_call() {
            Ok(()) => Ok(()),
            Err(error) => Err(LegacyError::policy(&error.code, error.message)),
        }
    }
}

/// Native Yahoo capability route.
pub trait YahooNative {
    /// Execute one capability; `Ok` returns the capability envelope (`status`/`data`).
    fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, GatewayError>;
}

/// Compatibility fallback executor used after a native failure.
pub trait YahooFallback {
    /// Run one fallback attempt; `Err` only for terminal policy failures.
    fn run(
        &mut self,
        function: &str,
        params: &Map<String, Value>,
        reason: &Value,
        policy: &mut dyn ReservePolicy,
    ) -> Result<Value, GatewayRaise>;
}

/// Default fallback: the compatibility orchestrator over the seven legacy routes.
pub struct CompatFallback<'a> {
    /// Legacy gateway implementation.
    pub gateway: &'a dyn LegacyGateway,
    /// Injected clock (envelope timestamps).
    pub now: DateTime<Utc>,
}

impl YahooFallback for CompatFallback<'_> {
    fn run(
        &mut self,
        function: &str,
        params: &Map<String, Value>,
        reason: &Value,
        policy: &mut dyn ReservePolicy,
    ) -> Result<Value, GatewayRaise> {
        compat_fallback(
            self.gateway,
            function,
            params,
            reason,
            policy,
            self.now,
            None,
        )
    }
}

/// How the provider receives its cache directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CacheDirMode {
    /// No cache directory; preflight rejects every call.
    Missing,
    /// An explicitly invalid cache path (PathLike normalization failed).
    Invalid,
    /// A normalized string cache directory.
    Directory(String),
}

impl CacheDirMode {
    /// Resolve `cache_dir` from an optional path and `STOCK_DATA_YAHOO_CACHE_DIR`.
    pub fn resolve(cache_dir: Option<PathBuf>) -> Self {
        match cache_dir {
            Some(path) => {
                let text = path.to_string_lossy().to_string();
                if text.is_empty() {
                    CacheDirMode::Missing
                } else {
                    CacheDirMode::Directory(text)
                }
            }
            None => match std::env::var("STOCK_DATA_YAHOO_CACHE_DIR") {
                Ok(text) if !text.is_empty() => CacheDirMode::Directory(text),
                _ => CacheDirMode::Missing,
            },
        }
    }
}

/// Bounded Yahoo provider.
pub struct YahooProvider<'a> {
    /// Normalized cache directory state.
    pub cache_dir: CacheDirMode,
    /// Shared transport policy.
    pub policy: TransportPolicy,
    /// Clock used for date math.
    pub now: DateTime<Utc>,
    native: &'a mut dyn YahooNative,
    fallback: &'a mut dyn YahooFallback,
    /// Whether the deadline is the default (batch calls may extend it).
    pub default_deadline: bool,
    /// Whether the request budget is the default.
    pub default_budget: bool,
}

fn valid_symbol(symbol: &str) -> bool {
    let bytes = symbol.as_bytes();
    if bytes.is_empty() || bytes.len() > 31 {
        return false;
    }
    let first_ok = bytes[0].is_ascii_alphanumeric() || bytes[0] == b'^';
    first_ok
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'^' | b'=' | b'-'))
}

fn string_param(params: &Map<String, Value>, key: &str) -> Option<String> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn py_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|value| value != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

fn has_data(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.values().any(has_data),
        Value::Array(items) => !items.is_empty(),
        Value::Null => false,
        Value::String(text) => !text.is_empty(),
        _ => true,
    }
}

fn parse_iso_datetime(text: &str) -> Option<DateTime<chrono::FixedOffset>> {
    if let Ok(parsed) = DateTime::parse_from_rfc3339(text) {
        return Some(parsed);
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(parsed) = chrono::NaiveDateTime::parse_from_str(text, format) {
            return Some(parsed.and_utc().fixed_offset());
        }
    }
    chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .ok()
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|time| time.and_utc().fixed_offset())
}

impl<'a> YahooProvider<'a> {
    /// Create a provider over one native route and one fallback executor.
    pub fn new(
        cache_dir: CacheDirMode,
        policy: TransportPolicy,
        native: &'a mut dyn YahooNative,
        fallback: &'a mut dyn YahooFallback,
    ) -> Self {
        Self {
            cache_dir,
            policy,
            now: Utc::now(),
            native,
            fallback,
            default_deadline: true,
            default_budget: true,
        }
    }

    /// `call`: native first, compatibility fallback second; terminal errors are latched.
    pub fn call(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        if let Some((code, message)) = job_circuit_block() {
            return Err(GatewayError::new(code, message));
        }
        let result = self.route(name, params);
        if let Some((code, message)) = self.policy.blocked.clone() {
            latch_job_circuit(&code, &message);
        }
        result
    }

    fn route(&mut self, name: &str, params: &Map<String, Value>) -> Result<Value, GatewayError> {
        if !NAMES.contains(&name) {
            return Err(GatewayError::unsupported("Unregistered Yahoo capability"));
        }
        self.preflight(name, params)?;
        let outcome = self.native.call(name, params);
        match outcome {
            Ok(result) => {
                let status = result.get("status").and_then(Value::as_str).unwrap_or("");
                if ["empty", "unavailable", "error", "failed"].contains(&status) {
                    let error = result.get("error").cloned().unwrap_or(Value::Null);
                    let category = error
                        .get("category")
                        .and_then(Value::as_str)
                        .unwrap_or("DataUnavailable")
                        .to_string();
                    let exception_type = error
                        .get("exception_type")
                        .and_then(Value::as_str)
                        .unwrap_or("EmptyYahooResult")
                        .to_string();
                    let reason =
                        serde_json::json!({"category": category, "exception_type": exception_type});
                    self.fallback(name, params, &reason)
                } else {
                    Ok(result)
                }
            }
            Err(error) => {
                if error.code == "InvalidParameters"
                    && error.message != "Unsupported parameters or upstream signature"
                {
                    return Err(error);
                }
                // Explicit native capability gaps (screener, repair) have no equivalent
                // legacy route; returning a NoLegacyRoute envelope would pretend a
                // fallback happened. Trailing financial statements keep falling back.
                let repair_requested = params
                    .get("repair")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                if error.code == "UnsupportedCapability"
                    && (name == "yahoo_screen" || repair_requested)
                {
                    return Err(error);
                }
                let reason = serde_json::json!({
                    "category": error.code,
                    "exception_type": "GatewayError",
                });
                self.fallback(name, params, &reason)
            }
        }
    }

    fn fallback(
        &mut self,
        name: &str,
        params: &Map<String, Value>,
        reason: &Value,
    ) -> Result<Value, GatewayError> {
        if let Some((code, message)) = job_circuit_block() {
            return Err(GatewayError::new(code, message));
        }
        if let Some((code, message)) = self.policy.blocked.clone() {
            return Err(GatewayError::new(code, message));
        }
        let category = reason.get("category").and_then(Value::as_str).unwrap_or("");
        if TERMINAL_FALLBACK.contains(&category) {
            let message = "Yahoo policy failure; legacy fallback is not permitted";
            latch_job_circuit(category, message);
            return Err(GatewayError::new(category, message));
        }
        let result = self.fallback.run(name, params, reason, &mut self.policy);
        match result {
            Ok(value) => Ok(value),
            Err(raised) => {
                if POLICY_ERROR_CODES.contains(&raised.code.as_str()) {
                    Err(GatewayError::new(raised.code, raised.message))
                } else {
                    Err(GatewayError::new("DataUnavailable", raised.message))
                }
            }
        }
    }

    fn preflight(&mut self, name: &str, params: &Map<String, Value>) -> Result<(), GatewayError> {
        if !matches!(self.cache_dir, CacheDirMode::Directory(_)) {
            return Err(GatewayError::invalid(
                "Explicit writable cache_dir required for Yahoo calls",
            ));
        }
        if name == "yahoo_history" || name == "yahoo_history_batch" {
            self.window(params)?;
        }
        if name == "yahoo_history" {
            match params.get("symbol") {
                Some(Value::String(symbol)) if valid_symbol(symbol) => {}
                _ => return Err(GatewayError::invalid("Invalid Yahoo symbol")),
            }
            let basis =
                string_param(params, "price_basis").unwrap_or_else(|| "provider".to_string());
            if basis != "provider" && basis != "adjusted" {
                return Err(GatewayError::invalid(
                    "price_basis must be provider or adjusted",
                ));
            }
        }
        if params.contains_key("symbols") {
            let symbols = match params.get("symbols") {
                Some(Value::Array(items)) => items.clone(),
                _ => return Err(GatewayError::invalid("Batch calls accept 1 to 10 symbols")),
            };
            if symbols.is_empty() || symbols.len() > 10 {
                return Err(GatewayError::invalid("Batch calls accept 1 to 10 symbols"));
            }
            let mut names: Vec<String> = Vec::new();
            for symbol in &symbols {
                match symbol.as_str() {
                    Some(text) if valid_symbol(text) => names.push(text.to_string()),
                    _ => return Err(GatewayError::invalid("Invalid Yahoo symbol")),
                }
            }
            let distinct: std::collections::BTreeSet<&String> = names.iter().collect();
            if distinct.len() != names.len() {
                return Err(GatewayError::invalid("Batch symbols must be distinct"));
            }
            let hk: std::collections::BTreeSet<bool> = names
                .iter()
                .map(|symbol| symbol.to_uppercase().ends_with(".HK"))
                .collect();
            if hk.len() > 1 {
                return Err(GatewayError::invalid(
                    "US and HK batches must be separate calls",
                ));
            }
            let concurrency = params
                .get("concurrency")
                .and_then(Value::as_i64)
                .unwrap_or(1);
            let basis =
                string_param(params, "price_basis").unwrap_or_else(|| "provider".to_string());
            if concurrency != 1 && concurrency != 2 || (basis != "provider" && basis != "adjusted")
            {
                return Err(GatewayError::invalid("Invalid concurrency or price basis"));
            }
        }
        if let Some(symbol) = params.get("symbol") {
            if !symbol.is_null() {
                match symbol.as_str() {
                    Some(text) if valid_symbol(text) => {}
                    _ => return Err(GatewayError::invalid("Invalid Yahoo symbol")),
                }
            }
        }
        Ok(())
    }

    fn window(&mut self, params: &Map<String, Value>) -> Result<(), GatewayError> {
        let start = string_param(params, "start");
        let end = string_param(params, "end");
        let period = string_param(params, "period");
        let interval = string_param(params, "interval").unwrap_or_else(|| "1d".to_string());
        let has_start = py_truthy(params.get("start"));
        let has_end = py_truthy(params.get("end"));
        let has_period = py_truthy(params.get("period"));
        if has_period && (has_start || has_end) {
            return Err(GatewayError::invalid(
                "period and start/end are mutually exclusive",
            ));
        }
        if has_start != has_end {
            return Err(GatewayError::invalid(
                "start and end must be provided together",
            ));
        }
        if ![
            "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo",
        ]
        .contains(&interval.as_str())
        {
            return Err(GatewayError::invalid("Unsupported interval"));
        }
        if has_start {
            let Some(start_text) = start.as_deref() else {
                return Err(GatewayError::invalid("Invalid ISO window"));
            };
            let Some(end_text) = end.as_deref() else {
                return Err(GatewayError::invalid(
                    "start and end must be provided together",
                ));
            };
            let Some(a) = parse_iso_datetime(start_text) else {
                return Err(GatewayError::invalid("Invalid ISO window"));
            };
            let Some(b) = parse_iso_datetime(end_text) else {
                return Err(GatewayError::invalid("Invalid ISO window"));
            };
            if b <= a {
                return Err(GatewayError::invalid("Invalid ISO window"));
            }
            let limit = if interval == "1m" {
                Some(8)
            } else if interval == "60m" || interval == "1h" {
                Some(730)
            } else if interval.ends_with('m') && interval != "1mo" && interval != "3mo" {
                Some(60)
            } else {
                None
            };
            if let Some(limit) = limit {
                if (b - a).num_seconds() as f64 > (limit * 86400) as f64 {
                    return Err(GatewayError::new(
                        "InsufficientCoverage",
                        "Requested intraday window exceeds provider range",
                    ));
                }
                let retention = (self.now - a.with_timezone(&Utc)).num_days();
                if retention > limit {
                    return Err(GatewayError::new(
                        "InsufficientCoverage",
                        "Intraday start outside current provider retention",
                    ));
                }
            }
        } else if let Some(period) = &period {
            if ![
                "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
            ]
            .contains(&period.as_str())
            {
                return Err(GatewayError::invalid("Unsupported period"));
            }
        }
        if !has_start
            && !["1d", "5d", "1wk", "1mo", "3mo"].contains(&interval.as_str())
            && !matches!(period.as_deref(), Some("1d" | "5d" | "1mo"))
        {
            return Err(GatewayError::invalid(
                "Intraday queries require an explicit bounded window",
            ));
        }
        Ok(())
    }
}

#[allow(dead_code)]
fn _keep_has_data(value: &Value) -> bool {
    has_data(value)
}
