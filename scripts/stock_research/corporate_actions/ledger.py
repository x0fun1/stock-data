"""Corporate-action ledger.

The ledger is the only source of split and cash-entitlement facts.  A missing
ledger entry is treated as unknown, never as proof that no action occurred.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from ..common.errors import DataError
from ..common.timeutil import parse_date, parse_dt
from ..contracts import ActionCoverage, ActionType, CorporateAction


@dataclass(slots=True)
class ActionEffect:
    q: float = 1.0
    cash_old: float = 0.0
    unsupported: bool = False
    labels: list[str] = field(default_factory=list)
    actions: list[CorporateAction] = field(default_factory=list)

    @property
    def has_effect(self) -> bool:
        return self.q != 1.0 or self.cash_old != 0.0 or bool(self.actions)

    def label(self) -> str:
        if not self.actions:
            return ""
        parts = []
        for action in self.actions:
            parts.append(f"{action.action_type.value}@{action.ex_date.isoformat()}")
        return ";".join(parts)


class ActionLedger:
    def __init__(
        self,
        actions: Iterable[CorporateAction] = (),
        coverages: Iterable[ActionCoverage] = (),
    ) -> None:
        self._by_security: dict[str, list[CorporateAction]] = defaultdict(list)
        self._coverages: dict[str, list[ActionCoverage]] = defaultdict(list)
        for action in actions:
            self._by_security[action.security_id].append(action)
        for security_id, items in self._by_security.items():
            items.sort(key=lambda a: (a.ex_date, a.action_type.value))
        for coverage in coverages:
            self._coverages[coverage.security_id].append(coverage)
        for items in self._coverages.values():
            items.sort(key=lambda c: c.start)

    def actions(
        self,
        security_id: str,
        start: date | str | None = None,
        end: date | str | None = None,
        known_by: str | datetime | date | None = None,
    ) -> list[CorporateAction]:
        start = parse_date(start) if start is not None else None
        end = parse_date(end) if end is not None else None
        known_by = parse_dt(known_by) if known_by is not None else None
        out = []
        for action in self._by_security.get(security_id, []):
            if start is not None and action.ex_date < start:
                continue
            if end is not None and action.ex_date > end:
                continue
            if known_by is not None and action.available_at is not None and action.available_at > known_by:
                continue
            out.append(action)
        return out

    def aggregate(self, security_id: str, ex_date: date | str) -> ActionEffect:
        ex_date = parse_date(ex_date)
        effect = ActionEffect()
        for action in self._by_security.get(security_id, []):
            if action.ex_date != ex_date:
                continue
            effect.actions.append(action)
            effect.labels.append(action.action_type.value)
            if not action.supported:
                effect.unsupported = True
                continue
            if action.action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
                effect.q *= action.q
            elif action.action_type in {ActionType.CASH_DIVIDEND, ActionType.SPECIAL_DIVIDEND}:
                effect.cash_old += action.cash_old
        return effect

    def cumulative_q(
        self,
        security_id: str,
        after: date | str,
        through: date | str,
        known_by: str | datetime | date | None = None,
    ) -> float:
        after, through = parse_date(after), parse_date(through)
        known = parse_dt(known_by) if known_by is not None else None
        factor = 1.0
        for action in self._by_security.get(security_id, []):
            if not (after < action.ex_date <= through and action.supported):
                continue
            if known is not None and action.available_at is not None and action.available_at > known:
                continue
            if action.action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
                factor *= action.q
        return factor

    def aggregate_between(
        self,
        security_id: str,
        after: date | str,
        through: date | str,
        known_by: str | datetime | date | None = None,
    ) -> ActionEffect:
        after, through = parse_date(after), parse_date(through)
        effect = ActionEffect()
        for action in self.actions(security_id, start=after, end=through, known_by=known_by):
            if action.ex_date <= after:
                continue
            effect.actions.append(action)
            effect.labels.append(action.action_type.value)
            if not action.supported:
                effect.unsupported = True
                continue
            if action.action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
                effect.q *= action.q
            elif action.action_type in {ActionType.CASH_DIVIDEND, ActionType.SPECIAL_DIVIDEND}:
                effect.cash_old += action.cash_old
        return effect

    def unsupported_between(self, security_id: str, start: date | str, end: date | str) -> list[CorporateAction]:
        start, end = parse_date(start), parse_date(end)
        return [
            a for a in self._by_security.get(security_id, [])
            if start < a.ex_date <= end and not a.supported
        ]

    def coverage(self, security_id: str, start: date | str, end: date | str) -> ActionCoverage | None:
        start, end = parse_date(start), parse_date(end)
        for coverage in self._coverages.get(security_id, []):
            if coverage.covers(start, end):
                return coverage
        return None

    def coverage_status(self, security_id: str, start: date | str, end: date | str) -> str:
        coverage = self.coverage(security_id, start, end)
        if coverage is not None:
            return "verified"
        if self._coverages.get(security_id):
            return "partial"
        return "unverified"

    def coverages(self) -> list[ActionCoverage]:
        return [c for items in self._coverages.values() for c in items]

    def securities(self) -> list[str]:
        return sorted(self._by_security)

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping],
        coverage_rows: Iterable[Mapping] = (),
    ) -> "ActionLedger":
        actions = []
        for row in rows:
            actions.append(
                CorporateAction(
                    security_id=str(row["security_id"]),
                    ex_date=parse_date(row["ex_date"]),
                    action_type=ActionType(str(row.get("action_type", "other"))),
                    q=float(row.get("q") or 1.0),
                    cash_old=float(row.get("cash_old") or 0.0),
                    currency=str(row.get("currency", "")),
                    announced_at=parse_dt(row["announced_at"]) if row.get("announced_at") else None,
                    available_at=parse_dt(row["available_at"]) if row.get("available_at") else None,
                    accession=str(row.get("accession", "")),
                    source_id=str(row.get("source_id", "")),
                    confirmed=bool(row.get("confirmed", True)),
                    supported=bool(row.get("supported", True)),
                    note=str(row.get("note", "")),
                )
            )
        coverages = []
        for row in coverage_rows:
            coverages.append(
                ActionCoverage(
                    security_id=str(row["security_id"]),
                    start=parse_date(row["start"]),
                    end=parse_date(row["end"]),
                    complete=bool(row.get("complete", False)),
                    evidence=str(row.get("evidence", "")),
                    unresolved=list(row.get("unresolved") or []),
                    source_id=str(row.get("source_id", "")),
                )
            )
        return cls(actions, coverages)


def load_actions_csv(path: str | Path, coverage_path: str | Path | None = None) -> ActionLedger:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    coverage_rows = []
    if coverage_path:
        with open(coverage_path, "r", encoding="utf-8-sig", newline="") as handle:
            coverage_rows = list(csv.DictReader(handle))
    try:
        return ActionLedger.from_rows(rows, coverage_rows)
    except (KeyError, ValueError) as exc:
        raise DataError(f"invalid actions file {path}: {exc}") from exc
