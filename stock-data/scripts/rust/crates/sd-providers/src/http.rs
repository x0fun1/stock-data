//! Fixed-origin HTTPS transport used by the provider adapters (R13-R16).
//!
//! One request at a time goes through the R12 policy (`validated_origin`, same-origin
//! redirects, bounded bodies, request/byte budgets, per-host rate limits). TLS certificate
//! verification is always enabled; the client never follows redirects on its own, never
//! accepts invalid certificates and never retries unboundedly. Tests inject a mock
//! transport and never touch the network.

use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::transport::{
    validated_origin, JobContext, ProviderError, RateLimits, MAX_RESPONSE_BYTES, REDIRECT_LIMIT,
};

/// One provider request (URL already built; params are encoded by the transport).
#[derive(Debug, Clone, PartialEq)]
pub struct HttpRequest {
    /// Absolute HTTPS URL without query string.
    pub url: String,
    /// Ordered query parameters (values already stringified like `requests`).
    pub params: Vec<(String, String)>,
    /// Header map in insertion order.
    pub headers: BTreeMap<String, String>,
    /// Timeout in seconds.
    pub timeout: f64,
}

impl HttpRequest {
    /// Build a request with no extra headers.
    pub fn new(url: impl Into<String>, params: &[(&str, String)], timeout: f64) -> Self {
        Self {
            url: url.into(),
            params: params
                .iter()
                .map(|(name, value)| ((*name).to_string(), value.clone()))
                .collect(),
            headers: BTreeMap::new(),
            timeout,
        }
    }

    /// Add one header.
    pub fn with_header(mut self, name: &str, value: &str) -> Self {
        self.headers.insert(name.to_string(), value.to_string());
        self
    }

    /// Full URL with an RFC 3986 query string (spaces as `%20`, like `requests`).
    pub fn full_url(&self) -> String {
        if self.params.is_empty() {
            return self.url.clone();
        }
        let query: Vec<String> = self
            .params
            .iter()
            .map(|(name, value)| format!("{}={}", percent_encode(name), percent_encode(value)))
            .collect();
        format!("{}?{}", self.url, query.join("&"))
    }
}

/// Percent-encode one query component (UTF-8 bytes, unreserved characters kept).
pub fn percent_encode(text: &str) -> String {
    let mut encoded = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                encoded.push(*byte as char)
            }
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

/// Observed flags of one failed wire request (`reqwest::Error` accessors).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct FailureFlags {
    timeout: bool,
    connect: bool,
    redirect: bool,
    status: bool,
    body: bool,
    decode: bool,
    request: bool,
}

/// Stable category of one failed wire request.
///
/// The category is derived from the client's flags plus its bounded source chain so an
/// operator can tell DNS, TCP-connect, TLS and timeout failures apart. Only the error
/// chain is inspected; request URLs, query strings and headers are never copied here.
fn classify_failure(flags: FailureFlags, chain_text: &str) -> &'static str {
    let text = chain_text.to_ascii_lowercase();
    let has = |needle: &str| text.contains(needle);
    if flags.connect && (flags.timeout || has("timed out") || has("timeout")) {
        return "connect_timeout";
    }
    if flags.timeout {
        return "timeout";
    }
    if has("dns") || has("lookup address") || has("name or service not known") {
        return "dns";
    }
    if has("certificate") || has("tls") || has("handshake") || has("ssl") || has("invalid peer") {
        return "tls";
    }
    if flags.connect
        || has("tcp connect")
        || has("connection refused")
        || has("connection reset")
        || has("network is unreachable")
    {
        return "connect";
    }
    if flags.redirect {
        return "redirect";
    }
    if flags.status {
        return "status";
    }
    if flags.body {
        return "body";
    }
    if flags.decode {
        return "decode";
    }
    if flags.request {
        return "request";
    }
    "transport"
}

