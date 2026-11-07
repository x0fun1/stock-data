"""Bounded HTTP transport with retries, rate limiting, TLS verification and redaction."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from ..common.jsonio import redact_params
from ..common.timeutil import now_utc

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class TransportResult:
    status: str
    url: str
    http_status: int | None = None
    content_type: str = ""
    content: bytes = b""
    data: Any = None
    error: str = ""
    fetched_at: datetime | None = None
    params_redacted: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Transport:
    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        min_interval_seconds: float = 0.2,
        user_agent: str = "stock-research-skill/0.1",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff_seconds = backoff_seconds
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self._last_request: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_request.get(host, 0.0)
        wait = self.min_interval_seconds - (now - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expect: str = "json",
        timeout: float | None = None,
    ) -> TransportResult:
        host = urlsplit(url).netloc
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json,text/plain,*/*"}
        request_headers.update(headers or {})
        redacted = redact_params(params)
        last_error = ""
        for attempt in range(1, self.retries + 2):
            self._throttle(host)
            try:
                response = self.session.get(
                    url,
                    params=dict(params or {}),
                    headers=request_headers,
                    timeout=timeout or self.timeout,
                    verify=True,
                )
            except requests.RequestException as exc:
                last_error = f"network_or_timeout:{type(exc).__name__}"
                if attempt <= self.retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                return TransportResult("error", url, error=last_error, fetched_at=now_utc(), params_redacted=redacted, attempts=attempt)
            content = response.content or b""
            content_type = response.headers.get("Content-Type", "")
            if response.status_code in RETRY_STATUS and attempt <= self.retries:
                last_error = f"http_{response.status_code}"
                time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                continue
            if response.status_code >= 400:
                return TransportResult(
                    "error",
                    url,
                    response.status_code,
                    content_type,
                    content,
                    error=f"http_{response.status_code}",
                    fetched_at=now_utc(),
                    params_redacted=redacted,
                    attempts=attempt,
                )
            if expect == "json":
                stripped = content.lstrip()
                if not stripped or stripped[:1] not in {b"{", b"["}:
                    return TransportResult(
                        "error",
                        url,
                        response.status_code,
                        content_type,
                        content,
                        error="expected_json_got_other",
                        fetched_at=now_utc(),
                        params_redacted=redacted,
                        attempts=attempt,
                    )
                try:
                    data = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return TransportResult(
                        "error",
                        url,
                        response.status_code,
                        content_type,
                        content,
                        error="invalid_json",
                        fetched_at=now_utc(),
                        params_redacted=redacted,
                        attempts=attempt,
                    )
                return TransportResult("ok", url, response.status_code, content_type, content, data=data, fetched_at=now_utc(), params_redacted=redacted, attempts=attempt)
            return TransportResult("ok", url, response.status_code, content_type, content, data=content.decode("utf-8", errors="replace"), fetched_at=now_utc(), params_redacted=redacted, attempts=attempt)
        return TransportResult("error", url, error=last_error or "unknown", fetched_at=now_utc(), params_redacted=redacted, attempts=self.retries + 1)
