"""Closed-interval purging, embargo, and causal walk-forward folds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleInterval:
    decision_session: int
    information_start: int
    label_end: int


@dataclass(frozen=True)
class Fold:
    train: tuple[int, ...]
    test: tuple[int, ...]
    purged: int
    embargoed: int


def intervals_for(decisions: list[int], lookback: int, horizon: int) -> list[SampleInterval]:
    if lookback < 0 or horizon < 1:
        raise ValueError("lookback must be nonnegative and horizon positive")
    return [SampleInterval(i, max(0, i - lookback), i + horizon) for i in decisions]


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    # Closed intervals: touching an endpoint is an overlap and must be purged.
    return a_start <= b_end and b_start <= a_end


def purged_kfold(
    intervals: list[SampleInterval],
    *,
    n_splits: int = 5,
    embargo_sessions: int = 0,
) -> list[Fold]:
    """Generate contiguous test blocks and purge any intersecting train interval."""
    n = len(intervals)
    if n_splits < 2 or n < n_splits:
        return []
    if embargo_sessions < 0:
        raise ValueError("embargo_sessions must be nonnegative")
    boundaries = [round(i * n / n_splits) for i in range(n_splits + 1)]
    folds: list[Fold] = []
    for fold_number in range(n_splits):
        test = tuple(range(boundaries[fold_number], boundaries[fold_number + 1]))
        if not test:
            continue
        test_start = min(intervals[i].information_start for i in test)
        test_end = max(intervals[i].label_end for i in test)
        test_decision_end = max(intervals[i].decision_session for i in test)
        candidate = [i for i in range(n) if i not in test]
        keep: list[int] = []
        purged = 0
        embargoed = 0
        for i in candidate:
            interval = intervals[i]
            if _overlap(interval.information_start, interval.label_end, test_start, test_end):
                purged += 1
                continue
            if test_decision_end < interval.decision_session <= test_decision_end + embargo_sessions:
                embargoed += 1
                continue
            keep.append(i)
        folds.append(Fold(tuple(keep), test, purged, embargoed))
    return folds


def causal_walk_forward(
    intervals: list[SampleInterval],
    *,
    n_splits: int = 5,
    minimum_train: int = 100,
    pre_test_gap_sessions: int = 0,
) -> list[Fold]:
    """Expanding-window validation. Every train label ends before test starts."""
    if n_splits < 1 or pre_test_gap_sessions < 0:
        raise ValueError("invalid walk-forward options")
    n = len(intervals)
    first_test = max(minimum_train + pre_test_gap_sessions, n // 2)
    if first_test >= n:
        return []
    block = max(1, (n - first_test) // n_splits)
    folds: list[Fold] = []
    start = first_test
    while start < n:
        stop = min(n, start + block)
        test = tuple(range(start, stop))
        first_decision = min(intervals[i].decision_session for i in test)
        test_information_start = min(intervals[i].information_start for i in test)
        train: list[int] = []
        purged = 0
        embargoed = 0
        for i in range(start):
            interval = intervals[i]
            # A fixed pre-test gap is separate from interval-aware purging.
            if first_decision - pre_test_gap_sessions <= interval.decision_session < first_decision:
                embargoed += 1
                continue
            if interval.label_end >= test_information_start:
                purged += 1
                continue
            train.append(i)
        if len(train) >= minimum_train:
            folds.append(Fold(tuple(train), test, purged, embargoed))
        start = stop
    return folds


def count_interval_overlaps(intervals: list[SampleInterval], train: tuple[int, ...], test: tuple[int, ...]) -> int:
    count = 0
    for i in train:
        a = intervals[i]
        if any(_overlap(a.information_start, a.label_end, intervals[j].information_start, intervals[j].label_end) for j in test):
            count += 1
    return count