/// Bounded `Display` chain of one transport error (`error` plus at most four sources).
fn error_chain(error: &(dyn std::error::Error + 'static)) -> Vec<String> {
    const MAX_PARTS: usize = 5;
    const MAX_CHARS: usize = 240;
    let mut parts: Vec<String> = Vec::new();
    let mut current: Option<&(dyn std::error::Error + 'static)> = Some(error);
    while let Some(entry) = current {
        let text: String = entry.to_string().chars().take(MAX_CHARS).collect();
        if !text.is_empty() && parts.last() != Some(&text) {
            parts.push(text);
        }
        if parts.len() >= MAX_PARTS {
            break;
        }
        current = entry.source();
    }
    parts
}

/// Legacy `HTTP request failed` message enriched with the failure category and error chain.
fn transport_failure_message(error: &reqwest::Error) -> String {
    let flags = FailureFlags {
        timeout: error.is_timeout(),
        connect: error.is_connect(),
        redirect: error.is_redirect(),
        status: error.is_status(),
        body: error.is_body(),
        decode: error.is_decode(),
        request: error.is_request(),
    };
    let parts = error_chain(error);
    let category = classify_failure(flags, &parts.join("; "));
    format!("HTTP request failed ({category}): {}", parts.join("; "))
}

/// Legacy `HTTP read failed` message enriched with the I/O error kind.
fn read_failure_message(error: &std::io::Error) -> String {
    format!("HTTP read failed ({:?}): {error}", error.kind())
}

/// One provider response with raw bytes and the final URL after same-origin redirects.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    /// HTTP status code.
    pub status: u16,
    /// Status reason phrase (`Not Found`), used only for legacy error messages.
    pub reason: String,
    /// Decoded body bytes.
    pub body: Vec<u8>,
    /// Response headers (lowercase names).
    pub headers: BTreeMap<String, String>,
    /// Final URL the response came from.
    pub final_url: String,
}

impl HttpResponse {
    /// Build a plain response body.
    pub fn new(status: u16, reason: &str, body: Vec<u8>) -> Self {
        Self {
            status,
            reason: reason.to_string(),
            body,
            headers: BTreeMap::new(),
            final_url: String::new(),
        }
    }

    /// UTF-8 text view (used by JSONP/CSV sources that are already UTF-8).
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).to_string()
    }

    /// Decode `gbk`/`gb18030` text like `requests` after `response.encoding = "gbk"`.
    pub fn gbk_text(&self) -> String {
        let (decoded, _, _) = encoding_rs::GBK.decode(&self.body);
        decoded.to_string()
    }

    /// Parse the body as JSON with the legacy error type on failure.
    pub fn json(&self) -> Result<serde_json::Value, ProviderError> {
        serde_json::from_slice(&self.body)
            .map_err(|_| ProviderError::provider("Provider returned invalid JSON."))
    }

    /// Legacy `raise_for_status`: 4xx/5xx become an `HTTPError`.
    pub fn raise_for_status(&self) -> Result<(), ProviderError> {
        if (400..600).contains(&self.status) {
            let kind = if self.status < 500 {
                "Client"
            } else {
                "Server"
            };
            return Err(ProviderError::new(
                crate::transport::ProviderErrorKind::HttpError,
                format!(
                    "{} {} Error: {} for url: {}",
                    self.status,
                    kind,
                    self.reason,
                    if self.final_url.is_empty() {
                        self.url_placeholder()
                    } else {
                        &self.final_url
                    }
                ),
            ));
        }
        Ok(())
    }

    fn url_placeholder(&self) -> &str {
        ""
    }
}

/// Injected HTTP client.
pub trait HttpTransport {
    /// Perform one fixed-origin HTTPS GET under the shared job budget.
    fn get(
        &self,
        request: &HttpRequest,
        context: &mut JobContext,
    ) -> Result<HttpResponse, ProviderError>;
}

/// Real fixed-origin HTTPS client (TLS verification always enabled).
pub struct ReqwestTransport {
    client: reqwest::blocking::Client,
    limits: RateLimits,
    last_request: Mutex<HashMap<String, Instant>>,
}

impl Default for ReqwestTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl ReqwestTransport {
    /// Build a client with automatic redirects disabled and TLS verification on.
    pub fn new() -> Self {
        let client = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(120))
            .user_agent(crate::transport::USER_AGENT)
            .build()
            .expect("the fixed HTTPS client is buildable");
        Self {
            client,
            limits: RateLimits::default(),
            last_request: Mutex::new(HashMap::new()),
        }
    }

    fn throttle(&self, hostname: &str) {
        let interval = 1.0 / self.limits.for_host(hostname).max(0.001);
        let mut state = match self.last_request.lock() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        };
        let now = Instant::now();
        if let Some(previous) = state.get(hostname) {
            let elapsed = now.duration_since(*previous).as_secs_f64();
            if elapsed < interval {
                std::thread::sleep(Duration::from_secs_f64(interval - elapsed));
            }
        }
        state.insert(hostname.to_string(), Instant::now());
    }
}

