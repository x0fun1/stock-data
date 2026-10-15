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


def check_f15_one_year_window_defaults() -> None:
    from quant_research.researchers import ml, quant
    from yahoo_collect import _year_back

    assert _year_back(dt.date(2026, 1, 2)) == dt.date(2025, 1, 2)
    assert _year_back(dt.date(2026, 1, 2), 3) == dt.date(2023, 1, 2)

    bars = synth_bars(252)
    manifest, market = ml_snapshot_stub(bars)
    loaded = (manifest, market)
    for module in (quant, ml):
        result = module.run(Path("unused"), loaded)
        validation = result.get("validation") or {}
        count = validation.get("non_overlapping_prediction_count")
        assert result.get("prob_up") is not None, (module.__name__, result.get("status"), result.get("warnings"))
        assert isinstance(count, int) and count >= 11, (module.__name__, count)  # weak-support floor at the 5D horizon


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
    ("F15 one-year window defaults", check_f15_one_year_window_defaults),
]

if __name__ == "__main__":
    for name, check in CHECKS:
        case(name, check)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("\nall checks passed")