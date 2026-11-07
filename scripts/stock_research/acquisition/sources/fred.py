"""FRED/ALFRED macro adapter.

FRED observation dates are statistical periods, not release times.  The adapter
therefore sets ``available_at`` only when an ALFRED realtime query provides a
usable vintage boundary; otherwise the series is context/forward-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...common.errors import DataError
from ...common.hashing import sha256_bytes
from ...common.timeutil import parse_date
from ...contracts import AcquisitionReceipt, MacroObservation

BASE = "https://api.stlouisfed.org/fred"


def parse_observations(
    data: dict,
    series_id: str,
    available_at: datetime | None = None,
    vintage_id: str = "",
    market: str = "US",
) -> list[MacroObservation]:
    observations = data.get("observations")
    if not isinstance(observations, list):
        raise DataError("FRED payload has no observations list")
    out = []
    for row in observations:
        value = row.get("value")
        if value in (None, ".", ""):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        out.append(
            MacroObservation(
                series_id=series_id,
                observation_date=parse_date(row["date"]),
                value=numeric,
                unit="",
                market=market,
                available_at=available_at,
                vintage_id=vintage_id,
                source_id="fred",
                quality="availability_unknown" if available_at is None else "availability_provided",
            )
        )
    return out


class FredClient:
    def __init__(self, transport, api_key: str) -> None:
        if not (api_key or "").strip():
            raise ValueError("FRED requests require an API key")
        self.transport = transport
        self.api_key = api_key.strip()

    def observations(self, series_id: str, realtime_start: str | None = None, realtime_end: str | None = None) -> tuple[list[MacroObservation], AcquisitionReceipt]:
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": "1900-01-01",
        }
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end
        result = self.transport.get(f"{BASE}/series/observations", params=params, expect="json")
        receipt = AcquisitionReceipt(
            source_id="fred",
            dataset="macro",
            subrequest=f"observations:{series_id}",
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
            raise DataError(f"FRED request failed: {result.error}")
        vintage = f"{realtime_start}:{realtime_end}" if realtime_start or realtime_end else ""
        observations = parse_observations(result.data, series_id, available_at=None, vintage_id=vintage)
        receipt.record_count = len(observations)
        if observations:
            receipt.coverage_start = min(o.observation_date for o in observations)
            receipt.coverage_end = max(o.observation_date for o in observations)
        return observations, receipt
