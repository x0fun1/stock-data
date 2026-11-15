from __future__ import annotations

import pytest
import requests

from stock_research.acquisition import Transport
from stock_research.acquisition.sources import FinnhubClient, FredClient, assert_source_allowed, parse_company_news
from stock_research.common.errors import SourceNotAllowed
from stock_research.config import load_policies
from stock_research.contracts import EvidenceScope, Grade


class FakeResponse:
    def __init__(self, status_code=200, payload=b"{}", content_type="application/json"):
        self.status_code = status_code
        self.content = payload
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, verify=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "verify": verify})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_transport_retries_and_redacts():
    session = FakeSession([FakeResponse(503, b"busy", "text/html"), FakeResponse(200, b'{"ok": 1}')])
    transport = Transport(session=session, retries=1, backoff_seconds=0, min_interval_seconds=0)
    result = transport.get("https://example.com/data", params={"token": "SECRET", "q": "a"})
    assert result.ok
    assert result.data == {"ok": 1}
    assert result.attempts == 2
    assert result.params_redacted["token"] == "***"
    assert session.calls[0]["verify"] is True


def test_transport_rejects_html_as_json():
    session = FakeSession([FakeResponse(200, b"<html>no</html>", "text/html")])
    result = Transport(session=session, retries=0).get("https://example.com/data")
    assert not result.ok
    assert result.error == "expected_json_got_other"


def test_transport_network_error_is_bounded():
    session = FakeSession([requests.Timeout("slow"), requests.Timeout("slow")])
    result = Transport(session=session, retries=1, backoff_seconds=0).get("https://example.com/data")
    assert not result.ok
    assert result.error.startswith("network_or_timeout")
    assert len(session.calls) == 2


def test_finnhub_headlines_are_title_scope_only():
    events = parse_company_news([{"headline": "Company beats estimates", "url": "https://x", "datetime": 1717416000}], "US:XNAS:X")
    assert len(events) == 1
    assert events[0].evidence_scope is EvidenceScope.TITLE
    assert events[0].grade is Grade.INSUFFICIENT


def test_finnhub_client_returns_events_and_a_redacted_receipt():
    payload = b'[{"headline": "Company signs contract", "url": "https://x", "datetime": 1717416000}]'
    client = FinnhubClient(Transport(session=FakeSession([FakeResponse(200, payload)]), retries=0, min_interval_seconds=0), "SECRET")
    events, receipt = client.company_news("US:XNAS:X", "x", "2024-06-01", "2024-06-05")
    assert [event.title for event in events] == ["Company signs contract"]
    assert receipt.record_count == 1 and receipt.params_redacted["token"] == "***"


def test_fred_client_marks_unpaged_remainder_and_keeps_availability_unknown():
    payload = b'{"count": 3, "observations": [{"date": "2026-01-01", "value": "4.1"}, {"date": "2026-01-02", "value": "."}]}'
    client = FredClient(Transport(session=FakeSession([FakeResponse(200, payload)]), retries=0, min_interval_seconds=0), "SECRET")
    observations, receipt = client.observations("DGS10")
    assert len(observations) == 1 and observations[0].available_at is None
    assert receipt.cap_reached and not receipt.exhausted
    assert receipt.params_redacted["api_key"] == "***"


def test_source_policy_admits_only_message_datasets():
    policies = load_policies()
    assert_source_allowed(policies, "user_import", "HK", "news")
    assert_source_allowed(policies, "fred", "US", "macro")
    for source, market, dataset in (("fred", "HK", "macro"), ("user_import", "US", "prices"), ("eastmoney", "US", "prices")):
        with pytest.raises(SourceNotAllowed):
            assert_source_allowed(policies, source, market, dataset)
