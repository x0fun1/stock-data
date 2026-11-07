"""Eastmoney raw daily kline adapter (fqt=0 only)."""

from __future__ import annotations

from datetime import date

from ...common.errors import DataError
from ...common.hashing import sha256_bytes
from ...common.timeutil import parse_date
from ...contracts import AcquisitionReceipt, Market, PriceBar

BASE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EXCHANGE_PREFIX = {"XNAS": "105", "XNYS": "106", "XASE": "107", "XHKG": "116", "HKEX": "116"}


def secid_for(market: Market | str, ticker: str, exchange: str = "") -> str:
    market = Market(market)
    prefix = EXCHANGE_PREFIX.get(exchange.upper(), "105" if market is Market.US else "116")
    return f"{prefix}.{ticker.upper()}"


def parse_klines(
    data: dict,
    security_id: str,
    market: Market | str,
    currency: str = "",
    source_id: str = "eastmoney",
) -> list[PriceBar]:
    market = Market(market)
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise DataError("Eastmoney payload has no data object")
    klines = payload.get("klines") or []
    if not isinstance(klines, list):
        raise DataError("Eastmoney klines is not a list")
    bars = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        try:
            day = parse_date(parts[0])
            open_ = float(parts[1])
            close = float(parts[2])
            high = float(parts[3])
            low = float(parts[4])
            volume = float(parts[5])
            amount = float(parts[6]) if parts[6] not in {"", "-"} else None
        except (ValueError, TypeError):
            continue
        bars.append(
            PriceBar(
                security_id=security_id,
                date=day,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                amount=amount,
                currency=currency or ("USD" if market is Market.US else "HKD"),
                source_id=source_id,
                quality="raw_fqt0_vendor_history_unverified",
            )
        )
    return bars


class EastmoneyClient:
    def __init__(self, transport) -> None:
        self.transport = transport

    def daily(
        self,
        security_id: str,
        market: Market | str,
        ticker: str,
        exchange: str = "",
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> tuple[list[PriceBar], AcquisitionReceipt]:
        market = Market(market)
        params = {
            "secid": secid_for(market, ticker, exchange),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": parse_date(start).strftime("%Y%m%d") if start else "19900101",
            "end": parse_date(end).strftime("%Y%m%d") if end else "20500101",
            "lmt": "1000000",
        }
        result = self.transport.get(BASE, params=params, expect="json")
        receipt = AcquisitionReceipt(
            source_id="eastmoney",
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
            raise DataError(f"Eastmoney request failed: {result.error}")
        bars = parse_klines(result.data, security_id, market)
        receipt.record_count = len(bars)
        if bars:
            receipt.coverage_start = min(b.date for b in bars)
            receipt.coverage_end = max(b.date for b in bars)
        return bars, receipt
