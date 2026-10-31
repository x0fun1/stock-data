//! Project untrusted provider DATA: redact credentials, never interpret it as an
//! execution plan.
//!
//! Port of `stock-data/scripts/quant_research/security.py`. The Python module uses
//! regular expressions with lookahead assertions for the scheme-prefixed secrets
//! (`Bearer`/`Basic`/`Token`); the `regex` crate has no lookaround, so those three
//! rules are implemented as an explicit scanner with the same accept/reject
//! condition and differential-tested against Python in R02.

use serde_json::{Map, Value};

use crate::contracts::py_str;

const SENSITIVE_KEYS: &[&str] = &[
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "crumb",
    "headers",
];
const DROPPED_ACTION_KEYS: &[&str] = &["next_actions", "tool_calls", "function_call"];
const QUERY_SENSITIVE_KEYS: &[&str] = &[
    "api_key",
    "apikey",
    "key",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "crumb",
    "headers",
    "session",
    "session_id",
    "sessionid",
    "sid",
    "jsessionid",
    "phpsessid",
    "auth",
    "signature",
    "sig",
    "awsaccesskeyid",
];
const QUERY_SENSITIVE_SUBSTRINGS: &[&str] =
    &["token", "secret", "password", "signature", "credential"];

fn is_control(character: char) -> bool {
    matches!(character as u32, 0x00..=0x08 | 0x0b..=0x1f | 0x7f)
}

fn normalise_key(name: &str) -> String {
    name.to_lowercase().replace('-', "_")
}

fn sensitive_key(name: &str) -> bool {
    let normalised = normalise_key(name);
    SENSITIVE_KEYS
        .iter()
        .any(|item| normalise_key(item) == normalised)
}

fn sensitive_query_key(name: &str) -> bool {
    let lowered = name.to_lowercase();
    if QUERY_SENSITIVE_KEYS.iter().any(|item| *item == lowered) {
        return true;
    }
    if QUERY_SENSITIVE_SUBSTRINGS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        return true;
    }
    // `x-amz-*` signing parameters are always sensitive.
    lowered.starts_with("x-amz-")
}

fn percent_encode(text: &str) -> String {
    let mut output = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        let character = *byte as char;
        let safe = character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.' | '~');
        if safe {
            output.push(character);
        } else if character == ' ' {
            output.push('+');
        } else {
            output.push_str(&format!("%{byte:02X}"));
        }
    }
    output
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut output: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok();
                match hex.and_then(|value| u8::from_str_radix(value, 16).ok()) {
                    Some(decoded) => {
                        output.push(decoded);
                        index += 3;
                    }
                    None => {
                        output.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&output).into_owned()
}

