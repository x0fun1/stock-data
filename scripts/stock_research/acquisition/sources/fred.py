"""FRED/ALFRED macro adapter.

FRED observation dates are statistical periods, not release times.  The adapter
therefore sets ``available_at`` only when an ALFRED realtime query provides a
usable vintage boundary; otherwise the series is context/forward-only.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
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
    use_realtime_boundaries: bool = False,
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
        row_available, row_vintage = available_at, vintage_id
        quality = "availability_unknown" if available_at is None else "availability_provided"
        if use_realtime_boundaries:
            if not row.get('realtime_start') or not row.get('realtime_end'):
                raise DataError('ALFRED revision history requires row realtime boundaries')
            released = parse_date(row['realtime_start'])
            # ALFRED supplies a vintage DATE, not an intraday release instant.
            # Waiting through that date in New York avoids using a release
            # before its actual publication time; never call this exact-time.
            row_available = datetime.combine(released, time(23, 59, 59), tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
            row_vintage = f"{row['realtime_start']}:{row['realtime_end']}"
            quality = 'alfred_vintage_date_conservative'
        out.append(
            MacroObservation(
                series_id=series_id,
                observation_date=parse_date(row["date"]),
                value=numeric,
                unit="",
                market=market,
                available_at=row_available,
                vintage_id=row_vintage,
                source_id="fred",
                quality=quality,
            )
        )
    return out


class FredClient:
    def __init__(self, transport, api_key: str) -> None:
        if not (api_key or "").strip():
            raise ValueError("FRED requests require an API key")
        self.transport = transport
        self.api_key = api_key.strip()

    def observations(self, series_id: str, realtime_start: str | None = None, realtime_end: str | None = None,
                     observation_start: str = "1900-01-01", observation_end: str | None = None,
                     offset: int = 0, limit: int = 100000) -> tuple[list[MacroObservation], AcquisitionReceipt]:
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": observation_start, "offset": offset, "limit": limit,
            "output_type": 1,
        }
        if observation_end:
            params['observation_end'] = observation_end
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
        revision_history = bool(realtime_start and realtime_end and realtime_start < realtime_end)
        observations = parse_observations(result.data, series_id, available_at=None,
            vintage_id=vintage, use_realtime_boundaries=revision_history)
        count = int(result.data.get('count', len(result.data.get('observations', []))))
        receipt.exhausted = offset + len(result.data.get('observations', [])) >= count
        receipt.cap_reached = not receipt.exhausted
        receipt.record_count = len(observations)
        if observations:
            receipt.coverage_start = min(o.observation_date for o in observations)
            receipt.coverage_end = max(o.observation_date for o in observations)
        return observations, receipt
