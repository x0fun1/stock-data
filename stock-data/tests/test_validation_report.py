from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stock_research.contracts import (
    CapabilityDecision,
    CapabilityState,
    DataLevel,
    Market,
    QuantitativeResult,
    ReportBundle,
    ResearchRequest,
    RunManifest,
    SecurityIdentity,
    Target,
    ValidationEvidence,
    ValidationState,
)
from stock_research.publication.capabilities import decide
from stock_research.reporting.report import render_report
from stock_research.validation import (
    auc,
    brier,
    classify_evidence,
    date_block_bootstrap,
    evaluate_binary,
    log_loss,
    make_evidence,
)


def test_basic_metrics():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.6, 0.9])
    assert brier(y, p) == pytest.approx(0.085)
    assert log_loss(y, p) == pytest.approx(-np.mean(np.log([0.9, 0.6, 0.6, 0.9])))
    assert auc(y, p) == pytest.approx(1.0)
    assert auc(np.array([0, 0]), np.array([0.2, 0.8])) is None


def test_date_block_bootstrap_reports_interval():
    dates = pd.date_range("2024-01-01", periods=40).date
    values = np.linspace(0.1, 0.2, 40)
    result = date_block_bootstrap(dates, values, n_resamples=100, block_days=5, seed=1)
    assert result["low"] <= result["mean"] <= result["high"]
    assert result["n_dates"] == 40


def test_evaluate_binary_and_classification():
    rng = np.random.default_rng(9)
    rows = []
    for day in pd.date_range("2024-01-01", periods=120).date:
        for _ in range(3):
            y = int(rng.random() < 0.55)
            p = float(np.clip(0.55 + 0.2 * (y - 0.5) + rng.normal(0, 0.08), 0.01, 0.99))
            rows.append({"cutoff_date": day, "label": y, "p": p, "base_p": 0.55, "security_id": "X"})
    evaluation = evaluate_binary(pd.DataFrame(rows), bootstrap_resamples=100)
    assert evaluation["sample_count"] == 360
    assert evaluation["independent_dates"] == 120
    assert evaluation["skill"]["ci"] is not None
    policy = {"min_samples": 200, "min_independent_dates": 60, "min_brier_skill_ci_low": 0.0, "calibration_slope": [0.2, 5.0], "max_ece": 0.2}
    status, reason = classify_evidence(evaluation, policy)
    assert status in {ValidationState.VERIFIED, ValidationState.UNVERIFIED, ValidationState.FAILED}
    assert reason


def test_decision_is_target_local():
    decision = decide("US", 5, "absolute", data_ready=False, data_reason="no_price", evidence=None, artifact=None, policy={})
    assert decision.state is CapabilityState.INSUFFICIENT_DATA
    decision2 = decide("US", 5, "absolute", data_ready=True, data_reason="", evidence=None, artifact=None, policy={})
    assert decision2.state is CapabilityState.NOT_EXECUTED
    assert decision2.data_level is DataLevel.DESCRIPTIVE


def _bundle(result_state=CapabilityState.UNVERIFIED, probability=None):
    request = ResearchRequest(market="US", tickers=["X"], benchmark="SPY", cutoff=datetime(2024, 6, 3, 21, tzinfo=timezone.utc), horizons=(1, 5))
    security = SecurityIdentity("US:XNAS:X", "X", "US", "XNAS", "common_stock", "USD", date(2000, 1, 1))
    results = [
        QuantitativeResult("US", "US:XNAS:X", 5, Target.ABSOLUTE, result_state, DataLevel.RESEARCH, probability=probability),
        QuantitativeResult("US", "US:XNAS:X", 5, Target.EXCESS, CapabilityState.NOT_EXECUTED, DataLevel.DESCRIPTIVE),
    ]
    decisions = [
        CapabilityDecision(Market.US, 5, Target.ABSOLUTE, result_state, DataLevel.RESEARCH, "reason", security_id="US:XNAS:X", details={"independent_dates": 80}),
        CapabilityDecision(Market.US, 5, Target.EXCESS, CapabilityState.NOT_EXECUTED, DataLevel.DESCRIPTIVE, "no_benchmark", security_id="US:XNAS:X"),
    ]
    manifest = RunManifest("run-test", datetime(2024, 6, 4, tzinfo=timezone.utc), {}, "p", "c", {"datasets": {}, "policy_hash": "h"})
    return ReportBundle(request, [security], results, decisions, [], {"US:XNAS:X": {}}, manifest, ["missing financial facts"])


def test_report_never_fabricates_probability():
    markdown = render_report(_bundle())
    assert "不发布" in markdown
    assert "50.0%" not in markdown
    assert "未证实优于基准" in markdown


def test_report_prints_verified_probability_only_when_verified():
    markdown = render_report(_bundle(CapabilityState.VERIFIED, 0.62))
    assert "62.0%" in markdown
    assert "已验证" in markdown
    assert "买卖" not in markdown or "不提供买卖" in markdown
