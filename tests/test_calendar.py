from __future__ import annotations

from datetime import date

import pytest

from stock_research.identity_calendar import build_observed_calendar, build_us_calendar


def test_us_calendar_has_252_sessions_and_holidays():
    cal = build_us_calendar(2024, 2024)
    sessions = cal.sessions(date(2024, 1, 1), date(2024, 12, 31))
    assert len(sessions) == 252
    assert not cal.is_session(date(2024, 1, 1))
    assert not cal.is_session(date(2024, 7, 4))
    assert not cal.is_session(date(2024, 6, 19))  # Juneteenth
    assert not cal.is_session(date(2024, 3, 29))  # Good Friday
    assert cal.next_session(date(2024, 7, 3)) == date(2024, 7, 5)


def test_session_offsets_are_trading_sessions():
    cal = build_us_calendar(2024, 2024)
    assert cal.session_offset(date(2024, 7, 3), 1) == date(2024, 7, 5)
    assert cal.session_offset(date(2024, 7, 5), -1) == date(2024, 7, 3)
    with pytest.raises(Exception):
        cal.session_offset(date(2024, 7, 4), 1)


def test_hk_observed_calendar_marks_missing_weekdays_closed():
    cal = build_observed_calendar("HK", [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)])
    assert cal.is_session(date(2025, 1, 2))
    assert not cal.is_session(date(2025, 1, 1))
    assert not cal.is_session(date(2025, 1, 7))
    assert cal.provenance == "observed_benchmark_sessions"


def test_calendar_coverage_is_bounded():
    cal = build_us_calendar(2024, 2024)
    assert not cal.is_session(date(2023, 12, 29))
    assert not cal.is_session(date(2025, 1, 2))
