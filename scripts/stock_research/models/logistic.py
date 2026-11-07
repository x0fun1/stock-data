"""Regularised logistic regression implemented with numpy/scipy."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize


@dataclass
class LogisticL2:
    l2: float = 1.0
    max_iter: int = 1000
    tol: float = 1e-9
    coefficients_: np.ndarray | None = None
    intercept_: float = 0.0
    converged_: bool = False
    n_iter_: int = 0
    feature_names: list[str] = field(default_factory=list)

    def _unpack(self, theta: np.ndarray) -> tuple[float, np.ndarray]:
        return float(theta[0]), theta[1:]

    def _objective(self, theta: np.ndarray, X: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, np.ndarray]:
        intercept, coef = self._unpack(theta)
        eta = X @ coef + intercept
        # Stable log loss
        loss = np.logaddexp(0.0, eta) - y * eta
        obj = float(np.sum(w * loss) + 0.5 * self.l2 * np.dot(coef, coef))
        prob = 1.0 / (1.0 + np.exp(-eta))
        residual = w * (prob - y)
        grad = np.empty_like(theta)
        grad[0] = float(np.sum(residual))
        grad[1:] = X.T @ residual + self.l2 * coef
        return obj, grad

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> "LogisticL2":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        if X.ndim != 2 or len(X) != len(y):
            raise ValueError("X and y shapes are incompatible")
        if not np.isin(y, [0.0, 1.0]).all():
            raise ValueError("y must contain only 0 and 1")
        if sample_weight is None:
            w = np.ones(len(y), dtype=float)
        else:
            w = np.asarray(sample_weight, dtype=float)
            if len(w) != len(y) or np.any(w < 0):
                raise ValueError("sample_weight must be non-negative and match y")
        total = float(np.sum(w))
        if total <= 0:
            raise ValueError("sample weights sum to zero")
        w = w / total * len(y)
        theta0 = np.zeros(X.shape[1] + 1, dtype=float)
        if np.mean(y) not in (0.0, 1.0):
            theta0[0] = float(np.log(np.mean(y) / (1.0 - np.mean(y))))
        result = minimize(
            self._objective,
            theta0,
            args=(X, y, w),
            method="L-BFGS-B",
            jac=True,
            options={"maxiter": self.max_iter, "ftol": self.tol, "gtol": 1e-7},
        )
        self.intercept_, self.coefficients_ = self._unpack(result.x)
        self.converged_ = bool(result.success)
        self.n_iter_ = int(result.nit)
        return self

    def decision_function(self, X: np.ndarray) -> np.ndarray:
        if self.coefficients_ is None:
            raise RuntimeError("model is not fitted")
        return np.asarray(X, dtype=float) @ self.coefficients_ + self.intercept_

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        eta = self.decision_function(X)
        return 1.0 / (1.0 + np.exp(-np.clip(eta, -60.0, 60.0)))

    def to_dict(self) -> dict:
        if self.coefficients_ is None:
            raise RuntimeError("model is not fitted")
        return {
            "type": "logistic_l2",
            "l2": self.l2,
            "intercept": self.intercept_,
            "coefficients": self.coefficients_.tolist(),
            "feature_names": self.feature_names,
            "converged": self.converged_,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LogisticL2":
        model = cls(l2=float(data.get("l2", 1.0)))
        model.intercept_ = float(data["intercept"])
        model.coefficients_ = np.asarray(data["coefficients"], dtype=float)
        model.feature_names = list(data.get("feature_names", []))
        model.converged_ = bool(data.get("converged", True))
        return model
