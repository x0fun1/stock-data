"""Timezone-aware point-in-time boundaries, without invented publication times."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("time must be a timezone-aware ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("time requires an explicit timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now() -> str:
    return iso(datetime.now(timezone.utc))


def bounds(value: str, *, precision: str = "timestamp", timezone_name: str | None = "UTC") -> tuple[datetime, datetime]:
    if precision == "date" or len(value) == 10:
        from zoneinfo import ZoneInfo

        date = datetime.strptime(value[:10], "%Y-%m-%d").date()
        if timezone_name is None:
            # Unknown source zone: bound every civil timezone rather than inventing a publication instant.
            earliest = datetime.combine(date, time.min, timezone(timedelta(hours=14)))
            latest = datetime.combine(date + timedelta(days=1), time.min, timezone(timedelta(hours=-12)))
            return earliest.astimezone(timezone.utc), latest.astimezone(timezone.utc)
        start = datetime.combine(date, time.min, ZoneInfo(timezone_name))
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
    instant = parse_time(value)
    return instant, instant


def known_by(value: str | None, as_of: str, *, precision: str = "timestamp", timezone_name: str = "UTC") -> bool:
    return bool(value) and bounds(value, precision=precision, timezone_name=timezone_name)[1] <= parse_time(as_of)
