"""Platt (sigmoid) probability calibration."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

EPS = 1e-6


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1.0 - EPS)
    return np.log(p / (1.0 - p))


@dataclass
class PlattCalibrator:
    a: float = 0.0
    b: float = 1.0
    converged_: bool = False

    def fit(self, p_raw: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        x = _logit(p_raw)
        y = np.asarray(y, dtype=float)
        if len(x) != len(y) or len(y) < 2:
            raise ValueError("calibration needs at least two matched observations")
        if np.all(y == y[0]):
            self.a, self.b, self.converged_ = 0.0, 1.0, False
            return self

        def objective(theta: np.ndarray) -> float:
            a, b = theta
            eta = a + b * x
            return float(np.sum(np.logaddexp(0.0, eta) - y * eta))

        result = minimize(objective, np.array([0.0, 1.0]), method="Nelder-Mead", options={"maxiter": 2000, "xatol": 1e-8, "fatol": 1e-10})
        self.a, self.b = float(result.x[0]), float(result.x[1])
        self.converged_ = bool(result.success)
        return self

    def predict(self, p_raw: np.ndarray) -> np.ndarray:
        x = _logit(p_raw)
        return np.clip(1.0 / (1.0 + np.exp(-np.clip(self.a + self.b * x, -60.0, 60.0))), EPS, 1.0 - EPS)

    def to_dict(self) -> dict:
        return {"type": "platt", "a": self.a, "b": self.b, "converged": self.converged_}

    @classmethod
    def from_dict(cls, data: dict) -> "PlattCalibrator":
        return cls(a=float(data.get("a", 0.0)), b=float(data.get("b", 1.0)), converged_=bool(data.get("converged", True)))
