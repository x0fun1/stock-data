"""Project untrusted provider DATA; never interpret it as an execution plan."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

_SENSITIVE = re.compile(r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|cookie|set-cookie|password|secret|crumb|headers)$", re.IGNORECASE)
_QUERY_SENSITIVE = re.compile(r"^(?:api[_-]?key|apikey|key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|authorization|cookie|set-cookie|password|secret|crumb|headers|session|session[_-]?id|sessionid|sid|jsessionid|phpsessid|auth|signature|sig|awsaccesskeyid|x-amz-[a-z-]+)$", re.IGNORECASE)
_QUERY_SUBSTRINGS = ("token", "secret", "password", "signature", "credential")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HEADER_SECRET = re.compile(
    r"(?im)\b((?:proxy-)?authorization|set-cookie|cookie)\b[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\r\n]+(?:\r?\n[ \t][^\r\n]+)*)")
_KEYWORD_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|crumb)\b[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER_SECRET = re.compile(r"(?i)\b(bearer)\s+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])([A-Za-z0-9._~+/=-]{12,}=*)")
_BASIC_SECRET = re.compile(r"(?i)\b(basic)\s+(?=[A-Za-z0-9+/]*[0-9+/=])([A-Za-z0-9+/]{16,}={0,2})")
_TOKEN_SECRET = re.compile(r"(?i)\b(token)\s+(?=[A-Za-z0-9._~+/=-]*\d)([A-Za-z0-9._~+/=-]{12,}=*)")


def _sensitive_query_key(key: str) -> bool:
    lowered = key.lower()
    return bool(_QUERY_SENSITIVE.fullmatch(lowered)) or any(marker in lowered for marker in _QUERY_SUBSTRINGS)


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or _CONTROL.search(value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        query = [(key, "[redacted]" if _sensitive_query_key(key) else val) for key, val in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except ValueError:
        return None


def redact_text(value: Any, *, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            for spelling in {secret, quote(secret, safe=""), quote_plus(secret)}:
                text = text.replace(spelling, "[redacted]")
    text = re.sub(r"https?://[^\s<>\"']+", lambda match: safe_url(match[0]) or "[redacted-url]", text)
    # Header-style secrets (Authorization / Cookie families): mask the whole field value, including
    # quoted values and folded continuation lines, so no scheme prefix or cookie pair survives.
    text = _HEADER_SECRET.sub(r"\1=[redacted]", text)
    # Key/value-style secrets anywhere in free text; quoted values are masked in full.
    text = _KEYWORD_SECRET.sub(r"\1=[redacted]", text)
    # Scheme-prefixed credentials outside header fields (bare Bearer / Basic / Token blobs).
    text = _BEARER_SECRET.sub(r"\1 [redacted]", text)
    text = _BASIC_SECRET.sub(r"\1 [redacted]", text)
    text = _TOKEN_SECRET.sub(r"\1 [redacted]", text)
    return _CONTROL.sub("", text)


def sanitize_data(value: Any) -> Any:
    """Keep data shape, redact credentials and discard externally proposed actions."""
    if isinstance(value, dict):
        return {str(key): ("[redacted]" if _SENSITIVE.fullmatch(str(key)) else sanitize_data(item))
                for key, item in value.items() if str(key).lower() not in {"next_actions", "tool_calls", "function_call"}}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def provider_failed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or ("is_success" in value and value["is_success"] is not True):
        return True
    if str(value.get("status", "")).lower() in {"failed", "error", "failure", "invalid"} or value.get("error") not in (None, "", False):
        return True
    return any(provider_failed(value.get(key)) for key in ("gateway_envelope", "structuredContent", "result"))


def bounded_text(value: Any, limit: int = 600) -> str:
    return redact_text(value).strip()[:limit] if value is not None else ""


def markdown_text(value: Any, limit: int = 600) -> str:
    text = html.escape(bounded_text(value, limit)).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", text)
