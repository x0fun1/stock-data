//! Provider transport policy (`global_stock_data.py` constants and guards).
//!
//! Every network access goes through one fixed-origin HTTPS transport with an explicit
//! allowlist, bounded decoded bodies, same-origin redirects only, a per-job request
//! budget, host rate limits and credential redaction. The checks here are pure policy;
//! the actual HTTP client is injected so tests never touch the network.

use std::collections::BTreeSet;
use std::path::Path;

/// Fixed provider hostnames (Python `PROVIDER_HOSTS`).
pub const PROVIDER_HOSTS: [&str; 18] = [
    "hq.sinajs.cn",
    "qt.gtimg.cn",
    "stock.finance.sina.com.cn",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "searchapi.eastmoney.com",
    "fc.yahoo.com",
    "query2.finance.yahoo.com",
    "www.sec.gov",
    "sec.gov",
    "data.sec.gov",
    "efts.sec.gov",
    "cdn.cboe.com",
    "cdn.finra.org",
    "home.treasury.gov",
    "publicreporting.cftc.gov",
    "api.nasdaq.com",
];

/// Maximum decoded response bytes (Python `MAX_RESPONSE_BYTES`).
pub const MAX_RESPONSE_BYTES: usize = 20 * 1024 * 1024;
/// Redirect attempts before failing (Python allows the original plus 3 redirects).
pub const REDIRECT_LIMIT: usize = 4;
/// User agent used by the HTTP sources.
pub const USER_AGENT: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36";

/// Error classes of the provider layer, named like the Python exceptions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderErrorKind {
    /// `ValueError` - invalid caller input.
    ValueError,
    /// `ProviderError` - the provider reported an error or an invalid response.
    ProviderError,
    /// `ConfigurationError` - required configuration is missing.
    ConfigurationError,
    /// `AuthorizationRequired` - the caller has not recorded provider authorization.
    AuthorizationRequired,
    /// `DataNotAvailable` - the resource exists but has no data.
    DataNotAvailable,
    /// `KeyError` - a required mapping key is missing.
    KeyError,
    /// `TypeError` - a comparison between incompatible values failed.
    TypeError,
    /// `HTTPError` - the provider answered with an error status.
    HttpError,
    /// `BudgetExceeded` - the job request budget is exhausted.
    BudgetExceeded,
    /// `DeadlineExceeded` - the job deadline is exhausted.
    DeadlineExceeded,
}

impl ProviderErrorKind {
    /// Python exception type name.
    pub fn error_type(self) -> &'static str {
        match self {
            Self::ValueError => "ValueError",
            Self::ProviderError => "ProviderError",
            Self::ConfigurationError => "ConfigurationError",
            Self::AuthorizationRequired => "AuthorizationRequired",
            Self::DataNotAvailable => "DataNotAvailable",
            Self::KeyError => "KeyError",
            Self::TypeError => "TypeError",
            Self::HttpError => "HTTPError",
            Self::BudgetExceeded => "BudgetExceeded",
            Self::DeadlineExceeded => "DeadlineExceeded",
        }
    }
}

/// One provider failure with the Python exception class name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderError {
    /// Python exception class.
    pub kind: ProviderErrorKind,
    /// Redacted message.
    pub message: String,
}

impl ProviderError {
    /// Build a provider-layer error.
    pub fn new(kind: ProviderErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    /// Caller input error (`ValueError`).
    pub fn value(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::ValueError, message)
    }

    /// Provider response error (`ProviderError`).
    pub fn provider(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::ProviderError, message)
    }

    /// Missing configuration (`ConfigurationError`).
    pub fn configuration(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::ConfigurationError, message)
    }

    /// Missing authorization (`AuthorizationRequired`).
    pub fn authorization(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::AuthorizationRequired, message)
    }

    /// Confirmed absent resource (`DataNotAvailable`).
    pub fn data_not_available(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::DataNotAvailable, message)
    }

    /// Job request budget exhausted (`BudgetExceeded`).
    pub fn budget(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::BudgetExceeded, message)
    }

    /// Job deadline exhausted (`DeadlineExceeded`).
    pub fn deadline(message: impl Into<String>) -> Self {
        Self::new(ProviderErrorKind::DeadlineExceeded, message)
    }
}

