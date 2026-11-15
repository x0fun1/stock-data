"""Macro vintages and point-in-time background readings for message research."""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import log
from typing import Any, Iterable, Sequence

from ..contracts import MacroObservation

DEFAULT_MACRO_SERIES = {
    "curve_10y_2y": ("DGS10", "DGS2"),
    "cpi_yoy": ("CPIAUCSL",),
    "fx_20d": ("FX",),
    "policy_change_20d": ("RATE",),
}

LABELS = {
    "curve_10y_2y": "10年−2年国债利差（百分点）",
    "cpi_yoy": "CPI同比（%）",
    "fx_20d": "汇率20个观测对数变化",
    "policy_change_20d": "政策利率20个观测变化（百分点）",
}


def resolve_macro_series_map(
    series_map: dict[str, str | Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    mapping = dict(DEFAULT_MACRO_SERIES)
    for name, value in (series_map or {}).items():
        if name not in mapping:
            raise ValueError("unknown macro reading in series map: " + name)
        values = (value,) if isinstance(value, str) else tuple(value)
        if len(values) != len(mapping[name]) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("macro series map has wrong series count or an empty identifier: " + name)
        mapping[name] = tuple(v.strip() for v in values)
    return mapping


class MacroStore:
    def __init__(self, observations: Iterable[MacroObservation] = ()) -> None:
        self.observations = list(observations)
        self._by_series: dict[str, list[MacroObservation]] = {}
        for observation in self.observations:
            self._by_series.setdefault(observation.series_id, []).append(observation)

    def history(self, series_id: str, known_by: datetime | None = None) -> list[MacroObservation]:
        floor = datetime.min.replace(tzinfo=timezone.utc)
        latest: dict[date, MacroObservation] = {}
        for obs in self._by_series.get(series_id, []):
            if known_by is not None:
                if obs.observation_date > known_by.date() or obs.available_at is None or obs.available_at > known_by:
                    continue
            existing = latest.get(obs.observation_date)
            if existing is None or (obs.available_at or floor) >= (existing.available_at or floor):
                latest[obs.observation_date] = obs
        return [latest[d] for d in sorted(latest)]

    def latest(self, series_id: str, known_by: datetime | None = None) -> MacroObservation | None:
        history = self.history(series_id, known_by=known_by)
        return history[-1] if history else None


def macro_background(
    store: MacroStore,
    known_by: datetime,
    series_map: dict[str, str | Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Describe what was publicly known by the cutoff; a gap keeps its reason."""
    mapping = resolve_macro_series_map(series_map)

    def reading(name: str, value: float | None, reason: str = "", **inputs: Any) -> dict[str, Any]:
        return {"name": name, "label": LABELS[name], "series": list(mapping[name]),
                "status": "ok" if value is not None else "missing",
                "value": value, "reason": reason, "inputs": inputs}

    out = []
    ten = store.latest(mapping["curve_10y_2y"][0], known_by)
    two = store.latest(mapping["curve_10y_2y"][1], known_by)
    if ten and two:
        out.append(reading("curve_10y_2y", ten.value - two.value,
                           latest=max(ten.observation_date, two.observation_date).isoformat()))
    else:
        out.append(reading("curve_10y_2y", None, "no_data"))

    cpi = store.history(mapping["cpi_yoy"][0], known_by)
    if len(cpi) < 13:
        out.append(reading("cpi_yoy", None, "window_insufficient", observations=len(cpi)))
    else:
        latest = cpi[-1]
        prior = min(cpi[:-1], key=lambda x: abs((latest.observation_date - x.observation_date).days - 365))
        if prior.value > 0:
            out.append(reading("cpi_yoy", (latest.value / prior.value - 1.0) * 100.0,
                               latest=latest.observation_date.isoformat(), prior=prior.observation_date.isoformat()))
        else:
            out.append(reading("cpi_yoy", None, "nonpositive_base"))

    fx = store.history(mapping["fx_20d"][0], known_by)
    if len(fx) < 21:
        out.append(reading("fx_20d", None, "window_insufficient", observations=len(fx)))
    elif fx[-21].value <= 0 or fx[-1].value <= 0:
        out.append(reading("fx_20d", None, "nonpositive_base"))
    else:
        out.append(reading("fx_20d", log(fx[-1].value / fx[-21].value),
                           latest=fx[-1].observation_date.isoformat(), prior=fx[-21].observation_date.isoformat()))

    rate = store.history(mapping["policy_change_20d"][0], known_by)
    if len(rate) < 21:
        out.append(reading("policy_change_20d", None, "window_insufficient", observations=len(rate)))
    else:
        out.append(reading("policy_change_20d", rate[-1].value - rate[-21].value,
                           latest=rate[-1].observation_date.isoformat(), prior=rate[-21].observation_date.isoformat()))
    return out
