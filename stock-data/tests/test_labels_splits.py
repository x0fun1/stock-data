from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_research.contracts import PriceBar, Target
from stock_research.corporate_actions import ActionLedger, PriceSeries
from stock_research.datasets import LabelConfig, PartitionSpec, annotate_partitions, build_label, validate_partitions
from stock_research.identity_calendar import build_us_calendar

CAL = build_us_calendar(2024, 2024)


def bar(day, close, open_=None):
    return PriceBar("US:XNAS:X", day, open_ if open_ is not None else close, close, close, close, 1000.0)


def series():
    return PriceSeries(
        [
            bar(date(2024, 6, 3), 100),
            bar(date(2024, 6, 4), 100, open_=100),
            bar(date(2024, 6, 5), 105, open_=101),
        ]
    )


def test_label_uses_next_open_to_horizon_close():
    label = build_label("US:XNAS:X", "US", series(), ActionLedger(), None, None, CAL, date(2024, 6, 3), 2, Target.ABSOLUTE)
    assert label.status == "ok"
    assert label.entry_date == date(2024, 6, 4)
    assert label.end_date == date(2024, 6, 5)
    assert label.return_value == pytest.approx(0.05)
    assert label.label == 1


def test_strict_positive_label_is_zero_for_flat_return():
    prices = PriceSeries([bar(date(2024, 6, 3), 100), bar(date(2024, 6, 4), 100), bar(date(2024, 6, 5), 100)])
    label = build_label("US:XNAS:X", "US", prices, ActionLedger(), None, None, CAL, date(2024, 6, 3), 2, Target.ABSOLUTE)
    assert label.return_value == 0
    assert label.label == 0


def test_excess_label_requires_matching_benchmark():
    benchmark = PriceSeries([bar(date(2024, 6, 3), 100), PriceBar("US:XNAS:SPY", date(2024, 6, 4), 100, 100, 100, 100, 1000), PriceBar("US:XNAS:SPY", date(2024, 6, 5), 110, 110, 110, 110, 1000)])
    label = build_label("US:XNAS:X", "US", series(), ActionLedger(), benchmark, None, CAL, date(2024, 6, 3), 2, Target.EXCESS)
    assert label.excess_return == pytest.approx(0.05 - 0.10)
    assert label.label_excess == 0
    missing = build_label("US:XNAS:X", "US", series(), ActionLedger(), None, None, CAL, date(2024, 6, 3), 2, Target.EXCESS)
    assert missing.status == "missing"
    assert missing.missing_reason == "no_data"


def test_maturity_is_after_end_close_plus_delay():
    label = build_label("US:XNAS:X", "US", series(), ActionLedger(), None, None, CAL, date(2024, 6, 3), 2, Target.ABSOLUTE, LabelConfig(maturity_delay_hours=2))
    assert label.matured_at.hour == 22  # 16:00 New York + 2h = 22:00 UTC on 2024-06-05


def test_partition_purge_excludes_label_overlapping_next_block():
    spec = PartitionSpec(
        train_start=date(2024, 1, 2),
        train_end=date(2024, 3, 31),
        dev_start=date(2024, 4, 1),
        dev_end=date(2024, 4, 30),
        cal_start=date(2024, 5, 1),
        cal_end=date(2024, 5, 31),
        test_start=date(2024, 6, 1),
        test_end=date(2024, 12, 31),
        embargo_days=1,
    )
    frame = pd.DataFrame(
        [
            {"cutoff_date": "2024-03-28", "end_date": "2024-04-05", "security_id": "X"},
            {"cutoff_date": "2024-03-20", "end_date": "2024-03-27", "security_id": "X"},
            {"cutoff_date": "2024-04-10", "end_date": "2024-04-17", "security_id": "X"},
            {"cutoff_date": "2024-06-03", "end_date": "2024-12-31", "security_id": "X"},
        ]
    )
    annotated = annotate_partitions(frame, spec)
    assert annotated.loc[0, "partition"] == "excluded"  # train label crosses dev start
    assert annotated.loc[1, "partition"] == "train"
    assert annotated.loc[2, "partition"] == "dev"
    assert annotated.loc[3, "partition"] == "test"
    assert validate_partitions(annotated, spec)["validated"] is True


def test_partition_rejects_test_label_beyond_test_end():
    spec = PartitionSpec(
        train_start=date(2024, 1, 2),
        train_end=date(2024, 3, 31),
        dev_start=date(2024, 4, 1),
        dev_end=date(2024, 4, 30),
        cal_start=date(2024, 5, 1),
        cal_end=date(2024, 5, 31),
        test_start=date(2024, 6, 1),
        test_end=date(2024, 6, 28),
        embargo_days=1,
    )
    frame = pd.DataFrame([{"cutoff_date": "2024-06-27", "end_date": "2024-07-05", "security_id": "X"}])
    assert annotate_partitions(frame, spec).loc[0, "partition"] == "excluded"