impl std::fmt::Display for ProviderError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ProviderError {}

/// One parsed HTTPS origin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Origin {
    /// Lowercase hostname.
    pub hostname: String,
    /// Explicit port (only `None`/443 accepted).
    pub port: Option<u16>,
}

/// Parse and validate one URL against the fixed HTTPS provider allowlist.
///
/// Mirrors `_validated_origin`: scheme must be `https`, the host must be listed, no
/// userinfo and no non-443 port. The path/query are not inspected here.
pub fn validated_origin(url: &str) -> Result<Origin, ProviderError> {
    let Some((scheme, rest)) = url.split_once("://") else {
        return Err(outside_allowlist());
    };
    if !scheme.eq_ignore_ascii_case("https") {
        return Err(outside_allowlist());
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or(rest);
    if authority.is_empty() || authority.contains('@') {
        return Err(outside_allowlist());
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port_text))
            if !port_text.is_empty() && port_text.chars().all(|c| c.is_ascii_digit()) =>
        {
            let port: u16 = port_text.parse().map_err(|_| outside_allowlist())?;
            (host, Some(port))
        }
        _ => (authority, None),
    };
    let host = host.trim_matches(['[', ']']).to_ascii_lowercase();
    if host.is_empty() || !PROVIDER_HOSTS.contains(&host.as_str()) {
        return Err(outside_allowlist());
    }
    if !matches!(port, None | Some(443)) {
        return Err(outside_allowlist());
    }
    Ok(Origin {
        hostname: host,
        port,
    })
}

fn outside_allowlist() -> ProviderError {
    ProviderError::value("Network origin is outside the configured provider allowlist.")
}

/// Resolve a redirect target; only same-origin HTTPS targets are accepted.
pub fn same_origin_redirect(
    current: &Origin,
    location: &str,
    base_url: &str,
) -> Result<String, ProviderError> {
    let target = if location.starts_with("https://") || location.starts_with("http://") {
        location.to_string()
    } else {
        let base = base_url.split(['?', '#']).next().unwrap_or(base_url);
        let directory = match base.rfind('/') {
            Some(index) if index > "https://".len() => &base[..=index],
            _ => base,
        };
        if location.starts_with('/') {
            let prefix = format!("https://{}", current.hostname);
            format!("{prefix}{location}")
        } else {
            format!("{directory}{location}")
        }
    };
    let destination = validated_origin(&target)?;
    if destination.hostname != current.hostname {
        return Err(ProviderError::provider(
            "Provider redirect left the original HTTPS origin or exceeded the limit.",
        ));
    }
    Ok(target)
}

/// Bounded request budget for one gateway job (timeout/deadline/requests/bytes).
#[derive(Debug, Clone)]
pub struct JobContext {
    /// Maximum decoded bytes for the whole job.
    pub max_response_bytes: usize,
    /// Maximum requests for the whole job (wire requests, including redirect hops).
    pub max_requests: usize,
    /// Redirects allowed per request.
    pub redirect_limit: usize,
    /// Per-request timeout ceiling for the whole job (seconds).
    pub timeout: f64,
    /// Total monotonic job deadline (seconds).
    pub deadline_seconds: f64,
    /// Monotonic job start.
    pub started: std::time::Instant,
    /// Decoded bytes consumed so far.
    pub decoded_bytes: usize,
    /// Requests issued so far.
    pub requests: usize,
    /// Whether the job has been cancelled by its deadline.
    pub cancelled: bool,
}

impl Default for JobContext {
    fn default() -> Self {
        Self::new(15.0, 120.0, 128, MAX_RESPONSE_BYTES)
    }
}

impl JobContext {
    /// Build one job budget. All transports (native, session bootstrap, batch slices and
    /// the compatibility fallback) must share the same instance.
    pub fn new(
        timeout: f64,
        deadline_seconds: f64,
        max_requests: usize,
        max_response_bytes: usize,
    ) -> Self {
        Self {
            max_response_bytes,
            max_requests: max_requests.max(1),
            redirect_limit: REDIRECT_LIMIT,
            timeout: if timeout.is_finite() && timeout > 0.0 {
                timeout
            } else {
                0.001
            },
            deadline_seconds: if deadline_seconds.is_finite() && deadline_seconds > 0.0 {
                deadline_seconds
            } else {
                0.001
            },
            started: std::time::Instant::now(),
            decoded_bytes: 0,
            requests: 0,
            cancelled: false,
        }
    }

