"""Sina US/HK raw daily kline adapter (JSONP, vendor history unverified)."""

from __future__ import annotations

import json
from typing import Any

from ...common.errors import DataError
from ...common.hashing import sha256_bytes
from ...common.timeutil import parse_date
from ...contracts import AcquisitionReceipt, Market, PriceBar

US_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_stock_research=/US_MinKService.getDailyK"
HK_URL = "https://quotes.sina.cn/hk/api/jsonp_v2.php/var%20_stock_research=/HK_MinKService.getDailyK"


def _jsonp_payload(text: str) -> Any:
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end <= start:
        raise DataError("Sina response is not JSONP")
    try:
        return json.loads(text[start + 1:end])
    except json.JSONDecodeError as exc:
        raise DataError(f"Sina JSONP is invalid: {exc}") from exc


def parse_sina_rows(
    data: Any,
    security_id: str,
    market: Market | str,
    currency: str = "",
    source_id: str = "sina",
) -> list[PriceBar]:
    market = Market(market)
    if isinstance(data, dict):
        data = data.get("data") or data.get("result") or data.get("klines") or []
    if not isinstance(data, list):
        raise DataError("Sina payload is not a row list")
    bars = []
    for row in data:
        if not isinstance(row, dict):
            continue
        day_value = row.get("d") or row.get("day") or row.get("date")
        close_value = row.get("c") or row.get("close")
        if day_value is None or close_value is None:
            continue
        try:
            day = parse_date(str(day_value))
            close = float(close_value)
            open_ = float(row.get("o") or row.get("open") or close)
            high = float(row.get("h") or row.get("high") or close)
            low = float(row.get("l") or row.get("low") or close)
            volume = float(row.get("v") or row.get("volume") or 0.0)
            amount = float(row["amount"]) if row.get("amount") not in (None, "") else None
        except (ValueError, TypeError):
            continue
        bars.append(
            PriceBar(
                security_id,
                day,
                open_,
                high,
                low,
                close,
                volume,
                amount,
                currency or ("USD" if market is Market.US else "HKD"),
                source_id,
                quality="vendor_history_unverified",
            )
        )
    return bars


class SinaClient:
    def __init__(self, transport) -> None:
        self.transport = transport

    def daily(self, security_id: str, market: Market | str, ticker: str) -> tuple[list[PriceBar], AcquisitionReceipt]:
        market = Market(market)
        url = US_URL if market is Market.US else HK_URL
        result = self.transport.get(url, params={"symbol": ticker.upper()}, expect="text")
        receipt = AcquisitionReceipt(
            source_id="sina",
            dataset="prices",
            subrequest="daily",
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
            raise DataError(f"Sina request failed: {result.error}")
        bars = parse_sina_rows(_jsonp_payload(result.data), security_id, market)
        receipt.record_count = len(bars)
        if bars:
            receipt.coverage_start = min(b.date for b in bars)
            receipt.coverage_end = max(b.date for b in bars)
        return bars, receipt
