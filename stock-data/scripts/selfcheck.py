"""Offline self-check battery for the stock-data quant research skill.

Covers the regression cases fixed in the 2026-10 quality pass and runs entirely
offline with synthetic data (no network, no yfinance dependency):

- F01/F02  causal walk-forward ledger: current probability is not clobbered by the
           ledger loop; per-origin calibration boundary fields; prefix invariance
           under tail truncation; audit accepts fixed rows and rejects retrofits.
- F03      credential redaction completeness (headers, cookies, schemes, case).
- F04      delivery text validation: comments cannot satisfy fields, numbered/quoted
           renderings are folded, conflicting values are rejected.
- F05      retained-artifact binding: a report/summary changed after validation is
           refused before any deletion.
- F07      malformed nested market fields degrade instead of raising.
- F08      Yahoo preflight before dependency/route selection; fallback counts each
           gateway call at the lowest level and propagates budget failures.
- F09      block-bootstrap estimability: insufficient independent blocks and
           zero-width resamples are marked, not reported as certainty.
- F10      ensemble check naming: retrospective scope is explicit.
- F11      legacy option expiries accept Unix seconds: direct gateway validation
           and the fallback envelope with an explicit expiry.
- F12      PathLike cache_dir is normalized at the provider entry; non-path
           values stay rejected.
- F13      horizon-scaled OOS support thresholds (one-year default window) and the
           matching evidence grades.
- F14      weighted confidence composite bands: fully validated evidence reaches the
           high band; contradicted skill is capped below medium.
- F15      one-year window defaults: the collector window helper and the recalibrated
           quant/ML sample floors keep both paths feasible at ~252 sessions.
- F18      orphan scratch sweep: crash-leftover temp files/directories are removed,
           lock markers and user files are not, and a dry run reports without deleting.
- F19      retention: only completed run directories past the window expire, a run with
           unknown files or an unfinished receipt is skipped, stale cache files are pruned.

Usage: python stock-data/scripts/selfcheck.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import shutil
import sys
import tempfile
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

FAILURES: list[str] = []


def case(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a self-check reports whatever failed
        FAILURES.append(name)
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {name}")


def synth_bars(n: int = 640, seed: int = 11, start: dt.date = dt.date(2023, 1, 2)) -> list[dict]:
    rng = random.Random(seed)
    price = 100.0
    day = start
    bars = []
    while len(bars) < n:
        if day.weekday() < 5:
            price *= math.exp(rng.gauss(0.0005, 0.018))
            bars.append({
                "date": day.isoformat(), "open": round(price * 0.997, 4), "high": round(price * 1.012, 4),
                "low": round(price * 0.988, 4), "close": round(price, 4), "volume": 1_000_000 + len(bars),
            })
        day += dt.timedelta(days=1)
    return bars


def synth_market(bars: list[dict]) -> dict:
    return {
        "data": {"currency": "USD", "exchange": "US", "adjustment": "unadjusted", "bars": bars,
                 "source_symbol": "SYN", "fetched_at_utc": "2026-01-02T00:00:00Z",
                 "source_timestamp": "2026-01-02T00:00:00Z"},
        "_data_validation": {"forecast_eligible": True},
    }


def ml_snapshot_stub(bars: list[dict]) -> tuple[dict, dict]:
    manifest = {"ticker": "SYN", "horizon": "5D", "snapshot_id": f"synth_{len(bars)}", "market": "US"}
    return manifest, synth_market(bars)


# ---------------------------------------------------------------- F01 / F02
def _run_ml(bars: list[dict]):
    from quant_research.researchers import ml

    manifest, market = ml_snapshot_stub(bars)
    original_loader = ml.load_snapshot
    original_fit, original_platt = ml._fit, ml._fit_platt
    fits, platt = [], []

    def wrapped_fit(rows):
        model = original_fit(rows)
        fits.append(model)
        return model

    def wrapped_platt(scores, targets):
        result = original_platt(scores, targets)
        platt.append((list(scores), list(targets), result))
        return result

    ml.load_snapshot = lambda _dir: (manifest, market)
    ml._fit = wrapped_fit
    ml._fit_platt = wrapped_platt
    try:
        result = ml.run(Path("unused"))
    finally:
        ml.load_snapshot = original_loader
        ml._fit, ml._fit_platt = original_fit, original_platt
    return result, fits, platt, bars


def check_f01_current_probability_not_clobbered() -> None:
    bars = synth_bars()
    result, fits, platt, _ = _run_ml(bars)
    assert result["status"] in {"success", "partial"}, result["status"]
    rows = result["validation"]["oos_forecasts"]
    assert rows and fits and platt
    from quant_research.researchers.ml import logistic

    feature_vector = None
    from quant_research.researchers import ml

    features = ml._features(bars)
    feature_vector = tuple(features[-1])
    frozen = platt[0][2]
    if frozen:
        # The frozen Platt mapping is applied to the current refit model's logit; the
        # check proves the current probability is not clobbered by any ledger row.
        expected = logistic(frozen[1] + frozen[0] * fits[-1].score(feature_vector))
    else:
        expected = fits[-1].predict(feature_vector)
    assert abs(result["prob_up"] - expected) < 1e-9, (result["prob_up"], expected)
    assert abs((result["prob_up"] or -1) - rows[-1]["probability"]) > 1e-12, "prob_up equals the last ledger row"


def check_f02_causal_calibration_fields() -> None:
    bars = synth_bars()
    result, _, _, _ = _run_ml(bars)
    index = {bar["date"]: i for i, bar in enumerate(bars)}
    rows = result["validation"]["oos_forecasts"]
    applied = [row for row in rows if row.get("calibration_applied")]
    assert applied, "no calibrated rows in synthetic ledger"
    for row in applied:
        assert index[row["calibration_label_end_session"]] < index[row["decision_session"]]
        assert isinstance(row["calibration_fit_observations"], int) and row["calibration_fit_observations"] >= 40
    for row in rows:
        if not row.get("calibration_applied"):
            assert row["probability"] == row["raw_probability"]
    ledger = result["validation"]["historical_asof_ledger"]
    assert ledger["calibrated_prediction_count"] + ledger["uncalibrated_prediction_count"] == len(rows)


def check_f02_prefix_invariance() -> None:
    from quant_research.researchers import ml

    bars = synth_bars()
    result, _, _, _ = _run_ml(bars)
    cut = 560
    trunc_bars = bars[: cut + 1]
    manifest, market = ml_snapshot_stub(trunc_bars)
    original = ml.load_snapshot
    ml.load_snapshot = lambda _dir: (manifest, market)
    try:
        truncated = ml.run(Path("unused"))
    finally:
        ml.load_snapshot = original
    by_date = {row["decision_session"]: row for row in truncated["validation"]["oos_forecasts"]}
    checked = 0
    for row in result["validation"]["oos_forecasts"]:
        other = by_date.get(row["decision_session"])
        if other is None:
            continue
        checked += 1
        for key in ("probability", "raw_probability", "calibration_applied", "calibration_label_end_session",
                    "calibration_fit_observations"):
            assert row[key] == other[key], (row["decision_session"], key, row[key], other[key])
    assert checked > 50, checked


def check_f02_audit_accepts_and_rejects() -> None:
    import copy

    from quant_research.validation.probability_quality import audit_oos_result

    bars = synth_bars()
    result, _, _, _ = _run_ml(bars)
    manifest, _ = ml_snapshot_stub(bars)
    ok = audit_oos_result(result, manifest, bars)
    assert ok["status"] == "validated" and ok["rejected_samples"] == 0, ok
    bad = copy.deepcopy(result)
    row = bad["validation"]["oos_forecasts"][len(bad["validation"]["oos_forecasts"]) // 2]
    if not row.get("calibration_applied"):
        row = next(item for item in bad["validation"]["oos_forecasts"] if item.get("calibration_applied"))
    row["probability"] = min(0.999, row["raw_probability"] + 0.25)
    verdict = audit_oos_result(bad, manifest, bars)
    assert verdict["status"] != "validated" and verdict["rejected_samples"] >= 1
    assert "calibrated_probability_mismatch" in verdict["rejection_reasons"], verdict
    row.pop("calibration_applied", None)
    row.pop("calibration_label_end_session", None)
    row.pop("calibration_fit_observations", None)
    verdict = audit_oos_result(bad, manifest, bars)
    assert "calibrated_probability_without_recorded_origin_boundary" in verdict["rejection_reasons"], verdict


# ---------------------------------------------------------------- F03
def check_f03_redaction() -> None:
    from quant_research.security import redact_text, safe_url

    leaks = [
        ("Authorization: Bearer FAKE-TOKEN-XYZ123\nnext stays", ["FAKE-TOKEN-XYZ123"]),
        ("authorization=Basic dXNlcjpwYXNzRkFLRQ==", ["dXNlcjpwYXNzRkFLRQ"]),
        ('{"Authorization": "Bearer eyJabc123.def456", "Cookie": "sid=FAKESID1; token=FAKETOK2"}',
         ["eyJabc123.def456", "FAKESID1", "FAKETOK2"]),
        ("Cookie: a=FAKE1; b=FAKE2; c=FAKE3", ["FAKE1", "FAKE2", "FAKE3"]),
        ("Set-Cookie: session=FAKESESSION; HttpOnly", ["FAKESESSION"]),
        ("COOKIE: FAKEUPPER", ["FAKEUPPER"]),
        ("bearer eyJhbGciOi.FAKE-SIG123456", ["eyJhbGciOi.FAKE-SIG123456"]),
        ("x-api-key: FAKEKEY123456", ["FAKEKEY123456"]),
        ("Authorization: Bearer abc123\n  continuation-FAKE-CONT==\nnext stays", ["FAKE-CONT"]),
    ]
    for text, must_vanish in leaks:
        output = redact_text(text)
        for token in must_vanish:
            assert token not in output, (token, output)
    assert "apses" not in redact_text("authorization without a colon is fine")
    assert "[redacted]" not in redact_text("basic understanding of markets improves")
    assert "[redacted]" not in redact_text("bearer of good news for equities")
    url = safe_url("https://api.example.com/v1?sessionid=FAKESESS&foo=bar&x-amz-signature=FAKESIG&api_key=FAKEKEY")
    assert url and "FAKESESS" not in url and "FAKESIG" not in url and "FAKEKEY" not in url and "foo=bar" in url


# ---------------------------------------------------------------- F04 / F05
def check_f04_response_text_validation() -> None:
    from quant_research import delivery

    fake_required = {"identity": "- 标的：SYN；市场：US；预测窗口：5D（交易 session）；分析截至：2026-01-02T00:00:00Z",
                     "prob": "- 上涨概率 P(up)：61.4%", "status": "- 报告状态：pass"}
    original = delivery.required_response_lines
    delivery.required_response_lines = lambda result: dict(fake_required)
    try:
        ok = "\n".join(fake_required.values())
        delivery.validate_response_text({}, ok)
        numbered_ok = ok.replace(fake_required["prob"], f"1. {fake_required['prob'][2:]}")
        delivery.validate_response_text({}, numbered_ok)
        quoted_ok = ok.replace(fake_required["prob"], f"> {fake_required['prob'][2:]}")
        delivery.validate_response_text({}, quoted_ok)

        hidden = f"<!-- {fake_required['prob']} -->\n4. 上涨概率 P(up)：99.9%\n" + "\n".join(
            value for key, value in fake_required.items() if key != "prob")
        for bad_text, label in ((hidden, "comment-hidden"), (ok + "\n5. 上涨概率 P(up)：99.9%\n", "numbered-conflict")):
            try:
                delivery.validate_response_text({}, bad_text)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{label} was not rejected")
    finally:
        delivery.required_response_lines = original


def check_f05_retained_binding() -> None:
    from quant_research import cleanup

    original_md, original_fr, original_vr = cleanup.render_markdown, cleanup.render_final_response, cleanup.validate_response_text
    cleanup.render_markdown = lambda report: "MD:" + json.dumps(report, sort_keys=True)
    cleanup.render_final_response = lambda report, summary=None: "FR:" + json.dumps(summary, sort_keys=True)
    cleanup.validate_response_text = lambda result, text: None
    work = Path(tempfile.mkdtemp(prefix="selfcheck_f05_"))
    try:
        directory = work / "analysis"
        directory.mkdir()
        report = {"a": 1}
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (directory / "report.md").write_text(cleanup.render_markdown(report), encoding="utf-8")
        (directory / "agent_summary.json").write_text(json.dumps({"s": 1}), encoding="utf-8")
        (directory / "final_response.md").write_text(cleanup.render_final_response(report, summary={"s": 1}), encoding="utf-8")
        cleanup._verify_retained_artifacts(directory)
        (directory / "report.md").write_text(cleanup.render_markdown(report) + "tampered", encoding="utf-8")
        try:
            cleanup._verify_retained_artifacts(directory)
        except cleanup.CleanupError:
            pass
        else:
            raise AssertionError("rewritten report.md was not refused")
        (directory / "report.md").write_text(cleanup.render_markdown(report), encoding="utf-8")
        (directory / "final_response.md").write_text("tampered", encoding="utf-8")
        try:
            cleanup._verify_retained_artifacts(directory)
        except cleanup.CleanupError:
            pass
        else:
            raise AssertionError("rewritten final_response.md was not refused")
    finally:
        cleanup.render_markdown, cleanup.render_final_response, cleanup.validate_response_text = original_md, original_fr, original_vr
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- F07
def check_f07_malformed_fields() -> None:
    import copy
    from quant_research.data_checks import check_market

    bars = synth_bars(300)
    request = {"ticker": "SYN", "market": "US", "horizon": "5D", "asset_type": "stock", "intent": "forecast",
               "asof": "2026-01-02T00:00:00Z"}
    base = synth_market(bars)
    base["adjustment_evidence"] = {"source": "s", "actions": [], "checked_through": bars[-1]["date"], "price_basis": "unadjusted"}
    captured = bars[-1]["date"] + "T00:00:00Z"

    def run_with(mutate):
        block = copy.deepcopy(base)
        mutate(block)
        return check_market(block, request, captured_at=captured, allow_library_calendar=False)

    run_with(lambda b: None)
    for mutate, expected in (
        (lambda b: b.update(adjustment=[]), "ADJUSTMENT_UNKNOWN"),
        (lambda b: b.update(adjustment={"a": 1}), "ADJUSTMENT_UNKNOWN"),
        (lambda b: b.update(data=[1, 2]), "INSUFFICIENT_OHLCV"),
        (lambda b: b.update(data={"bars": {"a": 1}}), "INSUFFICIENT_OHLCV"),
        (lambda b: b.update(data={"bars": ["x", "y"]}), "INSUFFICIENT_OHLCV"),
    ):
        gate = run_with(mutate)
        codes = gate.get("reason_codes", [])
        assert expected in codes, (expected, codes)
    bad_request = {"ticker": "SYN", "market": "US", "horizon": "5D", "asset_type": [], "intent": "forecast",
                   "asof": "2026-01-02T00:00:00Z"}
    gate = check_market(base, bad_request, captured_at=captured, allow_library_calendar=False)
    assert "ASSET_TYPE_UNVERIFIED" in gate.get("reason_codes", [])


# ---------------------------------------------------------------- F08
def check_f08_preflight_and_counting() -> None:
    os.environ.pop("STOCK_DATA_YAHOO_CACHE_DIR", None)
    from yfinance_provider import GatewayError, YahooProvider
    import yahoo_compat_fallback as fb

    provider = YahooProvider(cache_dir=tempfile.mkdtemp())
    sentinel = {"sentinel": True}
    provider._call_yfinance = lambda name, **params: sentinel

    def expect_invalid(fn):
        try:
            fn()
        except GatewayError as exc:
            assert exc.code == "InvalidParameters", exc.code
        else:
            raise AssertionError("expected InvalidParameters")

    expect_invalid(lambda: provider.call("yahoo_history_batch", symbols=[f"S{i}" for i in range(11)], period="1y"))
    expect_invalid(lambda: provider.call("yahoo_history_batch", symbols=["AAPL", "AAPL"], period="1y"))
    expect_invalid(lambda: provider.call("yahoo_history_batch", symbols=["AAPL", "0700.HK"], period="1y"))
    expect_invalid(lambda: provider.call("yahoo_history_batch", symbols=[123], period="1y"))
    expect_invalid(lambda: provider.call("yahoo_history", period="1mo"))
    expect_invalid(lambda: YahooProvider().call("yahoo_quote", symbol="AAPL"))
    assert provider.call("yahoo_history_batch", symbols=["AAPL", "MSFT"], period="1y") is sentinel

    class FakeGateway(types.ModuleType):
        def __init__(self):
            super().__init__("global_stock_data")
            self.hits = 0
            self.FUNCTIONS = {"stock_kline_yahoo": self.kline, "institutional_holders": self.holders}

        def kline(self, symbol, **kwargs):
            self.hits += 1
            return {"symbol": symbol}

        def holders(self, **kwargs):
            self.hits += 1
            return {"ok": True}

        def options_chain(self, symbol, **kwargs):
            self.hits += 1
            return {"expiration_dates": [int(dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).timestamp())]}

    class FakePolicy:
        def __init__(self, limit=99):
            self.limit = limit
            self.calls = 0
            self.blocked = None

        def reserve(self):
            self.calls += 1
            if self.calls > self.limit:
                raise GatewayError("BudgetExceeded", "budget exhausted")

    real_gsd, real_norm = sys.modules.get("global_stock_data"), sys.modules.get("yfinance_normalize")
    fake_gsd = FakeGateway()
    fake_norm = types.ModuleType("yfinance_normalize")
    fake_norm.normalize_history = lambda raw: {"normalized": raw}
    sys.modules["global_stock_data"], sys.modules["yfinance_normalize"] = fake_gsd, fake_norm
    try:
        policy = FakePolicy()
        fb.fallback("yahoo_history_batch", {"symbols": ["A", "B", "C"]}, {"category": "DataUnavailable"}, policy=policy)
        assert policy.calls == 3, policy.calls
        policy = FakePolicy()
        fb.fallback("yahoo_history", {"symbol": "A"}, {"category": "DataUnavailable"}, policy=policy)
        assert policy.calls == 1
        policy = FakePolicy()
        fb.fallback("yahoo_options", {"symbol": "A", "expiration": "2026-01-02"}, {"category": "DataUnavailable"}, policy=policy)
        assert policy.calls == 2
        policy = FakePolicy(limit=2)
        try:
            fb.fallback("yahoo_history_batch", {"symbols": ["A", "B", "C"]}, {"category": "DataUnavailable"}, policy=policy)
        except GatewayError as exc:
            assert exc.code == "BudgetExceeded" and policy.calls == 3
        else:
            raise AssertionError("budget failure did not propagate")
    finally:
        if real_gsd is not None:
            sys.modules["global_stock_data"] = real_gsd
        else:
            del sys.modules["global_stock_data"]
        if real_norm is not None:
            sys.modules["yfinance_normalize"] = real_norm
        else:
            del sys.modules["yfinance_normalize"]


# ---------------------------------------------------------------- F09 / F10
def check_f09_bootstrap_estimability() -> None:
    from quant_research.validation.probability_quality import (
        MIN_BOOTSTRAP_INDEPENDENT_BLOCKS, _block_bootstrap_skill_interval)

    def rows(count: int, same: bool = False):
        rng = random.Random(5)
        output = []
        for i in range(count):
            probability = 0.5 if same else min(max(0.5 + 0.15 * math.sin(i / 3) + rng.uniform(-0.1, 0.1), 0.01), 0.99)
            output.append({"probability": probability, "target": int(i % 3 == 0), "baseline_probability": 0.5})
        return output

    assert _block_bootstrap_skill_interval(rows(15), horizon=5)["status"] == "insufficient_independent_blocks"
    assert _block_bootstrap_skill_interval(rows(12), horizon=20)["status"] == "insufficient_independent_blocks"
    estimated = _block_bootstrap_skill_interval(rows(200), horizon=5)
    assert estimated["status"] == "estimated" and estimated["independent_blocks"] >= MIN_BOOTSTRAP_INDEPENDENT_BLOCKS
    assert _block_bootstrap_skill_interval(rows(200, same=True), horizon=5)["status"] == "degenerate_zero_width_interval"


def check_f10_ensemble_scope() -> None:
    from quant_research.validation.probability_quality import _ensemble_calibration_quality

    rng = random.Random(7)
    start = dt.date(2024, 1, 1)
    predicted = []
    for i in range(240):
        decision = start + dt.timedelta(days=i)
        probability = min(max(0.5 + 0.1 * math.sin(i / 7) + rng.uniform(-0.15, 0.15), 0.02), 0.98)
        predicted.append({"probability": probability, "target": int(rng.random() < 0.55), "baseline_probability": 0.5,
                          "decision_session": decision.isoformat(), "label_end_session": (decision + dt.timedelta(days=5)).isoformat()})
    quality = _ensemble_calibration_quality(predicted, horizon=5)
    assert quality["status"] in {"improved_on_subsequent_ledger_block", "no_improvement_on_subsequent_ledger_block"}
    assert "evidence_scope" in quality and "not an independent confirmation" in quality["evidence_scope"]
    assert "validated_on_chronological_holdout" not in json.dumps(quality)


# ---------------------------------------------------------------- F11 / F12
def check_f11_options_expiry_unix_seconds() -> None:
    import importlib.util

    if importlib.util.find_spec("requests") is None:
        # The Yahoo session is stubbed below, so the legacy gateway only needs a
        # placeholder when requests is not installed.
        sys.modules["requests"] = types.ModuleType("requests")

    import global_stock_data as gsd
    import yahoo_compat_fallback as fb

    unix = int(dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc).timestamp())
    captured: dict = {}

    class StubSession:
        _crumb = "stub"

    class StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"optionChain": {"error": None, "result": [{
                "expirationDates": [unix],
                "options": [{"calls": [], "puts": []}],
                "quote": {"regularMarketPrice": 101.5},
            }]}}

    def stub_session():
        return StubSession()

    def stub_get(session, url, params=None, timeout=None):
        captured["params"] = dict(params or {})
        return StubResponse()

    real_session, real_get = gsd.get_yahoo_session, gsd._session_get
    gsd.get_yahoo_session, gsd._session_get = stub_session, stub_get
    try:
        chain = gsd.options_chain("AAPL", expiration=unix)
        assert captured["params"].get("date") == unix, captured
        assert chain["expiration_dates"] == [unix], chain

        for bad in (0, -1, True, 4102444801, "2026-11-20"):
            rejected = False
            try:
                gsd.options_chain("AAPL", expiration=bad)
            except ValueError:
                rejected = True
            assert rejected, f"expiration={bad!r} must still be rejected"

        envelope = fb.fallback("yahoo_options", {"symbol": "AAPL", "expiration": "2026-11-20"},
                               {"category": "DependencyUnavailable"}, policy=None)
        assert envelope["status"] == "partial", envelope
        assert envelope["fallback_used"] is True and envelope["fallback_route"] == "options_chain", envelope
        assert (envelope.get("data") or {}).get("expiration_dates") == [unix], envelope.get("data")
    finally:
        gsd.get_yahoo_session, gsd._session_get = real_session, real_get


def check_f12_cache_dir_pathlike() -> None:
    os.environ.pop("STOCK_DATA_YAHOO_CACHE_DIR", None)
    from yfinance_provider import GatewayError, YahooProvider

    sentinel = {"sentinel": True}
    provider = YahooProvider(cache_dir=Path(tempfile.mkdtemp()))
    assert isinstance(provider.cache_dir, str), type(provider.cache_dir)
    provider._call_yfinance = lambda name, **params: sentinel
    assert provider.call("yahoo_quote", symbol="AAPL") is sentinel

    non_path = YahooProvider(cache_dir=123)
    try:
        non_path._preflight("yahoo_quote", {"symbol": "AAPL"})
    except GatewayError as exc:
        assert exc.code == "InvalidParameters", exc.code
    else:
        raise AssertionError("non-path cache_dir must be rejected")


def check_f13_horizon_scaled_support() -> None:
    from quant_research.consensus import (
        _predictive_evidence_grade,
        oos_support_thresholds,
    )

    table = oos_support_thresholds(5)
    assert table == {"strong": 38, "acceptable": 20, "weak": 11}, table
    for horizon in (1, 2, 3, 5, 10, 20, 60):
        scaled = oos_support_thresholds(horizon)
        assert 0 < scaled["weak"] <= scaled["acceptable"] <= scaled["strong"], (horizon, scaled)

    def replay(**over):
        payload = {"non_overlapping_predicted_dates": 25, "brier_skill_vs_expanding_base_rate": 0.01}
        payload.update(over)
        return {"algorithm_policy_replay": payload}

    manifest = {"horizon": "5D"}
    assert _predictive_evidence_grade(replay(non_overlapping_predicted_dates=10), manifest) == "insufficient_oos_evidence"
    assert _predictive_evidence_grade(replay(), manifest) == "exploratory_positive_skill_unconfirmed"
    assert _predictive_evidence_grade(
        replay(non_overlapping_predicted_dates=40, skill_uncertainty={"ci_low": 0.001, "ci_high": 0.05}),
        manifest) == "positive_skill_with_estimated_interval_excluding_zero"
    assert _predictive_evidence_grade(
        replay(non_overlapping_predicted_dates=40, skill_uncertainty={"ci_low": -0.02, "ci_high": 0.05}),
        manifest) == "positive_skill_with_estimated_interval_including_zero"
    assert _predictive_evidence_grade(
        replay(non_overlapping_predicted_dates=40, brier_skill_vs_expanding_base_rate=-0.01,
               skill_uncertainty={"ci_low": -0.05, "ci_high": -0.001}),
        manifest) == "observed_skill_below_expanding_base_rate"


def check_f14_confidence_composite() -> None:
    from quant_research.consensus import _confidence_composite

    strong_path = {"oos_validation": {"status": "strong"}, "probability_calibration": "calibrated", "reason_codes": []}
    ok_path = {"oos_validation": {"status": "acceptable"}, "probability_calibration": "empirical_frequency", "reason_codes": []}
    weak_path = {"oos_validation": {"status": "weak"}, "probability_calibration": "uncalibrated", "reason_codes": []}

    validated = _confidence_composite(
        base_score=90, path_assessments=[strong_path, ok_path], valid_count=2, agreement="high", diversity="medium",
        manifest={"horizon": "5D"},
        oos_probability_quality={
            "algorithm_policy_replay": {"non_overlapping_predicted_dates": 40, "brier_skill_vs_expanding_base_rate": 0.05,
                                        "skill_uncertainty": {"ci_low": 0.01, "ci_high": 0.09, "independent_blocks": 6}},
            "ensemble_calibration": {"status": "improved_on_subsequent_ledger_block"}})
    assert validated["label"] == "high" and validated["score"] >= 80, validated

    typical = _confidence_composite(
        base_score=75, path_assessments=[ok_path, weak_path], valid_count=2, agreement="medium", diversity="medium",
        manifest={"horizon": "5D"},
        oos_probability_quality={
            "algorithm_policy_replay": {"non_overlapping_predicted_dates": 18, "brier_skill_vs_expanding_base_rate": 0.01,
                                        "skill_uncertainty": {"ci_low": -0.02, "ci_high": 0.04, "independent_blocks": 5}},
            "ensemble_calibration": {}})
    assert 60 <= typical["score"] < 80, typical

    contradicted = _confidence_composite(
        base_score=75, path_assessments=[ok_path, weak_path], valid_count=2, agreement="medium", diversity="medium",
        manifest={"horizon": "5D"},
        oos_probability_quality={
            "algorithm_policy_replay": {"non_overlapping_predicted_dates": 30, "brier_skill_vs_expanding_base_rate": -0.05,
                                        "skill_uncertainty": {"ci_low": -0.09, "ci_high": -0.01, "independent_blocks": 6}},
            "ensemble_calibration": {}})
    assert contradicted["score"] <= 64, contradicted
    assert "policy_oos_skill_below_expanding_base_rate_interval" in contradicted["reasons"], contradicted


def check_f15_52_week_window_defaults() -> None:
    from quant_research.researchers import ml, quant
    from quant_research.news.decay import NEWS_MAX_AGE_CALENDAR_DAYS
    from yahoo_collect import DEFAULT_HISTORY_WEEKS, _default_history_start

    assert DEFAULT_HISTORY_WEEKS == 52
    assert _default_history_start(dt.date(2026, 1, 2)) == dt.date(2025, 1, 3)
    assert _default_history_start(dt.date(2026, 1, 2), 3) == dt.date(2025, 12, 12)
    assert NEWS_MAX_AGE_CALENDAR_DAYS == 90

    bars = synth_bars(252)
    manifest, market = ml_snapshot_stub(bars)
    loaded = (manifest, market)
    for module in (quant, ml):
        result = module.run(Path("unused"), loaded)
        validation = result.get("validation") or {}
        count = validation.get("non_overlapping_prediction_count")
        assert result.get("prob_up") is not None, (module.__name__, result.get("status"), result.get("warnings"))
        assert isinstance(count, int) and count >= 11, (module.__name__, count)  # weak-support floor at the 5D horizon


def check_f16_factor_path_fits_52_weeks() -> None:
    from quant_research.researchers import factor

    budget = factor._sample_feasibility(252, 5)
    assert budget["estimated_minimum_daily_bars"] == 240
    assert budget["status"] == "budget_possible_not_guaranteed_by_factor_scores"
    assert budget["selection_label_capacity"] >= factor.MINIMUM_SELECTION_LABELS
    assert budget["max_matured_non_overlapping_label_capacity"] >= 18
    assert factor._sample_feasibility(239, 5)["status"] == "capacity_below_52_week_minimum"
    assert factor._sample_feasibility(252, 20)["status"] == "capacity_below_52_week_minimum"

    bars = synth_bars(252, seed=11)
    manifest, market = ml_snapshot_stub(bars)
    original_loader = factor.load_snapshot
    factor.load_snapshot = lambda _dir: (manifest, market)
    try:
        result = factor.run(Path("unused"))
    finally:
        factor.load_snapshot = original_loader
    validation = result["validation"]
    assert result["status"] == "success"
    assert isinstance(result["prob_up"], float) and 0.0 < result["prob_up"] < 1.0
    assert validation["selection_label_count"] >= factor.MINIMUM_SELECTION_LABELS
    assert validation["calibration_label_capacity"] >= 18
    assert all(
        count >= factor.MINIMUM_FACTOR_BUCKET_OBSERVATIONS
        for count in validation["calibration_bucket_counts"].values()
    )
    assert validation["holdout_predictions"] >= 10
    assert len(validation["oos_forecasts"]) >= validation["holdout_predictions"]
    assert validation["leakage_audit"]["calibration_labels_matured_before_each_decision"] is True
    for forecast in validation["oos_forecasts"]:
        end = forecast["training_label_end_session"]
        assert end is None or end <= forecast["decision_session"]

    short_bars = synth_bars(120, seed=11)
    manifest, market = ml_snapshot_stub(short_bars)
    factor.load_snapshot = lambda _dir: (manifest, market)
    try:
        short_result = factor.run(Path("unused"))
    finally:
        factor.load_snapshot = original_loader
    assert short_result["status"] == "insufficient_data"
    assert short_result["prob_up"] is None


def _news_input(bars: list[dict], articles: list[dict], ticker: str = "MU"):
    from quant_research.news.contracts import NewsInput

    return NewsInput(
        ticker=ticker, market="US", horizon="5D",
        asof_timestamp=f"{bars[-1]['date']}T21:30:00Z", snapshot_id="snap_f17",
        news_status="complete", source_sentiment=None,
        articles=tuple(articles), bars=tuple(bars), warnings=(),
        coverage={"session_closes": {}, "calendar_verification": "source_supplied_schedule"},
    )


def _f17_articles(bars: list[dict]) -> list[dict]:
    dates = [bar["date"] for bar in bars[-5:]]
    return [
        {"article_id": "a1", "title": f"MU raises guidance on strong demand ({dates[-1]})", "url": "https://example.com/a1",
         "source": "Reuters", "source_type": "news", "record_type": "news", "published_at": f"{dates[-1]}T13:00:00Z",
         "tickers": ["MU"], "summary": None},
        {"article_id": "a2", "title": f"MU misses estimates as demand weak ({dates[-2]})", "url": "https://example.com/a2",
         "source": "Bloomberg", "source_type": "news", "record_type": "news", "published_at": f"{dates[-2]}T13:00:00Z",
         "tickers": ["MU"], "summary": None},
        {"article_id": "a3", "title": f"MU wins contract ({dates[-3]})", "url": "https://example.com/a3",
         "source": "Reuters", "source_type": "news", "record_type": "news", "published_at": f"{dates[-3]}T13:00:00Z",
         "tickers": ["MU"], "summary": None, "canonical_event_id": "dup"},
        {"article_id": "a4", "title": f"MU profit falls after lawsuit ({dates[-3]})", "url": "https://example.com/a4",
         "source": "Bloomberg", "source_type": "news", "record_type": "news", "published_at": f"{dates[-3]}T15:00:00Z",
         "tickers": ["MU"], "summary": None, "canonical_event_id": "dup"},
        {"article_id": "a5", "title": f"Chipmaker expands capacity in Asia ({dates[-1]})", "url": "https://example.com/a5",
         "source": "Wire", "source_type": "news", "record_type": "news", "published_at": f"{dates[-1]}T14:00:00Z",
         "tickers": [], "summary": None},
    ]


def check_f17_ai_lean_layer() -> None:
    from quant_research.news import AI_ASSESSMENT_SCHEMA_VERSION, analyze_news

    bars = synth_bars(60, seed=7)
    news_input = _news_input(bars, _f17_articles(bars))
    baseline = analyze_news(news_input)
    core = ("overall_direction", "confidence", "event_strength", "market_confirmation", "status")

    risk_ids = {row["event_id"] for row in baseline["risks"]}
    mixed_ids = {row["event_id"] for row in baseline["mixed_events"]}
    assert not (risk_ids & mixed_ids), "mixed-direction events must not be reported as bearish risks"
    assert baseline["mixed_events"], "the conflicting-headline cluster should be reported as mixed"
    assert baseline["method"]["news_probability"] == "not_emitted"
    assert baseline["method"]["calibrated"] is False

    candidates = baseline["ai_lean_candidates"]
    candidate_ids = {row["news_id"] for row in candidates}
    assert len(candidate_ids) == len(candidates) >= 3
    pregated = [row for row in candidates if row["eligible_evidence"] is False]
    assert pregated, "a record failing the relevance gate must still be offered to the lean stage"

    def artifact(items):
        return {"schema_version": AI_ASSESSMENT_SCHEMA_VERSION, "ticker": "MU",
                "asof_timestamp": news_input.asof_timestamp, "basis": "headline_level_ai_screen_v1",
                "model": {"id": "selfcheck-stub", "prompt_sha256": "0" * 64, "generated_at_utc": news_input.asof_timestamp},
                "items": items}

    bullish_id = next(row["news_id"] for row in candidates if row["direction"] == "positive")
    bearish_id = next(row["news_id"] for row in candidates if row["direction"] == "negative")
    pregated_id = pregated[0]["news_id"]
    good = artifact([
        {"news_id": bullish_id, "lean": "bullish", "reason": "标题称指引上调", "evidence_grade": "reported_event"},
        {"news_id": bearish_id, "lean": "bearish", "reason": "标题称不及预期", "evidence_grade": "reported_event"},
        {"news_id": pregated_id, "lean": "unclear", "reason": "标题未点明标的", "evidence_grade": "headline_only",
         "relevance_basis": "not_explicitly_matched"},
    ])
    scored = analyze_news(news_input, None, good)
    lean = scored["ai_lean"]
    assert lean["status"] == "assessed"
    assert [item["news_id"] for item in lean["bullish"]] == [bullish_id]
    assert [item["news_id"] for item in lean["bearish"]] == [bearish_id]
    assert lean["bullish"][0]["reason"] == "标题称指引上调"
    assert lean["display_only"] is True
    assert lean["coverage"]["candidate_count"] == len(candidates) and lean["coverage"]["assessed_count"] == 3
    for key in core:
        assert scored[key] == baseline[key], f"lean must not change News {key}"
    assert [row["direction"] for row in scored["major_events"]] == [row["direction"] for row in baseline["major_events"]]
    assert {row["event_id"] for row in scored["risks"]} == risk_ids
    attached = {row["event_id"]: row for row in scored["major_events"]}
    assert attached[bullish_id]["ai_lean"] == "bullish" and attached[bullish_id]["direction"] == "positive"

    rejections = {
        "unknown news id": artifact([{"news_id": "event_deadbeef", "lean": "bullish", "reason": "x"}]),
        "quant vocabulary": artifact([{"news_id": bullish_id, "lean": "bullish", "reason": "prob_up should rise"}]),
        "identity mismatch": {**artifact([{"news_id": bullish_id, "lean": "bullish", "reason": "x"}]), "ticker": "NVDA"},
        "unsupported field": artifact([{"news_id": bullish_id, "lean": "bullish", "reason": "x", "probability": 0.9}]),
        "bad label": artifact([{"news_id": bullish_id, "lean": "very bullish", "reason": "x"}]),
    }
    for name, bad in rejections.items():
        rejected = analyze_news(news_input, None, bad)
        assert rejected["ai_lean"]["status"] == "invalid", f"{name} should be rejected"
        for key in core:
            assert rejected[key] == baseline[key], f"{name} must not change News {key}"
        assert any("lean artifact rejected" in warning for warning in rejected["warnings"])

    absent = analyze_news(news_input, None, None)
    assert absent["ai_lean"]["status"] == "not_assessed"
    assert absent["ai_lean"]["bullish"] == [] and absent["ai_lean"]["bearish"] == []
    assert absent["ai_lean"]["probability_effect"].startswith("none")
    assert absent["status"] == baseline["status"]

    empty = _news_input(bars, [])
    empty_result = analyze_news(empty, None, artifact([{"news_id": "event_x", "lean": "bullish", "reason": "x"}]))
    assert empty_result["ai_lean"]["status"] == "invalid"


# ---------------------------------------------------------------- F18 / F19
def check_f18_orphan_sweep() -> None:
    from quant_research import cleanup

    work = Path(tempfile.mkdtemp(prefix="selfcheck_f18_"))
    try:
        root = work / "run"
        (root / "snapshots").mkdir(parents=True)
        (root / "research" / "a_1").mkdir(parents=True)
        (root / "run_manifest.json").write_text("{}", encoding="utf-8")
        (root / ".pipeline.lock").write_text("pid=1\n", encoding="ascii")
        (root / "research" / "a_1" / "report.md").write_text("md", encoding="utf-8")
        (root / "snapshots" / ".market.json.0123456789ab.tmp").write_text("x", encoding="utf-8")
        scratch = root / "snapshots" / ".snap_20260101T000000000000Z_deadbeef.abc123xy"
        (scratch / "raw").mkdir(parents=True)
        (scratch / "manifest.json").write_text("{}", encoding="utf-8")
        (scratch / "raw" / "market.json").write_text("{}", encoding="utf-8")
        (root / "notes.txt").write_text("keep", encoding="utf-8")

        preview = cleanup.sweep_orphans(root, dry_run=True)
        assert preview["removed"] is False
        assert preview["orphan_files"] == ["snapshots/.market.json.0123456789ab.tmp"], preview
        assert preview["orphan_directories"] == ["snapshots/.snap_20260101T000000000000Z_deadbeef.abc123xy"], preview
        assert scratch.exists(), "a dry run must not delete"

        report = cleanup.sweep_orphans(root)
        assert report["removed"] is True
        assert not (root / "snapshots" / ".market.json.0123456789ab.tmp").exists()
        assert not scratch.exists(), "orphan scratch directory survived"
        for kept in ("run_manifest.json", ".pipeline.lock", "notes.txt"):
            assert (root / kept).exists(), f"{kept} was removed by the orphan sweep"
        assert (root / "research" / "a_1" / "report.md").exists()
        assert (root / "snapshots").is_dir()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_f19_retention_gc() -> None:
    from quant_research import cleanup, retention

    work = Path(tempfile.mkdtemp(prefix="selfcheck_f19_"))
    moment = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def make_run(run_id: str, age_days: int, *, receipt: bool = True, extra: str | None = None,
                 state: str = "cleanup_in_progress") -> Path:
        root = work / "runs" / run_id
        (root / "research" / "a_1").mkdir(parents=True)
        (root / "research" / "a_1" / "report.md").write_text("md", encoding="utf-8")
        (root / "research" / "a_1" / "final_response.md").write_text("fr", encoding="utf-8")
        created = (moment - dt.timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (root / "run_manifest.json").write_text(json.dumps({
            "manifest_version": 1, "mode": "owned_pipeline", "run_id": run_id,
            "state": state, "created_at_utc": created, "collection_name": None,
            "snapshot_id": None, "analysis_id": "a_1",
        }), encoding="utf-8")
        if receipt:
            (root / cleanup.CLEANUP_RECEIPT).write_text(cleanup._receipt_data(
                mode="run", run_id=run_id, analysis_id="a_1", snapshot_id=None,
                collection_name=None, state="complete", response_hash=None, report_hash=None,
                final_response_hash=None, analysis_status=None, reporting_status=None,
                targets=[],
            ), encoding="utf-8")
        if extra:
            (root / extra).write_text("x", encoding="utf-8")
        return root

    try:
        (work / "runs").mkdir()
        expired = make_run("run_20260101T000000000000Z_aaaaaaaa", 40)
        fresh = make_run("run_20260530T000000000000Z_bbbbbbbb", 2)
        dirty = make_run("run_20260101T000000000000Z_cccccccc", 40, extra="notes.txt")
        unfinished = make_run("run_20260101T000000000000Z_dddddddd", 40, receipt=False)
        pending = make_run("run_20260101T000000000000Z_eeeeeeee", 40, receipt=False, state="awaiting_cleanup")
        cache = work / "yfinance-cache" / "tkr"
        cache.mkdir(parents=True)
        (cache / "stale.json").write_text("x", encoding="utf-8")
        old = (moment - dt.timedelta(days=30)).timestamp()
        os.utime(cache / "stale.json", (old, old))
        (cache / "fresh.json").write_text("x", encoding="utf-8")

        preview = retention.gc_runtime(work, retain_days=30, pending_days=1, dry_run=True, now=moment)
        assert expired.exists() and dirty.exists(), "a dry run must not delete"
        assert expired.name in preview["runs_would_delete"], preview

        report = retention.gc_runtime(work, retain_days=30, pending_days=1, now=moment)
        assert report["status"] == "success", report
        assert report["runs_deleted"] == [expired.name], report
        assert not expired.exists(), "expired run directory survived"
        assert fresh.exists(), "a young run directory was expired"
        assert dirty.exists(), "a run directory with unknown files was expired"
        assert unfinished.exists(), "a run directory without a complete receipt was expired"
        assert pending.exists(), "a pending run without verifiable artifacts was expired"
        skipped = {row["run_id"]: row["reason"] for row in report["runs_skipped"]}
        assert dirty.name in skipped and "non-retained file" in skipped[dirty.name], skipped
        assert unfinished.name in skipped, skipped
        assert pending.name in skipped, skipped
        assert not (work / "runs" / f".{expired.name}.cleanup.lock").exists()
        assert not (cache / "stale.json").exists(), "stale cache file survived"
        assert (cache / "fresh.json").exists(), "fresh cache file was pruned"

        throttled = retention.gc_runtime(work, retain_days=30, throttle_hours=24, now=moment)
        assert throttled["status"] == "throttled", throttled
        unthrottled = retention.gc_runtime(work, retain_days=30, throttle_hours=24,
                                           now=moment + dt.timedelta(hours=25))
        assert unthrottled["status"] == "success", unthrottled
    finally:
        shutil.rmtree(work, ignore_errors=True)


CHECKS = [
    ("F01 current probability not clobbered", check_f01_current_probability_not_clobbered),
    ("F02 per-origin calibration fields", check_f02_causal_calibration_fields),
    ("F02 prefix invariance", check_f02_prefix_invariance),
    ("F02 audit accepts/rejects", check_f02_audit_accepts_and_rejects),
    ("F03 credential redaction", check_f03_redaction),
    ("F04 response text validation", check_f04_response_text_validation),
    ("F05 retained artifact binding", check_f05_retained_binding),
    ("F07 malformed market fields", check_f07_malformed_fields),
    ("F08 preflight and fallback counting", check_f08_preflight_and_counting),
    ("F09 bootstrap estimability", check_f09_bootstrap_estimability),
    ("F10 ensemble check scope", check_f10_ensemble_scope),
    ("F11 options expiry Unix seconds", check_f11_options_expiry_unix_seconds),
    ("F12 cache_dir PathLike normalized", check_f12_cache_dir_pathlike),
    ("F13 horizon-scaled OOS support", check_f13_horizon_scaled_support),
    ("F14 confidence composite bands", check_f14_confidence_composite),
    ("F15 52-week window defaults", check_f15_52_week_window_defaults),
    ("F16 factor path fits 52 weeks", check_f16_factor_path_fits_52_weeks),
    ("F17 host AI lean layer", check_f17_ai_lean_layer),
    ("F18 orphan scratch sweep", check_f18_orphan_sweep),
    ("F19 retention and cache pruning", check_f19_retention_gc),
]

if __name__ == "__main__":
    for name, check in CHECKS:
        case(name, check)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("\nall checks passed")
