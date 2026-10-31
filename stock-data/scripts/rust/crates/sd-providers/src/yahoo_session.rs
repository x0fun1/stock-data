//! Yahoo session bootstrap (`get_yahoo_session`): cookie handshake plus crumb.

use std::collections::BTreeMap;

use crate::http::{HttpRequest, HttpResponse, HttpTransport};
use crate::transport::{JobContext, ProviderError};

/// Label one session-layer failure with the request stage that failed.
///
/// The Yahoo session is three ordered stages (cookie bootstrap, crumb, capability
/// request); a bare `HTTPError` cannot say which one failed, so every transport/status
/// error raised here carries its stage. The error class is preserved unchanged.
fn stage_failure(stage: &str, error: ProviderError) -> ProviderError {
    ProviderError::new(error.kind, format!("{stage} failed: {}", error.message))
}

/// Cookie name/value pairs established by the bootstrap handshake.
pub type CookieJar = BTreeMap<String, String>;

/// One Yahoo Finance session: crumb, request headers and cookies.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct YahooSession {
    /// `v1/test/getcrumb` value.
    pub crumb: String,
    /// Headers every session request carries (the legacy session sets a UA).
    pub headers: BTreeMap<String, String>,
    /// Cookies captured from `Set-Cookie` headers.
    pub cookies: CookieJar,
}

impl YahooSession {
    /// Collect `name=value` pairs from one response's `set-cookie` headers.
    pub fn absorb_cookies(&mut self, response: &HttpResponse) {
        let mut entries: Vec<(String, String)> = Vec::new();
        for (name, value) in &response.headers {
            if name.eq_ignore_ascii_case("set-cookie") {
                if let Some(pair) = value.split(';').next() {
                    if let Some((key, val)) = pair.split_once('=') {
                        entries.push((key.trim().to_string(), val.trim().to_string()));
                    }
                }
            }
        }
        for (key, value) in entries {
            self.cookies.insert(key, value);
        }
    }

    /// `_session_get`: one GET with the session headers and cookies.
    pub fn get(
        &self,
        transport: &dyn HttpTransport,
        context: &mut JobContext,
        url: &str,
        params: &[(&str, String)],
        timeout: f64,
    ) -> Result<HttpResponse, ProviderError> {
        let mut headers = self.headers.clone();
        if !self.cookies.is_empty() {
            let cookie = self
                .cookies
                .iter()
                .map(|(key, value)| format!("{key}={value}"))
                .collect::<Vec<_>>()
                .join("; ");
            headers.insert("Cookie".into(), cookie);
        }
        let request = HttpRequest {
            url: url.to_string(),
            params: params
                .iter()
                .map(|(name, value)| ((*name).to_string(), value.clone()))
                .collect(),
            headers,
            timeout,
        };
        transport.get(&request, context)
    }
}

/// `_bootstrap_yahoo`: the cookie endpoint may answer 404 while setting cookies.
pub fn bootstrap(
    session: &mut YahooSession,
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<(), ProviderError> {
    let response = session
        .get(transport, context, "https://fc.yahoo.com", &[], 10.0)
        .map_err(|error| stage_failure("Yahoo session bootstrap request (fc.yahoo.com)", error))?;
    if response.status != 404 {
        response
            .raise_for_status()
            .map_err(|error| stage_failure("Yahoo session bootstrap response", error))?;
    }
    session.absorb_cookies(&response);
    Ok(())
}

/// `get_yahoo_session`: bootstrap cookies, then fetch and validate the crumb.
pub fn get_yahoo_session(
    transport: &dyn HttpTransport,
    context: &mut JobContext,
) -> Result<YahooSession, ProviderError> {
    // `requests.Session()` seeds these defaults; the legacy bootstrap only overrides the
    // user agent, and the recorded request headers must match byte for byte.
    let mut session = YahooSession {
        headers: BTreeMap::from([
            ("Accept".to_string(), "*/*".to_string()),
            ("Accept-Encoding".to_string(), "gzip, deflate".to_string()),
            ("Connection".to_string(), "keep-alive".to_string()),
            (
                "User-Agent".to_string(),
                crate::transport::USER_AGENT.to_string(),
            ),
        ]),
        ..YahooSession::default()
    };
    bootstrap(&mut session, transport, context)?;
    let response = session
        .get(
            transport,
            context,
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            &[],
            10.0,
        )
        .map_err(|error| {
            stage_failure(
                "Yahoo session crumb request (query2.finance.yahoo.com/v1/test/getcrumb)",
                error,
            )
        })?;
    response.raise_for_status().map_err(|error| {
        stage_failure(
            "Yahoo session crumb response (query2.finance.yahoo.com/v1/test/getcrumb)",
            error,
        )
    })?;
    let crumb = response.text();
    if crumb.trim().is_empty() || crumb.contains('<') || crumb.contains('\n') {
        return Err(stage_failure(
            "Yahoo session crumb validation",
            ProviderError::provider("Yahoo returned an invalid crumb."),
        ));
    }
    session.crumb = crumb;
    Ok(session)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::ProviderErrorKind;

    #[test]
    fn stage_failure_keeps_the_error_class_and_names_the_stage() {
        let wrapped = stage_failure(
            "Yahoo session crumb response",
            ProviderError::new(ProviderErrorKind::HttpError, "403 Client Error: Forbidden"),
        );
        assert_eq!(wrapped.kind, ProviderErrorKind::HttpError);
        assert_eq!(
            wrapped.message,
            "Yahoo session crumb response failed: 403 Client Error: Forbidden"
        );
        let value = stage_failure("bootstrap", ProviderError::value("bad input"));
        assert_eq!(value.kind, ProviderErrorKind::ValueError);
    }

    #[test]
    fn cookie_absorption_keeps_the_cookie_value_without_attributes() {
        let mut session = YahooSession::default();
        let mut response = HttpResponse::new(404, "Not Found", Vec::new());
        response.headers.insert(
            "set-cookie".into(),
            "A1=cookie-value; Path=/; Secure; HttpOnly".into(),
        );
        session.absorb_cookies(&response);
        assert_eq!(
            session.cookies.get("A1").map(String::as_str),
            Some("cookie-value")
        );
    }
}
