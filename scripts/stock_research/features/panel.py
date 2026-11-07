"""Assemble all factor observations for one security and cutoff."""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from ..contracts import FeatureObservation, FinancialFact, MacroObservation, MissingReason, SecurityIdentity
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries
from ..identity_calendar.calendar import TradingCalendar
from .financial import FinancialStore, compute_financial_features
from .macro import MacroStore, compute_macro_features
from .price import compute_price_features
from .registry import FINANCIAL_FACTORS, MACRO_FACTORS


def build_feature_row(
    identity: SecurityIdentity,
    prices: PriceSeries,
    ledger: ActionLedger,
    cutoff: date,
    benchmark: PriceSeries | None = None,
    benchmark_ledger: ActionLedger | None = None,
    calendar: TradingCalendar | None = None,
    financial_facts: list[FinancialFact] | FinancialStore | None = None,
    macro_observations: list[MacroObservation] | MacroStore | None = None,
    price_config: Mapping | None = None,
    macro_series_map: dict[str, str | Sequence[str]] | None = None,
    include_display: bool = True,
) -> dict[str, FeatureObservation]:
    known_by = calendar.session_close_utc(cutoff) if calendar is not None else None
    out = compute_price_features(
        prices,
        ledger,
        cutoff,
        benchmark=benchmark,
        benchmark_ledger=benchmark_ledger,
        price_config=dict(price_config or {}),
        known_by=known_by,
        include_display=include_display,
        market=identity.market.value,
    )
    store = financial_facts if isinstance(financial_facts, FinancialStore) else FinancialStore(financial_facts or [])
    if financial_facts:
        out.update(compute_financial_features(identity, store, prices, ledger, cutoff, known_by=known_by))
    else:
        for name in FINANCIAL_FACTORS:
            out[name] = FeatureObservation(name=name, value=None, as_of=cutoff, status="missing", reason=MissingReason.NO_DATA.value)
    macro = macro_observations if isinstance(macro_observations, MacroStore) else MacroStore(macro_observations or [])
    if macro_observations:
        out.update(compute_macro_features(macro, cutoff, series_map=macro_series_map, known_by=known_by, market=identity.market))
    else:
        for name in MACRO_FACTORS:
            out[name] = FeatureObservation(name=name, value=None, as_of=cutoff, status="missing", reason=MissingReason.NO_DATA.value)
    return out
