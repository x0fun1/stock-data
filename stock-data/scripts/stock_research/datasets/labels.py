"""Prediction labels with explicit entry/exit sessions and action-aware returns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from ..common.timeutil import parse_date
from ..contracts import LabelRecord, Market, MissingReason, Target
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries, compute_path_risk, compute_window_return
from ..identity_calendar.calendar import TradingCalendar


@dataclass(slots=True)
class LabelConfig:
    require_complete_path: bool = True
    maturity_delay_hours: int = 2
    require_verified_action_coverage: bool = False
    reinvest_dividends: bool = True


def build_label(
    security_id: str,
    market: Market | str,
    prices: PriceSeries,
    ledger: ActionLedger,
    benchmark: PriceSeries | None,
    benchmark_ledger: ActionLedger | None,
    calendar: TradingCalendar,
    cutoff: date | str,
    horizon: int,
    target: Target | str = Target.ABSOLUTE,
    config: LabelConfig | None = None,
) -> LabelRecord:
    cutoff = parse_date(cutoff)
    market = Market(market)
    target = Target(target)
    config = config or LabelConfig()
    record = LabelRecord(
        security_id=security_id,
        market=market,
        cutoff_date=cutoff,
        horizon=int(horizon),
        target=target,
    )
    if not calendar.is_session(cutoff):
        record.status = "missing"
        record.missing_reason = MissingReason.NO_DATA.value
        record.detail = {"cutoff_not_session": cutoff.isoformat()}
        return record
    entry = calendar.next_session(cutoff)
    end = calendar.session_offset(entry, int(horizon) - 1)
    record.entry_date = entry
    record.end_date = end
    record.matured_at = calendar.session_close_utc(end) + timedelta(hours=config.maturity_delay_hours)
    if config.require_verified_action_coverage:
        coverage = ledger.coverage_status(security_id, entry, end)
        if coverage != "verified":
            record.status = "missing"
            record.missing_reason = MissingReason.SOURCE_NOT_ELIGIBLE.value
            record.detail = {"action_coverage": coverage}
            return record
    if target == Target.PATH_RISK:
        risk = compute_path_risk(
            prices,
            ledger,
            entry,
            end,
            calendar,
            reinvest=config.reinvest_dividends,
        )
        record.detail = {"path_risk": risk}
        if risk.get("status") != "ok":
            record.status = "missing"
            record.missing_reason = str(risk.get("reason", MissingReason.INCOMPLETE_PATH.value))
        else:
            record.status = "ok"
            record.return_value = float(risk["return"])
        return record

    result = compute_window_return(
        prices,
        ledger,
        entry,
        end,
        calendar=calendar,
        benchmark=benchmark if target in {Target.EXCESS, Target.ABSOLUTE} else None,
        benchmark_ledger=benchmark_ledger,
        reinvest=config.reinvest_dividends,
        require_complete_path=config.require_complete_path,
    )
    record.detail = dict(result.detail)
    record.detail["path_complete"] = result.path_complete
    record.detail["actions_applied"] = result.actions_applied
    if result.status != "ok" or result.return_value is None:
        record.status = "missing"
        record.missing_reason = result.missing_reason or MissingReason.INCOMPLETE_PATH.value
        return record
    record.return_value = result.return_value
    record.benchmark_return = result.benchmark_return
    record.excess_return = result.excess_return
    if target == Target.ABSOLUTE:
        record.label = 1 if result.return_value > 0 else 0
    elif target == Target.EXCESS:
        if result.excess_return is None:
            record.status = "missing"
            record.missing_reason = MissingReason.NO_DATA.value
            record.detail["benchmark_missing"] = True
            return record
        record.label_excess = 1 if result.excess_return > 0 else 0
    return record


def build_labels(
    security_ids: Iterable[str],
    market: Market | str,
    price_map: dict[str, PriceSeries],
    ledger: ActionLedger,
    benchmark: PriceSeries | None,
    benchmark_ledger: ActionLedger | None,
    calendar: TradingCalendar,
    cutoffs: Iterable[date],
    horizons: Iterable[int],
    targets: Iterable[Target] = (Target.ABSOLUTE, Target.EXCESS),
    config: LabelConfig | None = None,
) -> list[LabelRecord]:
    out: list[LabelRecord] = []
    for security_id in security_ids:
        prices = price_map.get(security_id)
        if prices is None:
            continue
        for cutoff in cutoffs:
            for horizon in horizons:
                for target in targets:
                    out.append(
                        build_label(
                            security_id,
                            market,
                            prices,
                            ledger,
                            benchmark,
                            benchmark_ledger,
                            calendar,
                            cutoff,
                            horizon,
                            target,
                            config,
                        )
                    )
    return out
