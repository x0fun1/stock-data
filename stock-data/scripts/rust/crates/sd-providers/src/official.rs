//! Official-source access (`official_get`): SEC identity, CBOE authorization, rate
//! limiting, and "confirmed absent" handling for date/frame fallbacks.
//!
//! The configuration is injected (`ProviderEnv`) so tests are deterministic and no
//! environment variable leaks into a differential fixture.

use std::collections::BTreeMap;

use serde_json::Value;

use crate::http::{HttpRequest, HttpResponse, HttpTransport};
use crate::transport::{
    ensure_cboe_authorized, sec_contact, validated_origin, JobContext, ProviderError,
    ProviderErrorKind,
};

/// Provider configuration read once per gateway invocation.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProviderEnv {
    /// `SEC_CONTACT` (company/name plus a real contact email).
    pub sec_contact: Option<String>,
    /// `CBOE_AUTHORIZED=1` recorded by the host.
    pub cboe_authorized: bool,
}

impl ProviderEnv {
    /// Read the process environment (the only place environment variables are consulted).
    pub fn from_process_env() -> Self {
        let environ: BTreeMap<String, String> = std::env::vars().collect();
        let sec_contact = environ.get("SEC_CONTACT").and_then(|value| {
            let trimmed = value.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_string())
        });
        Self {
            sec_contact,
            cboe_authorized: environ.get("CBOE_AUTHORIZED").map(String::as_str) == Some("1"),
        }
    }

    fn environ_map(&self) -> BTreeMap<String, String> {
        let mut environ = BTreeMap::new();
        if let Some(contact) = &self.sec_contact {
            environ.insert("SEC_CONTACT".to_string(), contact.clone());
        }
        if self.cboe_authorized {
            environ.insert("CBOE_AUTHORIZED".to_string(), "1".to_string());
        }
        environ
    }
}

/// Return value of one `official_get` call.
#[derive(Debug, Clone, PartialEq)]
pub struct OfficialResponse {
    /// Raw HTTP response.
    pub response: HttpResponse,
    /// Decoded JSON when `as_json` was requested.
    pub json: Option<Value>,
    /// Decoded text otherwise.
    pub text: String,
}

/// Whether the host is a SEC endpoint.
fn is_sec(hostname: &str) -> bool {
    hostname == "sec.gov" || hostname.ends_with(".sec.gov")
}

/// Whether the host is a CBOE endpoint.
fn is_cboe(hostname: &str) -> bool {
    hostname == "cboe.com" || hostname.ends_with(".cboe.com")
}

/// `_is_object_missing`: only 404 / explicit missing-object errors permit a fallback.
fn is_object_missing(response: &HttpResponse) -> bool {
    crate::transport::is_object_missing(response.status, &response.text())
}

/// `official_get`: rate-limited fixed-origin HTTP with SEC/CBOE configuration gates.
#[allow(clippy::too_many_arguments)]
pub fn official_get(
    url: &str,
    params: &[(&str, String)],
    headers: &BTreeMap<String, String>,
    timeout: f64,
    as_json: bool,
    env: &ProviderEnv,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<OfficialResponse, ProviderError> {
    let origin = validated_origin(url)?;
    let hostname = origin.hostname.clone();
    let mut request_headers: BTreeMap<String, String> = BTreeMap::new();
    if is_cboe(&hostname) {
        ensure_cboe_authorized(&env.environ_map())?;
        request_headers.insert("User-Agent".into(), crate::transport::USER_AGENT.into());
    } else if is_sec(&hostname) {
        let contact = sec_contact(&env.environ_map())?;
        request_headers.insert("User-Agent".into(), contact.clone());
        request_headers.insert("Accept-Encoding".into(), "gzip, deflate".into());
        for (name, value) in headers {
            request_headers.insert(name.clone(), value.clone());
        }
        request_headers.insert("User-Agent".into(), contact);
    } else {
        request_headers.insert("User-Agent".into(), crate::transport::USER_AGENT.into());
    }
    for (name, value) in headers {
        if !is_sec(&hostname) || name.eq_ignore_ascii_case("user-agent") {
            request_headers.insert(name.clone(), value.clone());
        }
    }
    let request = HttpRequest {
        url: url.to_string(),
        params: params
            .iter()
            .map(|(name, value)| ((*name).to_string(), value.clone()))
            .collect(),
        headers: request_headers,
        timeout,
    };
    let response = transport.get(&request, context)?;
    if let Err(error) = response.raise_for_status() {
        if is_object_missing(&response) {
            return Err(ProviderError::data_not_available(format!(
                "HTTP {}: requested resource is not available.",
                response.status
            )));
        }
        return Err(error);
    }
    let text = response.text();
    if as_json {
        let json = serde_json::from_str::<Value>(&text).map_err(|_| {
            ProviderError::new(
                ProviderErrorKind::ValueError,
                "Provider returned invalid JSON.",
            )
        })?;
        Ok(OfficialResponse {
            response,
            json: Some(json),
            text,
        })
    } else {
        Ok(OfficialResponse {
            response,
            json: None,
            text,
        })
    }
}

/// `_cik`: 1-10 ASCII digits, zero-padded to ten.
pub fn normalize_cik(value: Option<&Value>) -> Result<String, ProviderError> {
    let text = match value {
        Some(Value::String(text)) if !text.trim().is_empty() => text.trim().to_string(),
        _ => return Err(ProviderError::value("cik must be a nonempty string.")),
    };
    if !text.is_ascii()
        || !text.chars().all(|character| character.is_ascii_digit())
        || text.len() > 10
    {
        return Err(ProviderError::value("cik must contain 1 to 10 digits."));
    }
    Ok(format!("{text:0>10}"))
}