    /// Seconds left before the total deadline (never negative).
    pub fn remaining_seconds(&self) -> f64 {
        let elapsed = self.started.elapsed().as_secs_f64();
        (self.deadline_seconds - elapsed).max(0.0)
    }

    /// Effective timeout for one wire request: caller timeout and job timeout are both
    /// ceilings, and the remaining job deadline always wins.
    pub fn effective_timeout(&self, requested: f64) -> Result<f64, ProviderError> {
        self.ensure_available()?;
        let requested = if requested.is_finite() && requested > 0.0 {
            requested
        } else {
            self.timeout
        };
        Ok(requested
            .min(self.timeout)
            .min(self.remaining_seconds())
            .max(0.001))
    }

    /// Check the deadline and whether any request budget remains without consuming it.
    pub fn ensure_available(&self) -> Result<(), ProviderError> {
        if self.cancelled {
            return Err(ProviderError::deadline("Provider job was cancelled."));
        }
        if self.remaining_seconds() <= 0.0 {
            return Err(ProviderError::deadline("Provider job deadline exhausted."));
        }
        if self.requests >= self.max_requests {
            return Err(ProviderError::budget("Provider request budget exhausted."));
        }
        Ok(())
    }

    /// Record one wire request (including redirect hops); fails once the budget/deadline
    /// is exhausted.
    pub fn take_request(&mut self) -> Result<(), ProviderError> {
        self.ensure_available()?;
        self.requests += 1;
        Ok(())
    }

    /// Record decoded bytes; fails once the size budget is exhausted.
    pub fn take_bytes(&mut self, bytes: usize) -> Result<(), ProviderError> {
        let total = self.decoded_bytes.saturating_add(bytes);
        if total > self.max_response_bytes {
            return Err(ProviderError::provider(
                "Provider response exceeds the size limit.",
            ));
        }
        self.decoded_bytes = total;
        Ok(())
    }

    /// Mark the job cancelled (deadline hit by the caller).
    pub fn cancel(&mut self) {
        self.cancelled = true;
    }
}

/// Rate limiter table (`_LIMITS`): minimum spacing between requests per source.
#[derive(Debug, Clone, Copy)]
pub struct RateLimits {
    /// SEC hard limit is 10/s; the legacy implementation uses 8/s.
    pub sec_per_second: f64,
    /// FINRA limit.
    pub finra_per_second: f64,
    /// CBOE limit.
    pub cboe_per_second: f64,
    /// Nasdaq limit.
    pub nasdaq_per_second: f64,
    /// Everything else.
    pub default_per_second: f64,
}

impl Default for RateLimits {
    fn default() -> Self {
        Self {
            sec_per_second: 8.0,
            finra_per_second: 4.0,
            cboe_per_second: 4.0,
            nasdaq_per_second: 2.0,
            default_per_second: 5.0,
        }
    }
}

impl RateLimits {
    /// Requests per second allowed for one host (mirrors `_limiter_for`).
    pub fn for_host(&self, hostname: &str) -> f64 {
        let host = hostname.to_ascii_lowercase();
        let matches = |domain: &str| host == domain || host.ends_with(&format!(".{domain}"));
        if matches("sec.gov") {
            self.sec_per_second
        } else if matches("finra.org") {
            self.finra_per_second
        } else if matches("cboe.com") {
            self.cboe_per_second
        } else if matches("nasdaq.com") {
            self.nasdaq_per_second
        } else {
            self.default_per_second
        }
    }
}

/// Provider hosts used by the redaction/allowlist tests.
pub fn allowlist() -> BTreeSet<&'static str> {
    PROVIDER_HOSTS.iter().copied().collect()
}

