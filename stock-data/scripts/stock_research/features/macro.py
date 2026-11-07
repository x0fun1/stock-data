"""Macro vintages and market-state factors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import log
from typing import Iterable, Sequence

from ..common.timeutil import parse_date
from ..contracts import FeatureObservation, MacroObservation, Market, MissingReason


DEFAULT_MACRO_SERIES = {
    "curve_10y_2y": ("DGS10", "DGS2"),
    "cpi_yoy": ("CPIAUCSL",),
    "fx_20d": ("FX",),
    "policy_change_20d": ("RATE",),
}


def resolve_macro_series_map(
    series_map: dict[str, str | Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    mapping = dict(DEFAULT_MACRO_SERIES)
    for name, value in (series_map or {}).items():
        if name not in mapping:
            raise ValueError("unknown macro factor in series map: " + name)
        values = (value,) if isinstance(value, str) else tuple(value)
        if len(values) != len(mapping[name]) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("macro series map has wrong series count or an empty identifier: " + name)
        mapping[name] = tuple(v.strip() for v in values)
    return mapping


@dataclass(slots=True)
class MacroStore:
    observations: list[MacroObservation]

    def __init__(self, observations: Iterable[MacroObservation] = ()) -> None:
        self.observations = list(observations)

    def history(self, series_id: str, known_by: datetime | None = None) -> list[MacroObservation]:
        latest: dict[date, MacroObservation] = {}
        for obs in self.observations:
            if obs.series_id != series_id:
                continue
            if known_by is not None:
                if obs.observation_date > known_by.date() or obs.available_at is None or obs.available_at > known_by:
                    continue
            existing = latest.get(obs.observation_date)
            if existing is None or (obs.available_at or datetime.min.replace(tzinfo=timezone.utc)) >= (existing.available_at or datetime.min.replace(tzinfo=timezone.utc)):
                latest[obs.observation_date] = obs
        return [latest[d] for d in sorted(latest)]

    def latest(self, series_id: str, known_by: datetime | None = None) -> MacroObservation | None:
        history = self.history(series_id, known_by=known_by)
        return history[-1] if history else None


def _miss(name: str, cutoff: date, reason: MissingReason | str, inputs: dict | None = None) -> FeatureObservation:
    return FeatureObservation(
        name=name,
        value=None,
        as_of=cutoff,
        status="missing",
        reason=reason.value if isinstance(reason, MissingReason) else str(reason),
        inputs=inputs or {},
    )


def _ok(name: str, value: float, cutoff: date, inputs: dict) -> FeatureObservation:
    return FeatureObservation(name=name, value=float(value), as_of=cutoff, inputs=inputs)


def compute_macro_features(
    store: MacroStore,
    cutoff: date | str,
    series_map: dict[str, str | Sequence[str]] | None = None,
    known_by: datetime | None = None,
    market: Market | str = Market.US,
) -> dict[str, FeatureObservation]:
    cutoff = parse_date(cutoff)
    mapping = resolve_macro_series_map(series_map)
    out: dict[str, FeatureObservation] = {}

    ten = store.latest(mapping["curve_10y_2y"][0], known_by)
    two = store.latest(mapping["curve_10y_2y"][1], known_by)
    if ten and two:
        out["curve_10y_2y"] = _ok(
            "curve_10y_2y", ten.value - two.value, cutoff,
            {"ten_year": ten.value, "two_year": two.value, "dates": [ten.observation_date.isoformat(), two.observation_date.isoformat()]},
        )
    else:
        out["curve_10y_2y"] = _miss("curve_10y_2y", cutoff, MissingReason.NO_DATA)

    cpi_series = mapping["cpi_yoy"][0]
    cpi_hist = store.history(cpi_series, known_by)
    if len(cpi_hist) >= 13:
        latest = cpi_hist[-1]
        prior = min(cpi_hist[:-1], key=lambda x: abs((latest.observation_date - x.observation_date).days - 365))
        if prior.value > 0:
            out["cpi_yoy"] = _ok("cpi_yoy", (latest.value / prior.value - 1.0) * 100.0, cutoff, {"latest": latest.observation_date.isoformat(), "prior": prior.observation_date.isoformat()})
        else:
            out["cpi_yoy"] = _miss("cpi_yoy", cutoff, MissingReason.NONPOSITIVE_BASE)
    else:
        out["cpi_yoy"] = _miss("cpi_yoy", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"observations": len(cpi_hist)})

    fx_series = mapping["fx_20d"][0]
    fx_hist = store.history(fx_series, known_by)
    if len(fx_hist) >= 21 and fx_hist[-21].value > 0:
        out["fx_20d"] = _ok("fx_20d", log(fx_hist[-1].value / fx_hist[-21].value), cutoff, {"latest": fx_hist[-1].observation_date.isoformat(), "prior": fx_hist[-21].observation_date.isoformat()})
    elif len(fx_hist) < 21:
        out["fx_20d"] = _miss("fx_20d", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"observations": len(fx_hist)})
    else:
        out["fx_20d"] = _miss("fx_20d", cutoff, MissingReason.NONPOSITIVE_BASE)

    rate_series = mapping["policy_change_20d"][0]
    rate_hist = store.history(rate_series, known_by)
    if len(rate_hist) >= 21:
        out["policy_change_20d"] = _ok("policy_change_20d", rate_hist[-1].value - rate_hist[-21].value, cutoff, {"latest": rate_hist[-1].observation_date.isoformat(), "prior": rate_hist[-21].observation_date.isoformat()})
    else:
        out["policy_change_20d"] = _miss("policy_change_20d", cutoff, MissingReason.WINDOW_INSUFFICIENT, {"observations": len(rate_hist)})
    return out
