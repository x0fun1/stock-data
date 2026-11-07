"""Trading calendars.

US sessions are generated from NYSE holiday rules.  Hong Kong sessions are only
accepted from an explicitly configured holiday list or from observed benchmark
sessions; the module never assumes that every weekday is a HK trading day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from ..common.errors import CoverageError
from ..common.timeutil import ensure_utc, parse_date

MARKET_ZONE = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}
MARKET_CLOSE = {"US": time(16, 0), "HK": time(16, 0)}


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:  # no tzdata on a minimal Windows install
        from datetime import timezone

        return timezone(timedelta(hours=-5 if name.startswith("America") else 8))


def easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    last = first_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def us_holidays(year: int) -> set[date]:
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),     # Memorial Day
        _observed(date(year, 7, 4)),   # Independence Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)), # Christmas
    }
    # NYSE does not observe Saturday New Year's Day on the preceding Friday.
    if date(year, 1, 1).weekday() == 5:
        days.discard(date(year - 1, 12, 31))
    if year >= 1998:
        days.add(_nth_weekday(year, 1, 0, 3))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    special = {
        1994: [date(1994, 4, 27)], 2001: [date(2001, 9, d) for d in range(11, 15)],
        2004: [date(2004, 6, 11)], 2007: [date(2007, 1, 2)],
        2012: [date(2012, 10, 29), date(2012, 10, 30)],
        2018: [date(2018, 12, 5)], 2025: [date(2025, 1, 9)],
    }
    days.update(special.get(year, []))
    return days


def us_early_closes(year: int) -> dict[date, time]:
    """Modern regular NYSE early closes; explicit policy overrides remain possible."""
    if year < 1993:
        return {}
    candidates = [date(year, 7, 3), date(year, 12, 24),
                  _nth_weekday(year, 11, 3, 4) + timedelta(days=1)]
    holidays = us_holidays(year)
    return {d: time(13, 0) for d in candidates if d.weekday() < 5 and d not in holidays}


@dataclass
class TradingCalendar:
    market: str
    holidays: set[date]
    provenance: str
    early_closes: dict[date, time] = field(default_factory=dict)
    coverage_start: date | None = None
    coverage_end: date | None = None

    def __post_init__(self) -> None:
        self.market = str(self.market).upper()
        self.holidays = {parse_date(d) for d in self.holidays}
        if self.coverage_start is not None:
            self.coverage_start = parse_date(self.coverage_start)
        if self.coverage_end is not None:
            self.coverage_end = parse_date(self.coverage_end)

    def is_session(self, day: date | str) -> bool:
        day = parse_date(day)
        if self.coverage_start and day < self.coverage_start:
            return False
        if self.coverage_end and day > self.coverage_end:
            return False
        return day.weekday() < 5 and day not in self.holidays

    def sessions(self, start: date | str, end: date | str) -> list[date]:
        start, end = parse_date(start), parse_date(end)
        if end < start:
            return []
        out: list[date] = []
        cursor = start
        while cursor <= end:
            if self.is_session(cursor):
                out.append(cursor)
            cursor += timedelta(days=1)
        return out

    def next_session(self, day: date | str, include_self: bool = False) -> date:
        cursor = parse_date(day) + (timedelta(0) if include_self else timedelta(days=1))
        for _ in range(370):
            if self.is_session(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CoverageError(f"no {self.market} session found after {day}")

    def prev_session(self, day: date | str) -> date:
        cursor = parse_date(day) - timedelta(days=1)
        for _ in range(370):
            if self.is_session(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise CoverageError(f"no {self.market} session found before {day}")

    def session_offset(self, day: date | str, offset: int) -> date:
        cursor = parse_date(day)
        if not self.is_session(cursor):
            raise CoverageError(f"{cursor} is not a {self.market} session")
        step = 1 if offset >= 0 else -1
        remaining = abs(offset)
        for _ in range(4000):
            if remaining == 0:
                return cursor
            cursor += timedelta(days=step)
            if self.is_session(cursor):
                remaining -= 1
        raise CoverageError(f"session offset {offset} leaves the {self.market} calendar coverage")

    def session_close_utc(self, day: date | str) -> datetime:
        day = parse_date(day)
        zone = _zone(MARKET_ZONE[self.market])
        close = self.early_closes.get(day, MARKET_CLOSE[self.market])
        local = datetime.combine(day, close, tzinfo=zone)
        return ensure_utc(local)

    def session_open_utc(self, day: date | str) -> datetime:
        day = parse_date(day)
        zone = _zone(MARKET_ZONE[self.market])
        local = datetime.combine(day, time(9, 30), tzinfo=zone)
        return ensure_utc(local)

    def coverage_label(self) -> str:
        return f"{self.provenance}:{self.coverage_start}:{self.coverage_end}"


def build_us_calendar(start_year: int, end_year: int) -> TradingCalendar:
    holidays: set[date] = set()
    early: dict[date, time] = {}
    for year in range(start_year - 1, end_year + 2):
        holidays |= us_holidays(year)
        early.update(us_early_closes(year))
    return TradingCalendar(
        market="US",
        holidays=holidays,
        provenance="nyse_rules_v2_known_special_closures",
        early_closes=early,
        coverage_start=date(start_year, 1, 1),
        coverage_end=date(end_year, 12, 31),
    )


def build_observed_calendar(
    market: str,
    observed_dates: Iterable[date],
    provenance: str = "observed_benchmark_sessions",
) -> TradingCalendar:
    days = sorted({parse_date(d) for d in observed_dates})
    if not days:
        raise CoverageError(f"no observed sessions for {market}")
    holidays: set[date] = set()
    cursor = days[0]
    present = set(days)
    while cursor <= days[-1]:
        if cursor.weekday() < 5 and cursor not in present:
            holidays.add(cursor)
        cursor += timedelta(days=1)
    return TradingCalendar(
        market=market,
        holidays=holidays,
        provenance=provenance,
        coverage_start=days[0],
        coverage_end=days[-1],
    )


def build_calendar(
    market: str,
    config: Mapping | None = None,
    observed_dates: Iterable[date] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> TradingCalendar:
    market = str(market).upper()
    config = config or {}
    mode = str(config.get("mode", "rules" if market == "US" else "observed_benchmark"))
    if market == "US" and mode == "rules":
        if start_year is None or end_year is None:
            observed = list(observed_dates or [])
            if observed:
                start_year = min(parse_date(d) for d in observed).year - 1
                end_year = max(parse_date(d) for d in observed).year + 1
            else:
                start_year = end_year = date.today().year
        calendar = build_us_calendar(start_year, end_year)
        calendar.holidays.update(parse_date(d) for d in config.get("holidays", []))
        for day, close in (config.get("early_closes") or {}).items():
            calendar.early_closes[parse_date(day)] = time.fromisoformat(str(close))
        return calendar
    holidays = config.get("holidays") or []
    if mode in {"configured", "rules"} and holidays:
        return TradingCalendar(
            market=market,
            holidays={parse_date(d) for d in holidays},
            provenance=str(config.get("provenance", "configured_holidays")),
            coverage_start=config.get("coverage_start"),
            coverage_end=config.get("coverage_end"),
        )
    if observed_dates:
        return build_observed_calendar(
            market,
            observed_dates,
            provenance=str(config.get("provenance", "observed_benchmark_sessions")),
        )
    raise CoverageError(
        f"no validated {market} calendar: configure holidays or provide observed benchmark sessions"
    )