/// SEC contact identity from the environment (`SEC_CONTACT`).
pub fn sec_contact(
    environ: &std::collections::BTreeMap<String, String>,
) -> Result<String, ProviderError> {
    let contact = environ
        .get("SEC_CONTACT")
        .map(|value| value.trim().to_string())
        .unwrap_or_default();
    let lowered = contact.to_ascii_lowercase();
    let placeholder = [
        "example.com",
        "example.org",
        "example.net",
        "domain.com",
        "your-email",
        "your name",
        "your company",
    ]
    .iter()
    .any(|value| lowered.contains(value));
    if !valid_sec_contact(&contact) || placeholder {
        return Err(ProviderError::configuration(
            "Set SEC_CONTACT to a real company/name and contact email before SEC requests.",
        ));
    }
    Ok(contact)
}

fn valid_sec_contact(contact: &str) -> bool {
    // One or more leading non-email words, then an email with a dotted domain.
    let Some((name, email)) = contact.rsplit_once(char::is_whitespace) else {
        return false;
    };
    if name.trim().is_empty() || email.contains(char::is_whitespace) {
        return false;
    }
    let Some((local, domain)) = email.split_once('@') else {
        return false;
    };
    !local.is_empty() && domain.contains('.') && !domain.starts_with('.') && !domain.ends_with('.')
}

/// CBOE authorization gate (`CBOE_AUTHORIZED=1`).
pub fn ensure_cboe_authorized(
    environ: &std::collections::BTreeMap<String, String>,
) -> Result<(), ProviderError> {
    if environ.get("CBOE_AUTHORIZED").map(String::as_str) == Some("1") {
        Ok(())
    } else {
        Err(ProviderError::authorization(
            "CBOE retrieval requires existing authorization and CBOE_AUTHORIZED=1.",
        ))
    }
}

/// Only 404 or an explicit missing-object error permits a date fallback.
///
/// XML `AccessDenied` (403) does not establish absence. The body is inspected as
/// bounded text and never parsed as XML.
pub fn is_object_missing(status_code: u16, body: &str) -> bool {
    if status_code == 404 {
        return true;
    }
    if status_code != 400 && status_code != 403 {
        return false;
    }
    let text: String = body.chars().take(65_536).collect();
    let mut rest = text.as_str();
    while let Some(start) = rest.find("<Code") {
        let after = &rest[start..];
        let Some(open_end) = after.find('>') else {
            break;
        };
        let content_start = &after[open_end + 1..];
        let Some(close) = content_start.find("</") else {
            break;
        };
        let value = content_start[..close].trim();
        if value == "NoSuchKey" || value == "NoSuchObject" {
            return true;
        }
        rest = &content_start[close..];
    }
    false
}

