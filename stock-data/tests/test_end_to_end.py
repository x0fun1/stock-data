from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_days, price_rows, security_rows, write_runtime
from stock_research.contracts import ResearchRequest
from stock_research.control.orchestration import run_research
from stock_research.control.studies import build_dataset_from_store, protocol_from_dict
from stock_research.models.registry import ModelRegistry
from stock_research.models.training import train_binary_model
from stock_research.storage import CuratedStore


def runtime_with_prices(tmp_path: Path):
    days = make_days(count=320)
    prices = pd.DataFrame(
        price_rows("US:XNAS:X", days, seed=21)
        + price_rows("US:XNAS:Y", days, start_price=50, seed=22)
        + price_rows("US:XNAS:SPY", days, start_price=400, vol=0.008, seed=23)
    )
    return write_runtime(tmp_path, prices, security_rows("X", "Y", "SPY")), days


def test_research_degrades_to_description_without_models(tmp_path):
    store, days = runtime_with_prices(tmp_path)
    request = ResearchRequest(market="US", tickers=["X"], benchmark="SPY", horizons=(1, 5))
    bundle, markdown = run_research(request, runtime_root=tmp_path)
    assert "## 量化结果" in markdown
    assert "不发布" in markdown
    probability_section = markdown.split("## 量化结果")[1].split("## 多因子分析")[0]
    assert "50.0%" not in probability_section
    assert len(bundle.results) == 4
    assert all(r.probability is None for r in bundle.results)
    assert any("缺少数据集" in x for x in bundle.limitations)