impl HttpTransport for ReqwestTransport {
    fn get(
        &self,
        request: &HttpRequest,
        context: &mut JobContext,
    ) -> Result<HttpResponse, ProviderError> {
        let mut current = request.clone();
        let mut current_url = current.url.clone();
        for redirect_count in 0..REDIRECT_LIMIT {
            let timeout = context.effective_timeout(current.timeout)?;
            context.take_request()?;
            let origin = validated_origin(&current_url)?;
            self.throttle(&origin.hostname);
            let mut builder = self
                .client
                .get(&current_url)
                .timeout(Duration::from_secs_f64(timeout.max(0.001)));
            if !current.params.is_empty() {
                builder = builder.query(&current.params);
            }
            for (name, value) in &current.headers {
                builder = builder.header(name, value);
            }
            let mut response = builder
                .send()
                .map_err(|error| ProviderError::provider(transport_failure_message(&error)))?;
            let status = response.status().as_u16();
            if (300..400).contains(&status) {
                let location = response
                    .headers()
                    .get("location")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("")
                    .to_string();
                let destination =
                    crate::transport::same_origin_redirect(&origin, &location, &current_url)?;
                if redirect_count + 1 >= REDIRECT_LIMIT {
                    return Err(ProviderError::provider(
                        "Provider redirect left the original HTTPS origin or exceeded the limit.",
                    ));
                }
                let retry = HttpRequest {
                    url: destination
                        .split('?')
                        .next()
                        .unwrap_or(&destination)
                        .to_string(),
                    params: Vec::new(),
                    headers: current.headers.clone(),
                    timeout: current.timeout,
                };
                current = retry;
                current_url = destination;
                continue;
            }
            if let Some(length) = response.headers().get("content-length") {
                let declared: usize = length
                    .to_str()
                    .ok()
                    .and_then(|value| value.trim().parse().ok())
                    .unwrap_or(0);
                if declared > MAX_RESPONSE_BYTES {
                    return Err(ProviderError::provider(
                        "Provider response exceeds the size limit.",
                    ));
                }
                context.take_bytes(declared)?;
            }
            let mut body: Vec<u8> = Vec::new();
            let mut buffer = [0u8; 65536];
            loop {
                let read = response
                    .read(&mut buffer)
                    .map_err(|error| ProviderError::provider(read_failure_message(&error)))?;
                if read == 0 {
                    break;
                }
                context.take_bytes(read)?;
                body.extend_from_slice(&buffer[..read]);
            }
            let headers: BTreeMap<String, String> = response
                .headers()
                .iter()
                .map(|(name, value)| {
                    (
                        name.as_str().to_ascii_lowercase(),
                        value.to_str().unwrap_or("").to_string(),
                    )
                })
                .collect();
            return Ok(HttpResponse {
                status,
                reason: response
                    .status()
                    .canonical_reason()
                    .unwrap_or("Unknown")
                    .to_string(),
                body,
                headers,
                final_url: current_url,
            });
        }
        Err(ProviderError::provider("Provider redirect limit exceeded."))
    }
}

/// Deterministic transport for differential tests: returns scripted responses and
/// records every request (URL/params/headers) for the fixture comparison.
pub struct MockTransport {
    responses: Mutex<Vec<HttpResponse>>,
    requests: Mutex<Vec<HttpRequest>>,
}

impl MockTransport {
    /// Build a transport that serves the scripted responses in order.
    pub fn new(responses: Vec<HttpResponse>) -> Self {
        Self {
            responses: Mutex::new(responses),
            requests: Mutex::new(Vec::new()),
        }
    }

    /// Requests observed so far.
    pub fn requests(&self) -> Vec<HttpRequest> {
        self.requests
            .lock()
            .map(|state| state.clone())
            .unwrap_or_default()
    }
}

