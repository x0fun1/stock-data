"""Finnhub headline/context adapter. Headlines are never upgraded to full-text evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...common.errors import DataError
from ...common.hashing import sha256_bytes
from ...common.timeutil import now_utc
from ...contracts import AcquisitionReceipt, ContextEvent, EvidenceScope, SourceKind

BASE = "https://finnhub.io/api/v1"
UTC = timezone.utc


def parse_company_news(
    data: Any,
    security_id: str,
    market: str = "US",
    source_id: str = "finnhub",
) -> list[ContextEvent]:
    if not isinstance(data, list):
        raise DataError("Finnhub company-news payload is not a list")
    out = []
    for row in data:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        epoch = row.get("datetime")
        published = datetime.fromtimestamp(float(epoch), tz=UTC) if epoch is not None else None
        out.append(
            ContextEvent(
                event_id="",
                market=market,
                security_ids=[security_id],
                title=str(row.get("headline", "")),
                published_at=published,
                available_at=published,
                received_at=now_utc(),
                source_id=source_id,
                source_kind=SourceKind.VENDOR,
                evidence_scope=EvidenceScope.TITLE,
                url=str(row.get("url", "")),
                body="",
                evidence_locator="headline",
                permissions={"store_full_text": False, "derived_results_restricted": True},
            )
        )
    return out


class FinnhubClient:
    def __init__(self, transport, api_key: str) -> None:
        if not (api_key or "").strip():
            raise ValueError("Finnhub requests require an API key")
        self.transport = transport
        self.api_key = api_key.strip()

    def company_news(self, security_id: str, symbol: str, start: str, end: str) -> tuple[list[ContextEvent], AcquisitionReceipt]:
        params = {"symbol": symbol.upper(), "from": start, "to": end, "token": self.api_key}
        result = self.transport.get(f"{BASE}/company-news", params=params, expect="json")
        receipt = AcquisitionReceipt(
            source_id="finnhub",
            dataset="news",
            subrequest="company-news",
            requested_at=result.fetched_at,
            received_at=result.fetched_at,
            status=result.status,
            http_status=result.http_status,
            content_type=result.content_type,
            byte_count=len(result.content),
            sha256=sha256_bytes(result.content),
            error=result.error,
            exhausted=result.ok,
            params_redacted=result.params_redacted,
        )
        if not result.ok:
            raise DataError(f"Finnhub request failed: {result.error}")
        events = parse_company_news(result.data, security_id)
        receipt.record_count = len(events)
        return events, receipt