def test_current_report_does_not_admit_an_explicitly_late_close(tmp_path):
    store, days = runtime_with_prices(tmp_path)
    frame = store.load("prices")
    cutoff = days[-1]
    late = (frame["security_id"] == "US:XNAS:X") & (frame["date"] == cutoff.isoformat())
    frame.loc[late, "available_at"] = (datetime.combine(cutoff, datetime.max.time(), tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
    store.save("prices", frame, {"scope": "synthetic_late_quote_behavior"})
    request = ResearchRequest(market="US", tickers=["X"], benchmark="SPY",
        cutoff=datetime.combine(cutoff, datetime.max.time(), tzinfo=timezone.utc), horizons=(1,), quant_baseline=False)
    bundle, _ = run_research(request, runtime_root=tmp_path)
    assert all(r.probability is None and r.reason == "no_price_at_cutoff" for r in bundle.results)
    assert any(e["ticker"] == "X" and e["reason"] == "no_available_completed_price_at_cutoff"
        for e in bundle.multifactor_snapshot["excluded"])


def test_active_verified_model_is_used_for_report(tmp_path):
    store, days = runtime_with_prices(tmp_path)
    proto = protocol_from_dict(
        {
            "name": "e2e",
            "market": "US",
            "horizons": [5],
            "targets": ["absolute"],
            "factors": ["mom_60", "rv_20"],
            "train_start": "2024-01-02",
            "train_end": "2024-06-30",
            "dev_start": "2024-07-01",
            "dev_end": "2024-07-31",
            "cal_start": "2024-08-01",
            "cal_end": "2024-08-31",
            "test_start": "2024-09-01",
            "test_end": "2024-12-31",
        }
    )
    built = build_dataset_from_store(
        "US", ["X", "Y"], "SPY", "2024-01-02", "2024-12-31", [5], ["absolute"], ["mom_60", "rv_20"], proto, tmp_path
    )
    frame = store.load("features_labels", built["version"])
    subset = frame[(frame["horizon"] == 5) & (frame["target"] == "absolute")]
    registry = ModelRegistry(tmp_path, store.catalog)
    trained = train_binary_model(
        subset,
        "US",
        5,
        "absolute",
        ["mom_60", "rv_20"],
        proto,
        {
            "min_samples": 20,
            "min_independent_dates": 20,
            "min_brier_skill_ci_low": -10.0,
            "calibration_slope": [0.0, 10.0],
            "max_ece": 1.0,
            "bootstrap_resamples": 50,
            "block_days": 5,
        },
        registry,
        "features_labels",
        built["version"],
    )
    assert trained["evidence"].status.value == "verified"
    registry.activate(trained["artifact"].model_id, trained["evidence"])
    request = ResearchRequest(
        market="US",
        tickers=["X"],
        benchmark="SPY",
        cutoff=datetime(2024, 12, 20, 23, tzinfo=timezone.utc),
        horizons=(5,),
        targets=("absolute", "excess"),
    )
    bundle, markdown = run_research(request, runtime_root=tmp_path)
    verified = [r for r in bundle.results if r.state.value == "verified"]
    assert len(verified) == 1
    assert verified[0].probability is not None
    assert "已验证" in markdown
    assert f"{verified[0].probability * 100:.1f}%" in markdown


def test_cli_doctor_and_research(tmp_path):
    store, days = runtime_with_prices(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "research.py"
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"), "STOCK_RESEARCH_RUNTIME": str(tmp_path)}
    doctor = subprocess.run([sys.executable, str(script), "doctor"], capture_output=True, text=True, encoding="utf-8", env=env)
    assert doctor.returncode == 0, doctor.stderr
    assert "policy_hash" in doctor.stdout
    research = subprocess.run(
        [sys.executable, str(script), "research", "--ticker", "X", "--benchmark", "SPY", "--horizons", "5", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert research.returncode == 0, research.stderr
    assert "# 研究报告" in research.stdout
    assert "不发布" in research.stdout


def test_hk_research_uses_observed_sessions_and_degrades_without_models(tmp_path):
    days = make_days(count=260)
    prices = pd.DataFrame(
        price_rows("HK:XHKG:0700", days, start_price=300, vol=0.014, seed=31, market="HK")
        + price_rows("HK:XHKG:2800", days, start_price=20, vol=0.009, seed=32, market="HK")
    )
    store = write_runtime(
        tmp_path,
        prices,
        [
            {"security_id": "HK:XHKG:0700", "ticker": "0700", "market": "HK", "exchange": "XHKG", "security_type": "common_stock", "currency": "HKD", "valid_from": "2000-01-01", "valid_to": None, "name": "0700", "industry": "technology", "source_id": "test"},
            {"security_id": "HK:XHKG:2800", "ticker": "2800", "market": "HK", "exchange": "XHKG", "security_type": "etf", "currency": "HKD", "valid_from": "2000-01-01", "valid_to": None, "name": "2800", "industry": "", "source_id": "test"},
        ],
    )
    request = ResearchRequest(market="HK", tickers=["0700"], benchmark=None, horizons=(5,), targets=("absolute",))
    bundle, markdown = run_research(request, runtime_root=tmp_path)
    assert "## 量化结果" in markdown
    assert "0700" in markdown
    assert bundle.run_manifest.snapshot["calendar"].startswith("observed_benchmark_sessions")
    assert all(r.probability is None for r in bundle.results)


def test_quantile_artifact_trains_activates_and_reports_interval(tmp_path):
    store, days = runtime_with_prices(tmp_path)
    proto = protocol_from_dict(
        {
            "name": "q-e2e",
            "market": "US",
            "horizons": [5],
            "targets": ["quantile"],
            "factors": ["mom_60", "rv_20"],
            "train_start": "2024-01-02",
            "train_end": "2024-06-30",
            "dev_start": "2024-07-01",
            "dev_end": "2024-07-31",
            "cal_start": "2024-08-01",
            "cal_end": "2024-08-31",
            "test_start": "2024-09-01",
            "test_end": "2024-12-31",
        }
    )
    built = build_dataset_from_store(
        "US", ["X", "Y"], "SPY", "2024-01-02", "2024-12-31", [5], ["quantile"], ["mom_60", "rv_20"], proto, tmp_path
    )
    frame = store.load("features_labels", built["version"])
    subset = frame[(frame["horizon"] == 5) & (frame["target"] == "quantile")]
    from stock_research.models.quantiles import train_quantile_artifact

    registry = ModelRegistry(tmp_path, store.catalog)
    trained = train_quantile_artifact(
        subset,
        "US",
        5,
        ["mom_60", "rv_20"],
        proto,
        {
            "min_samples": 20,
            "min_independent_dates": 20,
            "min_pinball_skill_ci_low": -10.0,
            "coverage_range": [0.0, 1.0],
            "bootstrap_resamples": 50,
            "block_days": 5,
            "winsor_quantiles": [0.01, 0.99],
            "mad_clip": 3.0,
        },
        registry,
        "features_labels",
        built["version"],
    )
    assert trained["evidence"].status.value == "verified"
    q = trained["artifact"].quantiles
    assert set(q) == {"0.1", "0.5", "0.9"}
    registry.activate_quantile(trained["artifact"].artifact_id, trained["evidence"])
    request = ResearchRequest(
        market="US",
        tickers=["X"],
        benchmark="SPY",
        cutoff=datetime(2024, 12, 20, 23, tzinfo=timezone.utc),
        horizons=(5,),
        targets=("quantile",),
    )
    bundle, markdown = run_research(request, runtime_root=tmp_path)
    quantile_results = [r for r in bundle.results if r.target.value == "quantile"]
    assert len(quantile_results) == 1
    assert quantile_results[0].state.value == "verified"
    qd = quantile_results[0].quantiles
    assert qd["q10"] <= qd["q50"] <= qd["q90"]
    assert "样本不足" not in markdown.split("## 量化结果")[1].split("## 多因子分析")[0].split("## 新闻")[0]
