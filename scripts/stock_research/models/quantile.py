"""Regularised linear quantile regression (IRLS approximation).

IRLS with asymmetric weights is a convex, fast approximation to pinball-loss
minimisation.  It is recorded as an approximation in the model metadata and is
never presented as an exact quantile solver.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class QuantileLinear:
    tau: float = 0.5
    l2: float = 0.01
    max_iter: int = 100
    tol: float = 1e-8
    coefficients_: np.ndarray | None = None
    intercept_: float = 0.0
    n_iter_: int = 0
    converged_: bool = False

    def fit(self, X: np.ndarray, y: np.ndarray) -> "QuantileLinear":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        if X.ndim != 2 or len(X) != len(y):
            raise ValueError("X and y shapes are incompatible")
        if not 0 < self.tau < 1:
            raise ValueError("tau must be in (0, 1)")
        design = np.column_stack([np.ones(len(X)), X])
        beta, *_ = np.linalg.lstsq(design, y, rcond=None)
        penalty = np.eye(design.shape[1]) * self.l2
        penalty[0, 0] = 0.0
        for iteration in range(1, self.max_iter + 1):
            pred = design @ beta
            residual = y - pred
            weights = np.where(residual >= 0, self.tau, 1.0 - self.tau)
            weights = np.maximum(weights, 1e-6)
            weighted = design * weights[:, None]
            lhs = design.T @ weighted + penalty
            rhs = weighted.T @ y
            try:
                new_beta = np.linalg.solve(lhs, rhs)
            except np.linalg.LinAlgError:
                new_beta = np.linalg.lstsq(lhs, rhs, rcond=None)[0]
            delta = float(np.max(np.abs(new_beta - beta)))
            beta = new_beta
            if delta < self.tol:
                self.converged_ = True
                self.n_iter_ = iteration
                break
        else:
            self.n_iter_ = self.max_iter
        self.intercept_ = float(beta[0])
        self.coefficients_ = beta[1:]
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.coefficients_ is None:
            raise RuntimeError("model is not fitted")
        return np.asarray(X, dtype=float) @ self.coefficients_ + self.intercept_

    def to_dict(self) -> dict:
        if self.coefficients_ is None:
            raise RuntimeError("model is not fitted")
        return {
            "type": "quantile_irls",
            "tau": self.tau,
            "l2": self.l2,
            "intercept": self.intercept_,
            "coefficients": self.coefficients_.tolist(),
            "approximation": "irls_asymmetric_weights",
            "converged": self.converged_,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QuantileLinear":
        model = cls(tau=float(data["tau"]), l2=float(data.get("l2", 0.01)))
        model.intercept_ = float(data["intercept"])
        model.coefficients_ = np.asarray(data["coefficients"], dtype=float)
        model.converged_ = bool(data.get("converged", True))
        return model
