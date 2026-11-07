"""Dataset assembly: feature rows joined with action-aware labels."""

from __future__ import annotations

from datetime import date
from typing import Iterable

import pandas as pd

from ..common.timeutil import now_utc
from ..contracts import (
    DatasetManifest,
    FinancialFact,
    LabelRecord,
    MacroObservation,
    SecurityIdentity,
    Target,
    to_dict,
)
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries
from ..features.panel import build_feature_row
from ..features.metadata import metadata_asof
from ..features.registry import INITIAL_MODEL_CANDIDATES
from ..identity_calendar.calendar import TradingCalendar
from .labels import LabelConfig, build_label


def build_dataset(
    identities: Iterable[SecurityIdentity],
    price_map: dict[str, PriceSeries],
    ledger: ActionLedger,
    benchmark: PriceSeries | None,
    benchmark_ledger: ActionLedger | None,
    calendar: TradingCalendar,
    cutoffs: Iterable[date],
    horizons: Iterable[int],
    targets: Iterable[Target] = (Target.ABSOLUTE, Target.EXCESS),
    factors: Iterable[str] | None = None,
    financial_facts: list[FinancialFact] | None = None,
    macro_observations: list[MacroObservation] | None = None,
    price_config: dict | None = None,
    label_config: LabelConfig | None = None,
    macro_series_map: dict[str, str] | None = None,
    security_metadata: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, DatasetManifest]:
    factor_names = list(factors or INITIAL_MODEL_CANDIDATES)
    horizon_list = [int(h) for h in horizons]
    target_list = [Target(t) for t in targets]
    cutoff_list = sorted({pd.Timestamp(c).date() for c in cutoffs})
    rows: list[dict] = []
    exclusions: dict[str, int] = {}
    identities = list(identities)
    for identity in identities:
        prices = price_map.get(identity.security_id)
        if prices is None:
            exclusions["no_prices"] = exclusions.get("no_prices", 0) + 1
            continue
        for cutoff in cutoff_list:
            if not calendar.is_session(cutoff):
                exclusions["cutoff_not_session"] = exclusions.get("cutoff_not_session", 0) + 1
                continue
            features = build_feature_row(
                identity,
                prices,
                ledger,
                cutoff,
                benchmark=benchmark,
                benchmark_ledger=benchmark_ledger,
                calendar=calendar,
                financial_facts=financial_facts,
                macro_observations=macro_observations,
                price_config=price_config,
                macro_series_map=macro_series_map,
                include_display=False,
            )
            controls = metadata_asof(security_metadata, identity.security_id, cutoff, calendar.session_close_utc(cutoff))
            for horizon in horizon_list:
                for target in target_list:
                    label: LabelRecord = build_label(
                        identity.security_id,
                        identity.market,
                        prices,
                        ledger,
                        benchmark,
                        benchmark_ledger,
                        calendar,
                        cutoff,
                        horizon,
                        target,
                        label_config,
                    )
                    row: dict = {
                        "security_id": identity.security_id,
                        "market": identity.market.value,
                        "security_type": identity.security_type,
                        "industry": controls.get("industry", identity.industry),
                        "industry_pit_confirmed": controls.get("industry_pit_confirmed", False),
                        "size_pit_confirmed": bool(controls.get("size_pit_confirmed", False) and all(b.available_at is not None for b in prices.bars if b.date <= cutoff)),
                        "cutoff_date": cutoff.isoformat(),
                        "feature_cutoff_at": calendar.session_close_utc(cutoff).isoformat(),
                        "horizon": int(horizon),
                        "target": Target(target).value,
                        "entry_date": label.entry_date.isoformat() if label.entry_date else None,
                        "end_date": label.end_date.isoformat() if label.end_date else None,
                        "matured_at": label.matured_at.isoformat() if label.matured_at else None,
                        "label_status": label.status,
                        "label_missing_reason": label.missing_reason,
                        "return_value": label.return_value,
                        "benchmark_return": label.benchmark_return,
                        "excess_return": label.excess_return,
                        "label": label.label,
                        "label_excess": label.label_excess,
                    }
                    for name in factor_names:
                        obs = features.get(name)
                        row[f"f__{name}"] = None if obs is None else obs.value
                        row[f"m__{name}"] = 1 if obs is None or obs.status != "ok" else 0
                        row[f"r__{name}"] = "" if obs is None else obs.reason
                    rows.append(row)
    frame = pd.DataFrame(rows)
    manifest = DatasetManifest(
        dataset_id="features_labels",
        market=identities[0].market if identities else "US",
        version="pending",
        created_at=now_utc(),
        row_count=len(frame),
        security_count=int(frame["security_id"].nunique()) if not frame.empty else 0,
        date_start=cutoff_list[0] if cutoff_list else None,
        date_end=cutoff_list[-1] if cutoff_list else None,
        factor_version="f1",
        label_version="label_v1",
        exclusions=exclusions,
        metadata={
            "factors": factor_names,
            "horizons": horizon_list,
            "targets": [t.value for t in target_list],
            "price_config": price_config or {},
        },
    )
    return frame, manifest


def feature_frame(
    identities: Iterable[SecurityIdentity],
    price_map: dict[str, PriceSeries],
    ledger: ActionLedger,
    benchmark: PriceSeries | None,
    benchmark_ledger: ActionLedger | None,
    calendar: TradingCalendar,
    cutoff: date,
    factors: Iterable[str] | None = None,
    financial_facts: list[FinancialFact] | None = None,
    macro_observations: list[MacroObservation] | None = None,
    price_config: dict | None = None,
    macro_series_map: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict[str, dict]]:
    factor_names = list(factors or INITIAL_MODEL_CANDIDATES)
    rows = []
    raw: dict[str, dict] = {}
    for identity in identities:
        prices = price_map.get(identity.security_id)
        if prices is None:
            continue
        features = build_feature_row(
            identity,
            prices,
            ledger,
            cutoff,
            benchmark=benchmark,
            benchmark_ledger=benchmark_ledger,
            calendar=calendar,
            financial_facts=financial_facts,
            macro_observations=macro_observations,
            price_config=price_config,
            macro_series_map=macro_series_map,
            include_display=True,
        )
        raw[identity.security_id] = features
        row = {"security_id": identity.security_id, "market": identity.market.value, "cutoff_date": cutoff.isoformat()}
        for name in factor_names:
            obs = features.get(name)
            row[f"f__{name}"] = None if obs is None else obs.value
            row[f"m__{name}"] = 1 if obs is None or obs.status != "ok" else 0
            row[f"r__{name}"] = "" if obs is None else obs.reason
        rows.append(row)
    return pd.DataFrame(rows), raw


def label_frame(labels: Iterable[LabelRecord]) -> pd.DataFrame:
    return pd.DataFrame([to_dict(label) for label in labels])
