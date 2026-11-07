from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from conftest import bars_from_rows, price_rows, make_days
from stock_research.contracts import ActionType, CorporateAction, MissingReason, PriceBar
from stock_research.corporate_actions import ActionLedger, PriceSeries
from stock_research.features.price import compute_price_features


def test_features_are_prefix_invariant():
    days = make_days(count=300)
    rows = price_rows("US:XNAS:X", days, seed=11)
    full = PriceSeries(bars_from_rows(rows, "US:XNAS:X"))
    cutoff = days[250]
    prefix = PriceSeries(full.slice(cutoff).bars)
    a = compute_price_features(prefix, ActionLedger(), cutoff, market="US")
    b = compute_price_features(full, ActionLedger(), cutoff, market="US")
    for name in a:
        assert a[name].status == b[name].status, name
        if a[name].value is None:
            assert b[name].value is None
        else:
            assert a[name].value == pytest.approx(b[name].value, rel=1e-12, abs=1e-12), name


def test_explicitly_late_quote_cannot_change_old_factors_or_returns():
    from stock_research.corporate_actions.returns import compute_window_return
    days = make_days(count=280)
    bars = bars_from_rows(price_rows("US:XNAS:X", days, seed=51), "US:XNAS:X")
    known = datetime.combine(days[-1], datetime.max.time(), tzinfo=timezone.utc)
    bars[-1].available_at = known + timedelta(days=1)
    bars[-1].open = bars[-1].close = bars[-1].high = bars[-1].low = 100000.0
    bars[-1].volume = bars[-1].amount = 1e12
    expected = compute_price_features(PriceSeries(bars[:-1]), ActionLedger(), days[-1], known_by=known)
    actual = compute_price_features(PriceSeries(bars), ActionLedger(), days[-1], known_by=known)
    for name in expected:
        assert actual[name].status == expected[name].status, name
        assert actual[name].value == expected[name].value, name
    result = compute_window_return(PriceSeries(bars), ActionLedger(), days[-2], days[-1], known_by=known)
    assert result.status == "missing" and result.missing_reason == MissingReason.NO_CLOSE.value


def test_split_adjustment_prevents_fake_momentum_crash():
    days = make_days(count=90)
    rows = []
    for i, day in enumerate(days):
        price = 100.0 if i < 70 else 50.0
        rows.append({"date": day.isoformat(), "open": price, "high": price, "low": price, "close": price, "volume": 100.0})
    bars = bars_from_rows(rows, "US:XNAS:X")
    ledger = ActionLedger([CorporateAction("US:XNAS:X", days[70], ActionType.SPLIT, q=2)])
    features = compute_price_features(PriceSeries(bars), ledger, days[-1], market="US")
    assert features["mom_60"].status == "ok"
    assert abs(features["mom_60"].value) < 0.01


def test_insufficient_window_returns_explicit_missing_reason():
    days = make_days(count=30)
    bars = bars_from_rows(price_rows("US:XNAS:X", days, seed=2), "US:XNAS:X")
    features = compute_price_features(PriceSeries(bars), ActionLedger(), days[-1], market="US")
    assert features["mom_60"].status == "missing"
    assert features["mom_60"].reason == MissingReason.WINDOW_INSUFFICIENT.value
    assert features["rv_20"].status == "ok"


def test_unsupported_action_blocks_affected_factors_only():
    days = make_days(count=90)
    bars = bars_from_rows(price_rows("US:XNAS:X", days, seed=5), "US:XNAS:X")
    ledger = ActionLedger([CorporateAction("US:XNAS:X", days[80], ActionType.RIGHTS)])
    features = compute_price_features(PriceSeries(bars), ledger, days[-1], market="US")
    assert features["mom_60"].status == "missing"
    assert features["mom_60"].reason == MissingReason.UNSUPPORTED_ACTION.value
    # The event corrupts the wealth-index path, so any later J/r window is blocked.
    assert features["rv_20"].status == "missing"
    assert features["rv_20"].reason == MissingReason.UNSUPPORTED_ACTION.value


def test_all_values_are_finite_or_missing():
    days = make_days(count=120)
    bars = bars_from_rows(price_rows("US:XNAS:X", days, seed=8), "US:XNAS:X")
    features = compute_price_features(PriceSeries(bars), ActionLedger(), days[-1], market="US")
    for name, obs in features.items():
        if obs.value is not None:
            assert np.isfinite(obs.value), name
