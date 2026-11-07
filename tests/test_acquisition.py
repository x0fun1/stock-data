from __future__ import annotations

import json
from datetime import date

import pytest
import requests

from stock_research.acquisition import Transport
from stock_research.acquisition.sources import (
    EastmoneyClient,
    FinnhubClient,
    parse_companyfacts,
    parse_company_news,
    parse_klines,
    parse_sina_rows,
    secid_for,
)
from stock_research.acquisition.sources.sina import SinaClient
from stock_research.common.errors import DataError
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


SEC_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"val": 100, "start": "2024-01-01", "end": "2024-03-31", "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-05-01", "accn": "0001"},
                        {"val": 220, "start": "2024-01-01", "end": "2024-06-30", "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-08-01", "accn": "0002"},
                    ]
                }
            }
        }
    }
}


def test_sec_companyfacts_parse_carries_estimated_availability():
    facts = parse_companyfacts(SEC_FACTS, "US:XNAS:X", "0000320193")
    assert len(facts) == 2
    assert facts[0].concept == "Revenues"
    assert facts[0].period_start == date(2024, 1, 1)
    assert facts[0].available_at is not None
    assert facts[0].quality == "date_only_estimated"


def test_sec_companyfacts_rejects_empty_payload():
    with pytest.raises(DataError):
        parse_companyfacts({}, "US:XNAS:X", "1")


def test_eastmoney_raw_klines_parse():
    payload = {"data": {"klines": ["2024-01-02,10.0,10.5,10.8,9.9,1000,10500.0,9.0,5.0,0.5,1.0"]}}
    bars = parse_klines(payload, "US:XNAS:X", "US")
    assert len(bars) == 1
    assert bars[0].close == 10.5
    assert bars[0].quality.startswith("raw_fqt0")
    assert secid_for("HK", "0700", "XHKG") == "116.0700"


def test_sina_jsonp_parsing_via_client():
    body = b'var _stock_research=([{"d":"2024-01-02","o":"10","h":"11","l":"9","c":"10.5","v":"1000"}]);'
    session = FakeSession([FakeResponse(200, body, "application/javascript")])
    client = SinaClient(Transport(session=session, retries=0, min_interval_seconds=0))
    bars, receipt = client.daily("US:XNAS:X", "US", "X")
    assert len(bars) == 1
    assert bars[0].close == 10.5
    assert receipt.record_count == 1


def test_finnhub_headlines_are_title_scope_only():
    events = parse_company_news([{"headline": "Company beats estimates", "url": "https://x", "datetime": 1717416000}], "US:XNAS:X")
    assert len(events) == 1
    assert events[0].evidence_scope is EvidenceScope.TITLE
    assert events[0].grade is Grade.INSUFFICIENT


def test_source_policy_blocks_hk_sec_and_allows_user_import():
    from stock_research.acquisition.sources import assert_source_allowed

    policies = load_policies()
    with pytest.raises(Exception):
        assert_source_allowed(policies, "sec", "HK", "financial_facts")
    assert_source_allowed(policies, "user_import", "HK", "prices")
