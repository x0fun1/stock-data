"""Date-grouped, label-aware partitions with purge and embargo.

Rows are never split randomly.  A training label must end before the next
partition begins, minus an embargo, and a test label must finish inside the test
window to be evaluable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from ..common.errors import DataError
from ..common.timeutil import parse_date


@dataclass(frozen=True)
class PartitionSpec:
    train_start: date
    train_end: date
    dev_start: date
    dev_end: date
    cal_start: date
    cal_end: date
    test_start: date
    test_end: date
    embargo_days: int = 1

    def __post_init__(self) -> None:
        for name in ("train_start", "train_end", "dev_start", "dev_end", "cal_start", "cal_end", "test_start", "test_end"):
            object.__setattr__(self, name, parse_date(getattr(self, name)))
        if not (self.train_start <= self.train_end < self.dev_start <= self.dev_end < self.cal_start <= self.cal_end < self.test_start <= self.test_end):
            raise ValueError("partition blocks must be ordered and non-overlapping")
        if self.embargo_days < 0:
            raise ValueError("embargo_days must be non-negative")

    @property
    def embargo(self) -> timedelta:
        return timedelta(days=self.embargo_days)


def assign_partition(cutoff: date | str, label_end: date | str | None, spec: PartitionSpec) -> str:
    cutoff = parse_date(cutoff)
    label_end = parse_date(label_end) if label_end is not None else cutoff
    if spec.train_start <= cutoff <= spec.train_end:
        return "train" if label_end < spec.dev_start - spec.embargo else "excluded"
    if spec.dev_start <= cutoff <= spec.dev_end:
        return "dev" if label_end < spec.cal_start - spec.embargo else "excluded"
    if spec.cal_start <= cutoff <= spec.cal_end:
        return "cal" if label_end < spec.test_start - spec.embargo else "excluded"
    if spec.test_start <= cutoff <= spec.test_end:
        return "test" if label_end <= spec.test_end else "excluded"
    return "excluded"


def annotate_partitions(frame: pd.DataFrame, spec: PartitionSpec, label_end_column: str = "end_date") -> pd.DataFrame:
    required = {"cutoff_date", label_end_column}
    missing = required - set(frame.columns)
    if missing:
        raise DataError(f"partition assignment requires columns {sorted(missing)}")
    out = frame.copy()
    out["partition"] = [
        assign_partition(row.cutoff_date, getattr(row, label_end_column), spec)
        for row in out.itertuples(index=False)
    ]
    return out


def validate_partitions(frame: pd.DataFrame, spec: PartitionSpec) -> dict:
    """Return counts and raise on a label boundary violation."""
    if "partition" not in frame.columns:
        raise DataError("frame has no partition column")
    counts = frame["partition"].value_counts().to_dict()
    problems = []
    checks = [
        ("train", "dev_start", spec.dev_start),
        ("dev", "cal_start", spec.cal_start),
        ("cal", "test_start", spec.test_start),
    ]
    end_col = "end_date"
    for partition, boundary_name, boundary in checks:
        subset = frame[frame["partition"] == partition]
        if subset.empty:
            continue
        ends = pd.to_datetime(subset[end_col]).dt.date
        limit = boundary - spec.embargo
        violations = subset[ends >= limit]
        if not violations.empty:
            problems.append(
                {
                    "partition": partition,
                    "boundary": boundary_name,
                    "violations": int(len(violations)),
                    "max_label_end": str(ends.max()),
                }
            )
    test = frame[frame["partition"] == "test"]
    if not test.empty:
        ends = pd.to_datetime(test[end_col]).dt.date
        late = test[ends > spec.test_end]
        if not late.empty:
            problems.append({"partition": "test", "boundary": "test_end", "violations": int(len(late)), "max_label_end": str(ends.max())})
    if problems:
        raise DataError(f"partition boundary violations: {problems}")
    return {"counts": counts, "spec": spec.__dict__, "validated": True}


def partition_summary(frame: pd.DataFrame) -> dict:
    if "partition" not in frame.columns:
        return {}
    summary = {}
    for name, group in frame.groupby("partition"):
        summary[name] = {
            "rows": int(len(group)),
            "securities": int(group["security_id"].nunique()) if "security_id" in group else None,
            "dates": int(pd.to_datetime(group["cutoff_date"]).dt.date.nunique()) if "cutoff_date" in group else None,
            "start": str(pd.to_datetime(group["cutoff_date"]).min().date()) if "cutoff_date" in group else None,
            "end": str(pd.to_datetime(group["cutoff_date"]).max().date()) if "cutoff_date" in group else None,
        }
    return summary


def walk_forward_specs(
    train_start: date | str,
    first_test_start: date | str,
    last_test_end: date | str,
    test_days: int,
    dev_days: int,
    cal_days: int,
    embargo_days: int = 1,
) -> list[PartitionSpec]:
    """Expanding-train walk-forward folds with fixed dev/cal/test lengths."""
    train_start = parse_date(train_start)
    cursor = parse_date(first_test_start)
    last = parse_date(last_test_end)
    specs = []
    day = timedelta(days=1)
    while cursor <= last:
        test_end = cursor + timedelta(days=max(test_days - 1, 0))
        if test_end > last:
            test_end = last
        cal_end = cursor - timedelta(days=embargo_days + 1)
        cal_start = cal_end - timedelta(days=max(cal_days - 1, 0))
        dev_end = cal_start - timedelta(days=embargo_days + 1)
        dev_start = dev_end - timedelta(days=max(dev_days - 1, 0))
        train_end = dev_start - timedelta(days=embargo_days + 1)
        if train_end <= train_start:
            raise ValueError("not enough history for the requested fold lengths")
        specs.append(
            PartitionSpec(
                train_start=train_start,
                train_end=train_end,
                dev_start=dev_start,
                dev_end=dev_end,
                cal_start=cal_start,
                cal_end=cal_end,
                test_start=cursor,
                test_end=test_end,
                embargo_days=embargo_days,
            )
        )
        cursor = test_end + day
    return specs
