"""Training-only feature transformation.

Winsor limits, median, MAD/IQR/standard-deviation fallbacks and missing
indicators are fitted on the training block only and serialised with the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass
class TransformerParams:
    features: list[dict] = field(default_factory=list)
    clip: float = 3.0
    winsor_low: float = 0.01
    winsor_high: float = 0.99
    max_missing_rate: float = 0.95

    @property
    def output_names(self) -> list[str]:
        names = []
        for item in self.features:
            names.append(item["name"])
            if item.get("indicator"):
                names.append(f"miss__{item['name']}")
        return names

    def to_dict(self) -> dict:
        return {
            "features": self.features,
            "clip": self.clip,
            "winsor_low": self.winsor_low,
            "winsor_high": self.winsor_high,
            "max_missing_rate": self.max_missing_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TransformerParams":
        return cls(
            features=list(data.get("features", [])),
            clip=float(data.get("clip", 3.0)),
            winsor_low=float(data.get("winsor_low", 0.01)),
            winsor_high=float(data.get("winsor_high", 0.99)),
            max_missing_rate=float(data.get("max_missing_rate", 0.95)),
        )


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    if f"f__{name}" in frame.columns:
        return pd.to_numeric(frame[f"f__{name}"], errors="coerce")
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _missing_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if f"m__{name}" in frame.columns:
        return pd.to_numeric(frame[f"m__{name}"], errors="coerce").fillna(1).astype(int)
    return _column(frame, name).isna().astype(int)


class FeatureTransformer:
    def __init__(self, params: TransformerParams | None = None) -> None:
        self.params = params or TransformerParams()

    def fit(
        self,
        frame: pd.DataFrame,
        feature_names: Iterable[str],
        winsor_quantiles: tuple[float, float] = (0.01, 0.99),
        clip: float = 3.0,
        max_missing_rate: float = 0.95,
    ) -> "FeatureTransformer":
        self.params = TransformerParams(
            clip=clip,
            winsor_low=winsor_quantiles[0],
            winsor_high=winsor_quantiles[1],
            max_missing_rate=max_missing_rate,
        )
        for name in feature_names:
            values = _column(frame, name).dropna()
            miss_rate = 1.0 - len(values) / max(len(frame), 1)
            if len(values) < 2 or miss_rate > max_missing_rate:
                continue
            low = float(values.quantile(self.params.winsor_low))
            high = float(values.quantile(self.params.winsor_high))
            clipped = values.clip(low, high)
            median = float(clipped.median())
            mad = float(np.median(np.abs(clipped - median)))
            scale = 1.4826 * mad
            if not np.isfinite(scale) or scale <= 0:
                q75, q25 = clipped.quantile(0.75), clipped.quantile(0.25)
                iqr = float(q75 - q25)
                scale = iqr / 1.349 if iqr > 0 else float(clipped.std(ddof=1))
            if not np.isfinite(scale) or scale <= 0:
                continue
            indicator = bool((_missing_column(frame, name) == 1).any())
            self.params.features.append(
                {
                    "name": name,
                    "low": low,
                    "high": high,
                    "median": median,
                    "scale": scale,
                    "indicator": indicator,
                }
            )
        if not self.params.features:
            raise ValueError("no usable feature columns after training-only filtering")
        return self

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        columns: list[np.ndarray] = []
        for item in self.params.features:
            values = _column(frame, item["name"])
            missing = _missing_column(frame, item["name"]) == 1
            values = values.clip(item["low"], item["high"])
            scaled = (values - item["median"]) / item["scale"]
            scaled = scaled.clip(-self.params.clip, self.params.clip)
            scaled = scaled.fillna(0.0)
            columns.append(scaled.to_numpy(dtype=float))
            if item.get("indicator"):
                columns.append(missing.to_numpy(dtype=float))
        return np.column_stack(columns) if columns else np.empty((len(frame), 0))

    @property
    def output_names(self) -> list[str]:
        return self.params.output_names

    def to_dict(self) -> dict:
        return self.params.to_dict()

    @classmethod
    def from_dict(cls, data: dict) -> "FeatureTransformer":
        return cls(TransformerParams.from_dict(data))