/// Resolve a cache directory argument to an absolute path (`--cache-dir`).
///
/// A leading `~` is expanded against the caller-supplied home; relative paths stay
/// relative to the current directory like Python's `Path(...).expanduser()`.
pub fn normalize_cache_dir(
    raw: &str,
    home: Option<&str>,
) -> Result<std::path::PathBuf, ProviderError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(ProviderError::value("cache_dir must be a nonempty path."));
    }
    let expanded = if trimmed == "~" || trimmed.starts_with("~/") || trimmed.starts_with("~\\") {
        let Some(home) = home else {
            return Err(ProviderError::value(
                "cache_dir cannot be expanded without a home directory.",
            ));
        };
        format!("{}{}", home.trim_end_matches(['/', '\\']), &trimmed[1..])
    } else {
        trimmed.to_string()
    };
    Ok(Path::new(&expanded).to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_allowlist_is_exact() {
        assert!(validated_origin("https://query2.finance.yahoo.com/v8/finance/chart/AAPL").is_ok());
        assert!(validated_origin("https://hq.sinajs.cn/list=gb_aapl").is_ok());
        assert!(validated_origin("http://hq.sinajs.cn/list=gb_aapl").is_err());
        assert!(validated_origin("https://evil.example.com/").is_err());
        assert!(validated_origin("https://user:pass@hq.sinajs.cn/").is_err());
        assert!(validated_origin("https://hq.sinajs.cn:8443/").is_err());
    }

    #[test]
    fn redirects_stay_on_origin() {
        let origin =
            validated_origin("https://query2.finance.yahoo.com/v8/finance/chart/AAPL").unwrap();
        assert!(same_origin_redirect(
            &origin,
            "/v8/finance/chart/MSFT",
            "https://query2.finance.yahoo.com/v8/finance/chart/AAPL"
        )
        .is_ok());
        assert!(same_origin_redirect(
            &origin,
            "https://cdn.cboe.com/x",
            "https://query2.finance.yahoo.com/"
        )
        .is_err());
        assert!(same_origin_redirect(
            &origin,
            "http://query2.finance.yahoo.com/x",
            "https://query2.finance.yahoo.com/"
        )
        .is_err());
    }

    #[test]
    fn budgets_fail_closed() {
        let mut context = JobContext {
            max_requests: 1,
            ..JobContext::default()
        };
        assert!(context.take_request().is_ok());
        assert!(context.take_request().is_err());
        let mut context = JobContext {
            max_response_bytes: 8,
            ..JobContext::default()
        };
        assert!(context.take_bytes(8).is_ok());
        assert!(context.take_bytes(1).is_err());
    }

    #[test]
    fn rate_limits_match_the_legacy_table() {
        let limits = RateLimits::default();
        assert_eq!(limits.for_host("www.sec.gov"), 8.0);
        assert_eq!(limits.for_host("cdn.finra.org"), 4.0);
        assert_eq!(limits.for_host("api.nasdaq.com"), 2.0);
        assert_eq!(limits.for_host("hq.sinajs.cn"), 5.0);
    }

    #[test]
    fn sec_contact_requires_a_real_identity() {
        let mut environ = std::collections::BTreeMap::new();
        assert!(sec_contact(&environ).is_err());
        environ.insert("SEC_CONTACT".into(), "Example Corp ops@example.com".into());
        assert!(sec_contact(&environ).is_err());
        environ.insert("SEC_CONTACT".into(), "Acme Research data@acme.io".into());
        assert_eq!(sec_contact(&environ).unwrap(), "Acme Research data@acme.io");
        assert!(ensure_cboe_authorized(&environ).is_err());
        environ.insert("CBOE_AUTHORIZED".into(), "1".into());
        assert!(ensure_cboe_authorized(&environ).is_ok());
    }

    #[test]
    fn missing_object_detection_is_narrow() {
        assert!(is_object_missing(404, ""));
        assert!(is_object_missing(
            403,
            "<Error><Code>NoSuchKey</Code></Error>"
        ));
        assert!(!is_object_missing(
            403,
            "<Error><Code>AccessDenied</Code></Error>"
        ));
        assert!(!is_object_missing(
            429,
            "<Error><Code>NoSuchKey</Code></Error>"
        ));
    }
}

/// A cheaply cloneable handle to one job budget.
///
/// The collector, the provider policy, the native route layer and the compatibility
/// fallback all hold clones of the same state; there is exactly one request counter and
/// one deadline per CLI job.
#[derive(Debug, Clone)]
pub struct JobState(std::rc::Rc<std::cell::RefCell<JobContext>>);

impl JobState {
    /// Wrap a job budget.
    pub fn new(context: JobContext) -> Self {
        Self(std::rc::Rc::new(std::cell::RefCell::new(context)))
    }

    /// Read-only snapshot of the shared budget (diagnostics/tests).
    pub fn with_borrow<R>(&self, body: impl FnOnce(&JobContext) -> R) -> R {
        body(&self.0.borrow())
    }

    /// Mutably borrow the shared budget.
    pub fn with_mut<R>(&self, body: impl FnOnce(&mut JobContext) -> R) -> R {
        body(&mut self.0.borrow_mut())
    }

    /// Borrow the shared budget mutably (temporary guard; do not hold across calls).
    pub fn borrow_mut(&self) -> std::cell::RefMut<'_, JobContext> {
        self.0.borrow_mut()
    }

    /// Check availability without consuming budget.
    pub fn ensure_available(&self) -> Result<(), ProviderError> {
        self.0.borrow().ensure_available()
    }

    /// Record one wire request against the shared budget.
    pub fn take_request(&self) -> Result<(), ProviderError> {
        self.0.borrow_mut().take_request()
    }

    /// Effective timeout for one wire request.
    pub fn effective_timeout(&self, requested: f64) -> Result<f64, ProviderError> {
        self.0.borrow().effective_timeout(requested)
    }
}
