"""Time helpers with an explicit UTC convention.

Economic dates are local trading dates and remain plain ``date`` objects.
Knowledge times are timezone-aware UTC datetimes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    if "T" in text or " " in text:
        return parse_dt(text).date()
    return date.fromisoformat(text[:10])


def parse_dt(value: str | datetime | date) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=UTC)
    text = str(value).strip()
    if not text:
        raise ValueError("empty datetime")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        return datetime.combine(date.fromisoformat(text[:10]), time(0, 0), tzinfo=UTC)


def iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return value.isoformat()


def date_str(value: date | datetime | str) -> str:
    return parse_date(value).isoformat()


def session_close_utc(local_day: date, utc_offset_hours: float, close_hour: int = 16) -> datetime:
    """Approximate close time when no full exchange calendar is available."""
    close_local = datetime.combine(local_day, time(close_hour, 0))
    return ensure_utc(close_local - timedelta(hours=utc_offset_hours))


def days_between(start: date, end: date) -> int:
    return (end - start).days
