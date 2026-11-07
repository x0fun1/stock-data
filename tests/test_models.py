from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_research.contracts import ModelArtifact, Target, ValidationEvidence, ValidationState
from stock_research.models import (
    FeatureTransformer,
    LogisticL2,
    PlattCalibrator,
    QuantileLinear,
    BaseRate,
)
from stock_research.models.registry import ModelRegistry, predict_proba
from stock_research.storage import Catalog


def frame(values, missing=None, dates=None):
    data = {"f__x": values, "f__z": [v * 2 for v in values], "m__x": [0] * len(values), "m__z": [0] * len(values)}
    if missing is not None:
        data["m__x"] = missing
    if dates is not None:
        data["cutoff_date"] = dates
    return pd.DataFrame(data)


def test_transformer_fits_on_training_only_and_handles_missing():
    train = frame([1.0, 2.0, 3.0, 4.0, 100.0], missing=[0, 0, 1, 0, 0], dates=["2024-01-01"] * 5)
    transformer = FeatureTransformer().fit(train, ["x", "z"], winsor_quantiles=(0.0, 1.0), clip=3.0)
    params = transformer.to_dict()
    X_train = transformer.transform(train)
    assert X_train.shape[0] == 5
    future = frame([1.0, 2.0, 3.0, 4.0, 100.0, 5000.0], missing=[0, 0, 1, 0, 0, 0], dates=["2024-02-01"] * 6)
    transformer.transform(future)
    assert transformer.to_dict() == params  # transform does not refit
    assert "miss__x" in transformer.output_names


def test_transformer_drops_constant_column():
    train = pd.DataFrame({"f__x": [1.0, 1.0, 1.0], "f__z": [1.0, 2.0, 3.0], "m__x": [0, 0, 0], "m__z": [0, 0, 0]})
    transformer = FeatureTransformer().fit(train, ["x", "z"], winsor_quantiles=(0.0, 1.0))
    assert "x" not in transformer.output_names  # constant x is dropped
    assert "z" in transformer.output_names


def test_logistic_recovers_direction_and_is_probabilistic():
    rng = np.random.default_rng(5)
    X = rng.normal(size=(1500, 2))
    y = (rng.random(1500) < 1 / (1 + np.exp(-(1.2 * X[:, 0] - 0.8 * X[:, 1])))).astype(float)
    model = LogisticL2(l2=0.1).fit(X, y)
    assert model.coefficients_[0] > 0
    assert model.coefficients_[1] < 0
    p = model.predict_proba(X)
    assert np.all((p > 0) & (p < 1))


def test_quantile_median_is_close_to_linear_median():
    rng = np.random.default_rng(6)
    x = rng.normal(size=3000)
    y = 2.0 * x + 1.0 + rng.normal(scale=0.2, size=3000)
    model = QuantileLinear(tau=0.5).fit(x.reshape(-1, 1), y)
    assert model.intercept_ == pytest.approx(1.0, abs=0.05)
    assert model.coefficients_[0] == pytest.approx(2.0, abs=0.05)


def test_platt_calibration_changes_scale():
    p = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
    y = np.array([0, 0, 1, 1, 1, 1])
    cal = PlattCalibrator().fit(p, y)
    out = cal.predict(p)
    assert np.all((out > 0) & (out < 1))
    assert out[0] < out[-1]


def test_base_rate():
    assert BaseRate.fit(np.array([0, 0, 1, 1, 1])).rate == pytest.approx(0.6)


def test_registry_activation_requires_verified_evidence(tmp_path):
    registry = ModelRegistry(tmp_path)
    artifact = ModelArtifact(
        model_id="m-test",
        market="US",
        horizon=5,
        target=Target.ABSOLUTE,
        status="candidate",
        factor_names=["x"],
        transformer={"features": [{"name": "x", "low": -10, "high": 10, "median": 0, "scale": 1, "indicator": False}], "clip": 3, "winsor_low": 0.01, "winsor_high": 0.99, "max_missing_rate": 0.95},
        coefficients=[0.5],
        intercept=0.0,
        baseline_rate=0.5,
        dataset_id="d",
        dataset_version="v",
    )
    registry.save(artifact)
    unverified = ValidationEvidence("ev-1", "m-test", "US", 5, "absolute", ValidationState.UNVERIFIED, 10, 10, None, None)
    with pytest.raises(Exception):
        registry.activate("m-test", unverified)
    verified = ValidationEvidence("ev-2", "m-test", "US", 5, "absolute", ValidationState.VERIFIED, 10, 10, None, None)
    registry.save_evidence(verified)
    active = registry.activate("m-test", verified)
    assert active.status == "active"
    p = predict_proba(active, frame([1.0]))
    assert 0 <= p[0] <= 1