impl HttpTransport for MockTransport {
    fn get(
        &self,
        request: &HttpRequest,
        context: &mut JobContext,
    ) -> Result<HttpResponse, ProviderError> {
        context.take_request()?;
        if let Ok(mut state) = self.requests.lock() {
            state.push(request.clone());
        }
        let mut responses = self
            .responses
            .lock()
            .map_err(|_| ProviderError::provider("mock transport poisoned"))?;
        if responses.is_empty() {
            return Err(ProviderError::provider(
                "mock transport has no scripted response left",
            ));
        }
        let mut response = responses.remove(0);
        if response.final_url.is_empty() {
            // The legacy scripted responses recorded the URL without its query string;
            // error messages must match the fixture byte for byte.
            response.final_url = request.url.clone();
        }
        Ok(response)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_encoding_matches_requests() {
        assert_eq!(percent_encode("A B"), "A%20B");
        assert_eq!(percent_encode("苹果"), "%E8%8B%B9%E6%9E%9C");
        assert_eq!(percent_encode("a+b"), "a%2Bb");
        let request = HttpRequest::new(
            "https://searchapi.eastmoney.com/api/suggest/get",
            &[("input", "苹果".to_string()), ("count", "10".to_string())],
            10.0,
        );
        assert_eq!(
            request.full_url(),
            "https://searchapi.eastmoney.com/api/suggest/get?input=%E8%8B%B9%E6%9E%9C&count=10"
        );
    }

    #[test]
    fn mock_transport_returns_scripted_bodies() {
        let mut response = HttpResponse::new(200, "OK", b"{\"ok\":true}".to_vec());
        response.final_url = "https://hq.sinajs.cn/list=gb_aapl".into();
        let transport = MockTransport::new(vec![response]);
        let request = HttpRequest::new("https://hq.sinajs.cn/list=gb_aapl", &[], 10.0);
        let mut context = JobContext::default();
        let fetched = transport.get(&request, &mut context).expect("response");
        assert_eq!(fetched.status, 200);
        assert_eq!(transport.requests().len(), 1);
        assert_eq!(context.requests, 1);
    }

    #[test]
    fn raise_for_status_uses_the_legacy_message() {
        let mut response = HttpResponse::new(404, "Not Found", Vec::new());
        response.final_url = "https://www.sec.gov/missing.json".into();
        let error = response.raise_for_status().expect_err("status error");
        assert_eq!(error.kind.error_type(), "HTTPError");
        assert_eq!(
            error.message,
            "404 Client Error: Not Found for url: https://www.sec.gov/missing.json"
        );
    }

    #[test]
    fn transport_failure_classification_separates_root_causes() {
        let chain = |text: &str| classify_failure(FailureFlags::default(), text);
        assert_eq!(
            chain("dns error: failed to lookup address information"),
            "dns"
        );
        assert_eq!(chain("tcp connect error: Connection refused"), "connect");
        assert_eq!(chain("network is unreachable"), "connect");
        assert_eq!(chain("invalid peer certificate: UnknownIssuer"), "tls");
        assert_eq!(chain("tls handshake failed"), "tls");
        assert_eq!(
            classify_failure(
                FailureFlags {
                    connect: true,
                    ..FailureFlags::default()
                },
                "tcp connect error: Connection timed out (os error 110)"
            ),
            "connect_timeout"
        );
        assert_eq!(
            classify_failure(
                FailureFlags {
                    timeout: true,
                    ..FailureFlags::default()
                },
                "operation timed out"
            ),
            "timeout"
        );
        assert_eq!(
            classify_failure(
                FailureFlags {
                    redirect: true,
                    ..FailureFlags::default()
                },
                "redirect limit reached"
            ),
            "redirect"
        );
        assert_eq!(
            classify_failure(
                FailureFlags {
                    body: true,
                    ..FailureFlags::default()
                },
                "error decoding response body"
            ),
            "body"
        );
        assert_eq!(chain("something else entirely"), "transport");
    }

    #[test]
    fn error_chain_is_bounded_and_deduplicated() {
        #[derive(Debug)]
        struct Link {
            text: String,
            source: Option<Box<Link>>,
        }
        impl std::fmt::Display for Link {
            fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str(&self.text)
            }
        }
        impl std::error::Error for Link {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                self.source
                    .as_deref()
                    .map(|link| link as &(dyn std::error::Error + 'static))
            }
        }
        let nested = Link {
            text: "root".into(),
            source: Some(Box::new(Link {
                text: "root".into(),
                source: Some(Box::new(Link {
                    text: "leaf".into(),
                    source: None,
                })),
            })),
        };
        assert_eq!(error_chain(&nested), vec!["root", "leaf"]);
        let long = Link {
            text: "x".repeat(400),
            source: None,
        };
        assert_eq!(error_chain(&long)[0].chars().count(), 240);
    }
}
