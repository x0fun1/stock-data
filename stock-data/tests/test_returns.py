from __future__ import annotations

from datetime import date

import pytest

from stock_research.contracts import ActionType, CorporateAction, PriceBar
from stock_research.corporate_actions import (
    PriceSeries,
    adjusted_history,
    build_total_return_series,
    compute_path_risk,
    compute_window_return,
)
from stock_research.corporate_actions.ledger import ActionLedger as LedgerClass
from stock_research.identity_calendar import build_us_calendar


def bar(day, close, open_=None, volume=100.0, amount=None):
    return PriceBar("US:XNAS:X", day, open_ if open_ is not None else close, close, close, close, volume, amount)


def test_split_does_not_create_fake_loss_or_gain():
    bars = [
        bar(date(2024, 6, 3), 100),
        bar(date(2024, 6, 4), 100),
        bar(date(2024, 6, 5), 50),
        bar(date(2024, 6, 6), 52),
    ]
    ledger = LedgerClass([CorporateAction("US:XNAS:X", date(2024, 6, 5), ActionType.SPLIT, q=2)])
    series = PriceSeries(bars)
    ts = build_total_return_series(series, ledger, date(2024, 6, 6))
    assert ts.index == pytest.approx([1.0, 1.0, 1.0, 1.04])
    cal = build_us_calendar(2024, 2024)
    window = compute_window_return(series, ledger, date(2024, 6, 4), date(2024, 6, 6), cal)
    assert window.status == "ok"
    assert window.return_value == pytest.approx(0.04)
    assert window.actions_applied == ["split@2024-06-05"]


def test_entry_on_ex_date_does_not_receive_split_or_dividend():
    bars = [
        bar(date(2024, 6, 3), 100),
        bar(date(2024, 6, 4), 100),
        bar(date(2024, 6, 5), 50),
        bar(date(2024, 6, 6), 52),
    ]
    ledger = LedgerClass(
        [
            CorporateAction("US:XNAS:X", date(2024, 6, 5), ActionType.SPLIT, q=2),
            CorporateAction("US:XNAS:X", date(2024, 6, 5), ActionType.CASH_DIVIDEND, cash_old=1.0),
        ]
    )
    series = PriceSeries(bars)
    window = compute_window_return(series, ledger, date(2024, 6, 5), date(2024, 6, 6), build_us_calendar(2024, 2024))
    assert window.return_value == pytest.approx(52 / 50 - 1)
    assert window.actions_applied == []


def test_cash_dividend_reinvestment_gives_zero_when_price_falls_by_dividend():
    bars = [
        bar(date(2024, 6, 3), 100),
        bar(date(2024, 6, 4), 100),
        bar(date(2024, 6, 5), 99),
        bar(date(2024, 6, 6), 99),
    ]
    ledger = LedgerClass([CorporateAction("US:XNAS:X", date(2024, 6, 5), ActionType.CASH_DIVIDEND, cash_old=1.0)])
    window = compute_window_return(PriceSeries(bars), ledger, date(2024, 6, 4), date(2024, 6, 6), build_us_calendar(2024, 2024))
    assert window.return_value == pytest.approx(0.0)
    assert window.actions_applied == ["cash_dividend@2024-06-05"]


def test_missing_entry_open_is_missing_not_zero():
    bars = [bar(date(2024, 6, 3), 100), PriceBar("US:XNAS:X", date(2024, 6, 4), None, 100, 100, 100, 100)]
    window = compute_window_return(PriceSeries(bars), LedgerClass(), date(2024, 6, 4), date(2024, 6, 4), build_us_calendar(2024, 2024))
    assert window.status == "missing"
    assert window.missing_reason == "no_open"


def test_missing_session_makes_label_incomplete():
    bars = [bar(date(2024, 6, 3), 100), bar(date(2024, 6, 4), 100), bar(date(2024, 6, 6), 100)]
    window = compute_window_return(PriceSeries(bars), LedgerClass(), date(2024, 6, 4), date(2024, 6, 6), build_us_calendar(2024, 2024))
    assert window.status == "missing"
    assert window.missing_reason == "incomplete_path"


def test_adjusted_history_uses_split_factor():
    bars = [
        bar(date(2024, 6, 3), 100, volume=100),
        bar(date(2024, 6, 4), 100, volume=100),
        bar(date(2024, 6, 5), 50, volume=200),
    ]
    ledger = LedgerClass([CorporateAction("US:XNAS:X", date(2024, 6, 5), ActionType.SPLIT, q=2)])
    adjusted = adjusted_history(PriceSeries(bars), ledger, date(2024, 6, 5))
    assert adjusted[0]["close"] == pytest.approx(50)
    assert adjusted[0]["volume"] == pytest.approx(200)
    assert adjusted[2]["close"] == pytest.approx(50)
    assert adjusted[2]["volume"] == pytest.approx(200)


def test_path_risk_reports_worst_floating_loss_and_drawdown():
    bars = [
        bar(date(2024, 6, 3), 100),
        bar(date(2024, 6, 4), 95),
        bar(date(2024, 6, 5), 90),
        bar(date(2024, 6, 6), 92),
    ]
    risk = compute_path_risk(
        PriceSeries(bars),
        LedgerClass(),
        date(2024, 6, 3),
        date(2024, 6, 6),
        build_us_calendar(2024, 2024),
        thresholds=(-0.05, -0.10),
    )
    assert risk["status"] == "ok"
    assert risk["worst_floating_return"] == pytest.approx(-0.10)
    assert risk["max_drawdown_close_path"] == pytest.approx(-0.10)
    assert risk["first_touch"]["-0.0500"] == "2024-06-04"
    assert risk["first_touch"]["-0.1000"] == "2024-06-05"


def test_unsupported_action_blocks_return():
    bars = [bar(date(2024, 6, 3), 100), bar(date(2024, 6, 4), 100), bar(date(2024, 6, 5), 90)]
    ledger = LedgerClass([CorporateAction("US:XNAS:X", date(2024, 6, 4), ActionType.RIGHTS, q=1.0)])
    window = compute_window_return(PriceSeries(bars), ledger, date(2024, 6, 3), date(2024, 6, 5), build_us_calendar(2024, 2024))
    assert window.status == "missing"
    assert window.missing_reason == "unsupported_action"
