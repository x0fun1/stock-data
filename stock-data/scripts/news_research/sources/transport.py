"""Bounded HTTPS transport. Credentials and response/error bodies are never logged."""

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov", "finnhub.io", "www.federalreserve.gov", "api.stlouisfed.org"})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SourceError(Exception):
    """A safe, machine-readable acquisition failure, without server/body details."""

    def __init__(self, reason: str, *, unavailable=False, cap_reached=False):
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable
        self.cap_reached = cap_reached


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def retry_after_seconds(value: str | None) -> float:
    """Use a conservative default for missing or invalid Retry-After values."""
    if not value:
        return 60.0
    try:
        if re.fullmatch(r"\d+", value.strip()):
            return max(1.0, float(value))
        return max(1.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 60.0


class Client:
    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self.requests = 0
        self._opener = build_opener(_NoRedirect())
        self._last_request = {}
        self._blocked_until = {}

    def get(self, host: str, path: str, *, params=None, headers=None) -> bytes:
        if host not in ALLOWED_HOSTS or not path.startswith("/") or path.startswith("//"):
            raise SourceError("endpoint_not_allowed")
        if any(part in path for part in ("?", "#", "\\", "..")):
            raise SourceError("endpoint_not_allowed")
        if self.requests >= self.max_requests:
            raise SourceError("request_budget_exhausted", cap_reached=True)
        now = time.monotonic()
        if self._blocked_until.get(host, 0) > now:
            raise SourceError("retry_after_deferred")
        # Deliberately below SEC's published ceiling, and bounded for every host.
        interval = 0.2 if host.endswith("sec.gov") else 0.1
        pause = interval - (now - self._last_request.get(host, 0))
        if pause > 0:
            time.sleep(pause)
        url = f"https://{host}{path}"
        if params:
            url += "?" + urlencode(params)
        if urlsplit(url).hostname != host:
            raise SourceError("endpoint_not_allowed")
        request_headers = {"User-Agent": "stock-data-news/1", "Accept": "application/json, application/xml, text/html"}
        request_headers.update(headers or {})
        self.requests += 1
        self._last_request[host] = time.monotonic()
        request = Request(url, headers=request_headers, method="GET")  # noqa: S310 -- constant HTTPS hosts and validated paths above
        try:
            with self._opener.open(request, timeout=10) as response:
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
                    raise SourceError("response_size_limit", cap_reached=True)
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SourceError("response_size_limit", cap_reached=True)
                return body
        except HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            if code in {429, 503} or retry_after:
                self._blocked_until[host] = time.monotonic() + retry_after_seconds(retry_after)
            raise SourceError(f"http_{code}", unavailable=code in {401, 403}) from None
        except (URLError, TimeoutError, OSError):
            raise SourceError("network_or_timeout") from None

    def json(self, host: str, path: str, *, params=None, headers=None):
        try:
            return json.loads(self.get(host, path, params=params, headers=headers))
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("invalid_json_response") from None