/// Rebuild a URL with sensitive query parameters redacted and the fragment removed.
pub fn safe_url(value: &Value) -> Option<String> {
    let Value::String(text) = value else {
        return None;
    };
    if text.chars().count() > 2048 || text.chars().any(is_control) {
        return None;
    }
    let (scheme, rest) = match text.split_once("://") {
        Some((scheme, rest)) => (scheme.to_lowercase(), rest),
        None => return None,
    };
    if scheme != "http" && scheme != "https" {
        return None;
    }
    let rest = rest.split('#').next().unwrap_or("");
    let (authority, path_and_query) = match rest.find(['/', '?']) {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    let authority = authority.rsplit('@').next().unwrap_or(authority);
    // A userinfo component makes the URL fail the legacy check entirely.
    if rest[..rest.len() - path_and_query.len()].contains('@') {
        return None;
    }
    let host = authority.split(':').next().unwrap_or("");
    if host.is_empty() {
        return None;
    }
    let (path, query) = match path_and_query.split_once('?') {
        Some((path, query)) => (path, Some(query)),
        None => (path_and_query, None),
    };
    let Some(query) = query else {
        return Some(format!("{scheme}://{authority}{path}"));
    };
    let mut rebuilt = String::new();
    for (index, pair) in query.split('&').enumerate() {
        if index > 0 {
            rebuilt.push('&');
        }
        let (key, value) = match pair.split_once('=') {
            Some((key, value)) => (key, Some(value)),
            None => (pair, None),
        };
        let decoded_key = percent_decode(key);
        let decoded_value = match value {
            Some(raw) => percent_decode(raw),
            None => String::new(),
        };
        let final_value = if sensitive_query_key(&decoded_key) {
            "[redacted]".to_string()
        } else {
            decoded_value
        };
        rebuilt.push_str(&percent_encode(&decoded_key));
        rebuilt.push('=');
        rebuilt.push_str(&percent_encode(&final_value));
    }
    Some(format!("{scheme}://{authority}{path}?{rebuilt}"))
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn starts_with_ignore_case(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.len() >= needle.len() && haystack[..needle.len()].eq_ignore_ascii_case(needle)
}

/// Header-style secrets: `Authorization: ...`, `Cookie=...`, including quoted
/// values and folded continuation lines. Masks the whole field value.
fn redact_header_fields(text: &str) -> String {
    const NAMES: &[&str] = &[
        "proxy-authorization",
        "authorization",
        "set-cookie",
        "cookie",
    ];
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0usize;
    let mut index = 0usize;
    while index < bytes.len() {
        let boundary_ok = index == 0 || !is_word_byte(bytes[index - 1]);
        if !boundary_ok {
            index += 1;
            continue;
        }
        let mut matched: Option<&str> = None;
        for name in NAMES {
            if starts_with_ignore_case(&bytes[index..], name.as_bytes()) {
                let after = index + name.len();
                // The Python pattern requires `\b` after the name; a plain check on
                // the next byte is equivalent for ASCII input.
                if after >= bytes.len() || !is_word_byte(bytes[after]) {
                    matched = Some(name);
                    break;
                }
            }
        }
        let Some(name) = matched else {
            index += 1;
            continue;
        };
        let mut probe = index + name.len();
        while probe < bytes.len() && (bytes[probe] == b'"' || bytes[probe] == b'\'') {
            probe += 1;
        }
        while probe < bytes.len()
            && bytes[probe].is_ascii_whitespace()
            && bytes[probe] != b'\n'
            && bytes[probe] != b'\r'
        {
            probe += 1;
        }
        if probe >= bytes.len() || (bytes[probe] != b':' && bytes[probe] != b'=') {
            index += 1;
            continue;
        }
        probe += 1;
        while probe < bytes.len()
            && bytes[probe].is_ascii_whitespace()
            && bytes[probe] != b'\n'
            && bytes[probe] != b'\r'
        {
            probe += 1;
        }
        // Value: quoted, or to end of line plus folded continuation lines.
        let mut end = probe;
        if probe < bytes.len() && (bytes[probe] == b'"' || bytes[probe] == b'\'') {
            let quote = bytes[probe];
            let mut scan = probe + 1;
            while scan < bytes.len() && bytes[scan] != quote {
                scan += 1;
            }
            end = (scan + 1).min(bytes.len());
        } else {
            while end < bytes.len() && bytes[end] != b'\n' && bytes[end] != b'\r' {
                end += 1;
            }
            loop {
                let mut look = end;
                if look + 1 < bytes.len() && bytes[look] == b'\r' && bytes[look + 1] == b'\n' {
                    look += 2;
                } else if look < bytes.len() && (bytes[look] == b'\n' || bytes[look] == b'\r') {
                    look += 1;
                } else {
                    break;
                }
                if look < bytes.len() && (bytes[look] == b' ' || bytes[look] == b'\t') {
                    while look < bytes.len() && bytes[look] != b'\n' && bytes[look] != b'\r' {
                        look += 1;
                    }
                    end = look;
                    continue;
                }
                break;
            }
        }
        output.push_str(&text[cursor..index]);
        // Keep the matched spelling verbatim: the Python replacement is `\1=[redacted]`.
        output.push_str(&text[index..index + name.len()]);
        output.push_str("=[redacted]");
        cursor = end;
        index = end.max(index + 1);
    }
    output.push_str(&text[cursor..]);
    output
}

/// Key/value-style secrets anywhere in free text (quoted values masked in full).
fn redact_keyword_fields(text: &str) -> String {
    const NAMES: &[&str] = &[
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "secret",
        "crumb",
    ];
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0usize;
    let mut index = 0usize;
    while index < bytes.len() {
        if index > 0 && is_word_byte(bytes[index - 1]) {
            index += 1;
            continue;
        }
        let mut matched: Option<&str> = None;
        for name in NAMES {
            let variants = if name.contains('_') {
                vec![name.to_string(), name.replace('_', "-")]
            } else {
                vec![name.to_string()]
            };
            for variant in variants {
                if starts_with_ignore_case(&bytes[index..], variant.as_bytes()) {
                    let after = index + variant.len();
                    if after >= bytes.len() || !is_word_byte(bytes[after]) {
                        matched = Some(name);
                        break;
                    }
                }
            }
            if matched.is_some() {
                break;
            }
        }
        let Some(name) = matched else {
            index += 1;
            continue;
        };
        // Recompute the length of the spelling that matched.
        let mut consumed = 0usize;
        for variant in [name.to_string(), name.replace('_', "-")] {
            if starts_with_ignore_case(&bytes[index..], variant.as_bytes()) {
                consumed = variant.len();
                break;
            }
        }
        let mut probe = index + consumed;
        while probe < bytes.len() && (bytes[probe] == b'"' || bytes[probe] == b'\'') {
            probe += 1;
        }
        while probe < bytes.len() && bytes[probe].is_ascii_whitespace() {
            probe += 1;
        }
        if probe >= bytes.len() || (bytes[probe] != b':' && bytes[probe] != b'=') {
            index += 1;
            continue;
        }
        probe += 1;
        while probe < bytes.len() && bytes[probe].is_ascii_whitespace() {
            probe += 1;
        }
        let mut end = probe;
        if probe < bytes.len() && (bytes[probe] == b'"' || bytes[probe] == b'\'') {
            let quote = bytes[probe];
            let mut scan = probe + 1;
            while scan < bytes.len() && bytes[scan] != quote {
                scan += 1;
            }
            end = (scan + 1).min(bytes.len());
        } else {
            while end < bytes.len()
                && !bytes[end].is_ascii_whitespace()
                && !matches!(bytes[end], b',' | b';')
            {
                end += 1;
            }
        }
        output.push_str(&text[cursor..index]);
        output.push_str(&text[index..index + consumed]);
        output.push_str("=[redacted]");
        cursor = end;
        index = end.max(index + 1);
    }
    output.push_str(&text[cursor..]);
    output
}

const SCHEME_TOKEN_CHARS: &[u8] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+/=-";

/// Scheme-prefixed credentials (`Bearer <blob>`, `Basic <blob>`, `Token <blob>`).
///
/// The Python patterns use a lookahead to require at least one "secret-looking"
/// character before masking; the scanner applies the same predicate explicitly.
fn redact_scheme_secrets(text: &str, scheme: &str, min_length: usize, guard: &[u8]) -> String {
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0usize;
    let mut index = 0usize;
    while index < bytes.len() {
        let boundary_ok = index == 0 || !is_word_byte(bytes[index - 1]);
        if !boundary_ok || !starts_with_ignore_case(&bytes[index..], scheme.as_bytes()) {
            index += 1;
            continue;
        }
        let after = index + scheme.len();
        if after < bytes.len() && is_word_byte(bytes[after]) {
            index += 1;
            continue;
        }
        let mut probe = after;
        while probe < bytes.len() && bytes[probe].is_ascii_whitespace() {
            probe += 1;
        }
        if probe == after {
            index += 1;
            continue;
        }
        let mut end = probe;
        while end < bytes.len() && SCHEME_TOKEN_CHARS.contains(&bytes[end]) {
            end += 1;
        }
        let token = &bytes[probe..end];
        if token.len() < min_length || !token.iter().any(|byte| guard.contains(byte)) {
            index += 1;
            continue;
        }
        output.push_str(&text[cursor..end]);
        // Rebuild the tail of the pushed slice as `scheme [redacted]`.
        output.truncate(output.len() - (end - index));
        output.push_str(&text[index..after]);
        output.push_str(" [redacted]");
        cursor = end;
        index = end.max(index + 1);
    }
    output.push_str(&text[cursor..]);
    output
}

fn redact_urls(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0usize;
    let mut index = 0usize;
    while index < bytes.len() {
        let candidate = if starts_with_ignore_case(&bytes[index..], b"http://") {
            Some(7usize)
        } else if starts_with_ignore_case(&bytes[index..], b"https://") {
            Some(8usize)
        } else {
            None
        };
        let Some(prefix) = candidate else {
            index += 1;
            continue;
        };
        let mut end = index + prefix;
        while end < bytes.len() {
            let close = matches!(
                bytes[end],
                b' ' | b'\t' | b'\r' | b'\n' | b'<' | b'>' | b'"' | b'\''
            );
            if close {
                break;
            }
            end += 1;
        }
        let raw = &text[index..end];
        output.push_str(&text[cursor..index]);
        match safe_url(&Value::String(raw.to_string())) {
            Some(url) => output.push_str(&url),
            None => output.push_str("[redacted-url]"),
        }
        cursor = end;
        index = end.max(index + 1);
    }
    output.push_str(&text[cursor..]);
    output
}

/// Redact credentials from free text.
pub fn redact_text(value: &Value) -> String {
    redact_text_with_secrets(value, &[])
}

/// Redact credentials, also masking caller-supplied literal secrets.
pub fn redact_text_with_secrets(value: &Value, secrets: &[String]) -> String {
    let mut text = py_str(value);
    for secret in secrets {
        if secret.is_empty() {
            continue;
        }
        for spelling in [
            secret.clone(),
            percent_encode(secret),
            percent_encode(secret).replace('+', "%20"),
        ] {
            text = text.replace(&spelling, "[redacted]");
        }
    }
    text = redact_urls(&text);
    text = redact_header_fields(&text);
    text = redact_keyword_fields(&text);
    text = redact_scheme_secrets(&text, "bearer", 12, b"0123456789._~+/=-");
    text = redact_scheme_secrets(&text, "basic", 16, b"0123456789+/=");
    text = redact_scheme_secrets(&text, "token", 12, b"0123456789");
    text.chars()
        .filter(|character| !is_control(*character))
        .collect()
}

/// Keep data shape, redact credentials and discard externally proposed actions.
pub fn sanitize_data(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut output = Map::new();
            for (key, item) in object {
                let lowered = key.to_lowercase();
                if DROPPED_ACTION_KEYS.contains(&lowered.as_str()) {
                    continue;
                }
                if sensitive_key(key) {
                    output.insert(key.clone(), Value::String("[redacted]".to_string()));
                } else {
                    output.insert(key.clone(), sanitize_data(item));
                }
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(sanitize_data).collect()),
        Value::String(_) => Value::String(redact_text(value)),
        other => other.clone(),
    }
}

/// True when a provider payload records a failure.
pub fn provider_failed(value: &Value) -> bool {
    let Value::Object(object) = value else {
        return false;
    };
    if object.get("isError") == Some(&Value::Bool(true)) {
        return true;
    }
    if let Some(success) = object.get("is_success") {
        if success != &Value::Bool(true) {
            return true;
        }
    }
    let status = object
        .get("status")
        .map(py_str)
        .unwrap_or_default()
        .to_lowercase();
    if matches!(status.as_str(), "failed" | "error" | "failure" | "invalid") {
        return true;
    }
    // Python: `value.get("error") not in (None, "", False)`, where `0 == False`.
    match object.get("error") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => {}
        Some(Value::String(text)) if text.is_empty() => {}
        Some(Value::Number(number)) if number.as_f64() == Some(0.0) => {}
        Some(_) => return true,
    }
    for key in ["gateway_envelope", "structuredContent", "result"] {
        if let Some(item) = object.get(key) {
            if provider_failed(item) {
                return true;
            }
        }
    }
    false
}

/// Redact and truncate to `limit` characters (never UTF-8 bytes).
pub fn bounded_text(value: &Value, limit: usize) -> String {
    if value.is_null() {
        return String::new();
    }
    redact_text(value).trim().chars().take(limit).collect()
}

/// Escape Markdown metacharacters so external text can never inject structure.
pub fn markdown_text(value: &Value, limit: usize) -> String {
    let text = bounded_text(value, limit).replace(['\n', '\r'], " ");
    let mut output = String::with_capacity(text.len());
    for character in text.chars() {
        if matches!(
            character,
            '\\' | '`'
                | '*'
                | '_'
                | '{'
                | '}'
                | '['
                | ']'
                | '('
                | ')'
                | '#'
                | '+'
                | '!'
                | '|'
                | '<'
                | '>'
        ) {
            output.push('\\');
        }
        output.push(character);
    }
    output
}
