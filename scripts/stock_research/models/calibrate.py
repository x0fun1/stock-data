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

    def diagnostics(self, p_raw: np.ndarray) -> dict:
        values = np.asarray(p_raw,dtype=float)
        finite = values[np.isfinite(values)]
        output = self.predict(finite) if len(finite) else np.array([])
        threshold = float(1/(1+np.exp(-np.clip(-self.a/self.b,-60,60)))) if self.b else None
        return {'parameters':self.to_dict(),'ranking_reversed':self.b<0,
            'raw_threshold_for_calibrated_half':threshold,
            'threshold_sense':'raw_below_threshold_predicts_up' if self.b<0 else 'raw_above_threshold_predicts_up' if self.b>0 else 'constant',
            'input_score_min':float(finite.min()) if len(finite) else None,
            'input_score_max':float(finite.max()) if len(finite) else None,
            'output_score_min':float(output.min()) if len(output) else None,
            'output_score_max':float(output.max()) if len(output) else None,
            'output_up_fraction':float((output>=.5).mean()) if len(output) else None,
            'is_direction_training':False}

    @classmethod
    def from_dict(cls, data: dict) -> "PlattCalibrator":
        return cls(a=float(data.get("a", 0.0)), b=float(data.get("b", 1.0)), converged_=bool(data.get("converged", True)))
