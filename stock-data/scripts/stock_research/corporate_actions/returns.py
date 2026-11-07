"""Action-aware return, wealth-index, label and path-risk engine.

The engine works from raw prices plus an independent action ledger.  It never
uses a vendor-adjusted price series as the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import exp, log
from typing import Iterable, Mapping

from ..common.errors import DataError
from ..common.timeutil import parse_date
from ..contracts import ActionType, CorporateAction, MissingReason, PriceBar, WindowResult
from ..identity_calendar.calendar import TradingCalendar
from .ledger import ActionEffect, ActionLedger


class PriceSeries:
    def __init__(self, bars: Iterable[PriceBar], security_id: str = "") -> None:
        ordered = sorted(bars, key=lambda b: b.date)
        seen: dict[date, PriceBar] = {}
        for bar in ordered:
            if bar.date in seen:
                raise DataError(f"duplicate price bar for {bar.security_id} on {bar.date}")
            seen[bar.date] = bar
        self.security_id = security_id or (ordered[0].security_id if ordered else "")
        self.bars = ordered
        self.by_date = seen
        self.dates = [b.date for b in ordered]

    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return bool(self.bars)

    def slice(self, end: date | str | None = None, known_by=None) -> "PriceSeries":
        """Mask future dates and explicitly late observations.

        Unknown availability is retained for descriptive research; retaining
        such a bar does not establish historical PIT readiness.
        """
        end = parse_date(end) if end is not None else None
        return PriceSeries([b for b in self.bars if (end is None or b.date <= end) and
                            (known_by is None or b.available_at is None or b.available_at <= known_by)],
                           self.security_id)

    def close(self, day: date | str) -> float | None:
        bar = self.by_date.get(parse_date(day))
        return None if bar is None else bar.close

    def open(self, day: date | str) -> float | None:
        bar = self.by_date.get(parse_date(day))
        return None if bar is None else bar.open

    def volume(self, day: date | str) -> float | None:
        bar = self.by_date.get(parse_date(day))
        return None if bar is None else bar.volume

    def amount(self, day: date | str) -> float | None:
        bar = self.by_date.get(parse_date(day))
        if bar is None:
            return None
        if bar.amount is not None and bar.amount > 0:
            return bar.amount
        if bar.close is not None and bar.volume is not None and bar.close > 0 and bar.volume > 0:
            return bar.close * bar.volume
        return None

    def missing_sessions(self, calendar: TradingCalendar, start: date, end: date) -> list[date]:
        return [
            day for day in calendar.sessions(start, end)
            if day not in self.by_date or self.by_date[day].close is None
        ]


@dataclass(slots=True)
class TotalReturnSeries:
    security_id: str
    dates: list[date]
    index: list[float]
    log_returns: list[float]
    simple_returns: list[float]
    quality: str = "ok"
    notes: list[str] = field(default_factory=list)
    unsupported_actions: list[CorporateAction] = field(default_factory=list)

    def index_as_of(self, day: date | str) -> float | None:
        day = parse_date(day)
        for d, value in zip(self.dates, self.index):
            if d == day:
                return value
        return None

    def log_return_at(self, day: date | str) -> float | None:
        day = parse_date(day)
        for i, d in enumerate(self.dates[1:], start=1):
            if d == day:
                return self.log_returns[i - 1]
        return None


def _actions_between(
    ledger: ActionLedger,
    security_id: str,
    after: date,
    through: date,
    known_by=None,
) -> list[CorporateAction]:
    return [
        a for a in ledger.actions(security_id, start=after, end=through, known_by=known_by)
        if after < a.ex_date <= through
    ]


def _apply_effects(actions: list[CorporateAction]) -> ActionEffect:
    effect = ActionEffect()
    for action in actions:
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


def build_total_return_series(
    series: PriceSeries,
    ledger: ActionLedger,
    cutoff: date | str | None = None,
    known_by=None,
) -> TotalReturnSeries:
    cutoff = parse_date(cutoff) if cutoff is not None else (series.dates[-1] if series else None)
    if cutoff is None:
        return TotalReturnSeries(series.security_id, [], [], [], [], quality="missing", notes=["no price data"])
    bars = series.slice(cutoff, known_by=known_by).bars
    if not bars:
        return TotalReturnSeries(series.security_id, [], [], [], [], quality="missing", notes=["no price data before cutoff"])
    dates: list[date] = [bars[0].date]
    index: list[float] = [1.0]
    log_returns: list[float] = []
    simple_returns: list[float] = []
    quality = "ok"
    notes: list[str] = []
    unsupported: list[CorporateAction] = []
    if known_by is not None:
        for action in ledger.actions(series.security_id, end=cutoff):
            if action.available_at is None:
                notes.append(f"action_availability_unknown:{action.ex_date.isoformat()}")
    for prev, curr in zip(bars, bars[1:]):
        if prev.close is None or prev.close <= 0 or curr.close is None or curr.close <= 0:
            quality = "break"
            notes.append(f"nonpositive_or_missing_close:{curr.date.isoformat()}")
            break
        actions = _actions_between(ledger, series.security_id, prev.date, curr.date, known_by=known_by)
        effect = _apply_effects(actions)
        if effect.unsupported:
            quality = "unreliable"
            unsupported.extend([a for a in effect.actions if not a.supported])
            notes.append(f"unsupported_action:{curr.date.isoformat()}")
        growth = (effect.q * curr.close + effect.cash_old) / prev.close
        if growth <= 0:
            quality = "break"
            notes.append(f"nonpositive_growth:{curr.date.isoformat()}")
            break
        index.append(index[-1] * growth)
        log_returns.append(log(growth))
        simple_returns.append(growth - 1.0)
        dates.append(curr.date)
    return TotalReturnSeries(
        security_id=series.security_id,
        dates=dates,
        index=index,
        log_returns=log_returns,
        simple_returns=simple_returns,
        quality=quality,
        notes=notes,
        unsupported_actions=unsupported,
    )


def adjusted_history(
    series: PriceSeries,
    ledger: ActionLedger,
    cutoff: date | str,
    known_by=None,
) -> list[dict]:
    """Convert bars up to ``cutoff`` into the share basis effective at cutoff."""
    cutoff = parse_date(cutoff)
    out = []
    for bar in series.bars:
        if bar.date > cutoff or (known_by is not None and bar.available_at is not None and bar.available_at > known_by):
            continue
        q = ledger.cumulative_q(series.security_id, bar.date, cutoff, known_by=known_by)
        row = {
            "date": bar.date,
            "open": None if bar.open is None else bar.open / q,
            "high": None if bar.high is None else bar.high / q,
            "low": None if bar.low is None else bar.low / q,
            "close": None if bar.close is None else bar.close / q,
            "volume": None if bar.volume is None else bar.volume * q,
            "amount": bar.amount,
            "q_factor": q,
        }
        out.append(row)
    return out


def _valued_actions(
    ledger: ActionLedger,
    security_id: str,
    entry_date: date,
    end_date: date,
    known_by=None,
) -> tuple[list[CorporateAction], str]:
    actions = _actions_between(ledger, security_id, entry_date, end_date, known_by=known_by)
    unsupported = [a for a in actions if not a.supported]
    if unsupported:
        return actions, MissingReason.UNSUPPORTED_ACTION.value
    return actions, ""


def compute_window_return(
    series: PriceSeries,
    ledger: ActionLedger,
    entry_date: date | str,
    end_date: date | str,
    calendar: TradingCalendar | None = None,
    benchmark: PriceSeries | None = None,
    benchmark_ledger: ActionLedger | None = None,
    reinvest: bool = True,
    require_complete_path: bool = True,
    known_by=None,
) -> WindowResult:
    entry_date, end_date = parse_date(entry_date), parse_date(end_date)
    if known_by is not None:
        series = series.slice(end_date, known_by=known_by)
    if end_date < entry_date:
        return WindowResult("missing", entry_date, end_date, missing_reason=MissingReason.INCOMPLETE_PATH.value)
    entry_price = series.open(entry_date)
    if entry_price is None or entry_price <= 0:
        return WindowResult("missing", entry_date, end_date, missing_reason=MissingReason.NO_OPEN.value)
    end_price = series.close(end_date)
    if end_price is None or end_price <= 0:
        return WindowResult("missing", entry_date, end_date, missing_reason=MissingReason.NO_CLOSE.value)
    detail: dict = {"entry_open": entry_price, "end_close": end_price}
    path_complete = True
    if calendar is not None:
        missing = series.missing_sessions(calendar, entry_date, end_date)
        if missing:
            path_complete = False
            detail["missing_sessions"] = [d.isoformat() for d in missing]
            if require_complete_path:
                return WindowResult(
                    "missing", entry_date, end_date,
                    missing_reason=MissingReason.INCOMPLETE_PATH.value,
                    path_complete=False,
                    detail=detail,
                )
    actions, unsupported_reason = _valued_actions(ledger, series.security_id, entry_date, end_date, known_by=known_by)
    if unsupported_reason:
        return WindowResult(
            "missing", entry_date, end_date,
            missing_reason=unsupported_reason,
            path_complete=path_complete,
            detail=detail,
        )
    shares = 1.0
    cash = 0.0
    applied: list[str] = []
    by_date: dict[date, list[CorporateAction]] = {}
    for action in actions:
        by_date.setdefault(action.ex_date, []).append(action)
    for action_date in sorted(by_date):
        if action_date <= entry_date:
            continue
        close = series.close(action_date)
        if close is None or close <= 0:
            return WindowResult(
                "missing", entry_date, end_date,
                missing_reason=MissingReason.NO_CLOSE.value,
                path_complete=False,
                detail={**detail, "action_date": action_date.isoformat()},
            )
        effect = _apply_effects(by_date[action_date])
        if effect.unsupported:
            return WindowResult(
                "missing", entry_date, end_date,
                missing_reason=MissingReason.UNSUPPORTED_ACTION.value,
                path_complete=path_complete,
                detail={**detail, "action_date": action_date.isoformat()},
            )
        cash += effect.cash_old * shares
        shares *= effect.q
        if reinvest and cash:
            shares += cash / close
            cash = 0.0
        applied.append(effect.label())
    end_value = shares * end_price + cash
    gross_return = end_value / entry_price - 1.0
    detail.update({"shares": shares, "cash": cash, "end_value": end_value})

    benchmark_return = None
    excess = None
    if benchmark is not None:
        bench = compute_window_return(
            benchmark,
            benchmark_ledger or ActionLedger(),
            entry_date,
            end_date,
            calendar=calendar,
            reinvest=reinvest,
            require_complete_path=require_complete_path,
            known_by=known_by,
        )
        if bench.status == "ok":
            benchmark_return = bench.return_value
            excess = gross_return - benchmark_return
        else:
            detail["benchmark_missing"] = bench.missing_reason
    return WindowResult(
        "ok",
        entry_date,
        end_date,
        return_value=gross_return,
        benchmark_return=benchmark_return,
        excess_return=excess,
        path_complete=path_complete,
        actions_applied=applied,
        detail=detail,
    )


def compute_path_risk(
    series: PriceSeries,
    ledger: ActionLedger,
    entry_date: date | str,
    end_date: date | str,
    calendar: TradingCalendar,
    thresholds: tuple[float, ...] = (-0.05, -0.10, -0.20),
    reinvest: bool = True,
    known_by=None,
) -> dict:
    entry_date, end_date = parse_date(entry_date), parse_date(end_date)
    if known_by is not None:
        series = series.slice(end_date, known_by=known_by)
    entry_price = series.open(entry_date)
    if entry_price is None or entry_price <= 0:
        return {"status": "missing", "reason": MissingReason.NO_OPEN.value}
    sessions = calendar.sessions(entry_date, end_date)
    missing = series.missing_sessions(calendar, entry_date, end_date)
    if missing:
        return {
            "status": "missing",
            "reason": MissingReason.INCOMPLETE_PATH.value,
            "missing_sessions": [d.isoformat() for d in missing],
        }
    actions = _actions_between(ledger, series.security_id, entry_date, end_date, known_by=known_by)
    if any(not a.supported for a in actions):
        return {"status": "missing", "reason": MissingReason.UNSUPPORTED_ACTION.value}
    by_date: dict[date, list[CorporateAction]] = {}
    for action in actions:
        by_date.setdefault(action.ex_date, []).append(action)
    shares = 1.0
    cash = 0.0
    running_peak = entry_price
    max_drawdown = 0.0
    worst_return = 0.0
    first_touch: dict[str, str | None] = {f"{t:.4f}": None for t in thresholds}
    values: list[float] = []
    for day in sessions:
        close = series.close(day)
        if close is None or close <= 0:
            return {"status": "missing", "reason": MissingReason.NO_CLOSE.value, "date": day.isoformat()}
        if day > entry_date and day in by_date:
            effect = _apply_effects(by_date[day])
            cash += effect.cash_old * shares
            shares *= effect.q
            if reinvest and cash:
                shares += cash / close
                cash = 0.0
        value = shares * close + cash
        values.append(value)
        ret = value / entry_price - 1.0
        worst_return = min(worst_return, ret)
        running_peak = max(running_peak, value)
        max_drawdown = min(max_drawdown, value / running_peak - 1.0)
        for threshold in thresholds:
            key = f"{threshold:.4f}"
            if first_touch[key] is None and ret <= threshold + 1e-12:
                first_touch[key] = day.isoformat()
    end_price = series.close(end_date)
    end_return = (shares * end_price + cash) / entry_price - 1.0
    return {
        "status": "ok",
        "entry_date": entry_date.isoformat(),
        "end_date": end_date.isoformat(),
        "return": end_return,
        "worst_floating_return": worst_return,
        "max_drawdown_close_path": max_drawdown,
        "first_touch": first_touch,
        "path_complete": True,
        "daily_values": len(values),
    }
