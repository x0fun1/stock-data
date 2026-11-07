"""Honest baselines: historical base rate and a market/risk-only model."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BaseRate:
    rate: float

    @classmethod
    def fit(cls, y: np.ndarray) -> "BaseRate":
        y = np.asarray(y, dtype=float)
        if len(y) == 0:
            raise ValueError("cannot fit a base rate on an empty sample")
        return cls(rate=float(np.mean(y)))

    def predict_proba(self, n: int) -> np.ndarray:
        return np.full(n, self.rate, dtype=float)

    def to_dict(self) -> dict:
        return {"type": "base_rate", "rate": self.rate}
