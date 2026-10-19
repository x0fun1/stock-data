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
- F20      pipeline golden: two fixed fixtures (with and without a news domain) must
           reproduce the same normalised digests and anchors, so a refactor can
           prove it moved no frozen value.
- F21      OOS record audit: one crafted ledger for the summary accounting plus one
           single-mutation case per rejection code, so the record checks can be moved
           without moving behaviour.

Usage: python stock-data/scripts/selfcheck.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import random
import re
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
    delivery.required_response_lines = lambda _result: dict(fake_required)
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
            except ValueError:  # noqa: PERF203 - each bad case is checked independently; a failure must not stop the battery
                pass
            else:
                raise AssertionError(f"{label} was not rejected")
    finally:
        delivery.required_response_lines = original


def check_f05_retained_binding() -> None:
    from quant_research import cleanup

    original_md, original_fr, original_vr = cleanup.render_markdown, cleanup.render_final_response, cleanup.validate_response_text
    cleanup.render_markdown = lambda report: "MD:" + json.dumps(report, sort_keys=True)
    cleanup.render_final_response = lambda _report, summary=None: "FR:" + json.dumps(summary, sort_keys=True)
    cleanup.validate_response_text = lambda _result, _text: None
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

    run_with(lambda _b: None)
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
    import yahoo_compat_fallback as fb
    from yfinance_provider import GatewayError, YahooProvider

    provider = YahooProvider(cache_dir=tempfile.mkdtemp())
    sentinel = {"sentinel": True}
    provider._call_yfinance = lambda _name, **_params: sentinel

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

        def kline(self, symbol, **_kwargs):
            self.hits += 1
            return {"symbol": symbol}

        def holders(self, **_kwargs):
            self.hits += 1
            return {"ok": True}

        def options_chain(self, _symbol, **_kwargs):
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
        MIN_BOOTSTRAP_INDEPENDENT_BLOCKS,
        _block_bootstrap_skill_interval,
    )

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

    def stub_get(session, url, params=None, timeout=None):  # noqa: ARG001 - signature mirrors the real transport call; only params is inspected
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
    provider._call_yfinance = lambda _name, **_params: sentinel
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
    from quant_research.news.decay import NEWS_MAX_AGE_CALENDAR_DAYS
    from quant_research.researchers import ml, quant
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


# ---------------------------------------------------------------- F20
# Golden regression: the frozen artifacts are a hash-committed contract, so any
# refactor must prove it moved no value. Two fixtures (without and with a news
# domain) run in-process; identity and timing fields are normalised before
# hashing, and a short anchor list is compared verbatim so a mismatch names the
# field that moved instead of only reporting a digest.
GOLDEN_PIPELINE: dict[str, dict[str, str]] = {
    "plain": {
        "report.json": "0a07d96a10f0159934f3920a3cc564528179dcb9fefab36c7e901d872a83ae3d",
        "quant_result.json": "b71cfe8fa867a38351140a8e3d50f55bd0e1c11b8f272d744825d3f878e8c529",
        "news_result.json": "44bfc39702eb43481a15ae736378576384f1ea4b59b84d6a1138111bc7521171",
        "agent_summary.json": "7d6192fb268d10e1a2f4c8de84c29995c7cd768b22982cb8a0b815eb1d734056",
        "final_response.md": "f23c4762739a13e5da17405b8e1f79c0d95ddd482bc251b48605eaa033492ed3",
    },
    "news": {
        "report.json": "473723c4d551551ca474f2e80746544d00549fbcc684f04c824b0f67608e5554",
        "quant_result.json": "21d6eefa9f352da879630406dfe24af74bd45cf825c03d8d3d1e7d97a01a6662",
        "news_result.json": "23b3cac4bab741d4cc621731e4b5ca0955f7e18f93c6c362388b8df5a343f02a",
        "agent_summary.json": "54ba77c2eb34204dd63e13bd1451154f8997f95619a58b6e4c163df52896ced6",
        "final_response.md": "5ebb6727030c07eb303ea6c665c24e63624e693e380c39a7be8897d8321cc813",
    },
}
GOLDEN_ANCHORS: dict[str, dict[str, object]] = {
    "plain": {
        "summary.analysis_status": "degraded",
        "summary.reporting_status": "degraded",
        "summary.sections.final_synthesis.may_report_direction": True,
        "summary.sections.final_synthesis.news_relationship": "消息面交叉验证未评估",
        "summary.sections.news.event_bias": "unknown",
        "summary.sections.news.status": "not_assessed",
        "quant.consensus.prob_up": 0.5853976420216035,
        "quant.consensus.direction": "bullish",
        "quant.consensus.confidence": "low",
        "quant.consensus.agreement": "medium",
        "quant.consensus.confidence_score": 58,
        "quant.consensus.reporting_status": "degraded",
        "quant.consensus.predictive_evidence_grade": "observed_skill_below_expanding_base_rate",
        "quant.consensus.probability_basis": "ensemble_calibrated_equal_weight_mean_of_valid_path_estimates",
        "quant.researchers.quant.status": "success",
        "quant.researchers.factor.status": "success",
        "quant.researchers.ml.status": "partial",
        "quant.researchers.factor_backtest.status": "partial",
        "news.status": "not_assessed",
        "news.overall_direction": "unknown",
        "news.event_strength": None,
        "news.confidence": "unavailable",
    },
    "news": {
        "summary.analysis_status": "degraded",
        "summary.reporting_status": "degraded",
        "summary.sections.final_synthesis.may_report_direction": True,
        "summary.sections.final_synthesis.news_relationship": "消息面交叉验证未评估",
        "summary.sections.news.event_bias": "unknown",
        "summary.sections.news.status": "partial",
        "quant.consensus.prob_up": 0.5853976420216035,
        "quant.consensus.direction": "bullish",
        "quant.consensus.confidence": "low",
        "quant.consensus.agreement": "medium",
        "quant.consensus.confidence_score": 58,
        "quant.consensus.reporting_status": "degraded",
        "quant.consensus.predictive_evidence_grade": "observed_skill_below_expanding_base_rate",
        "quant.consensus.probability_basis": "ensemble_calibrated_equal_weight_mean_of_valid_path_estimates",
        "quant.researchers.quant.status": "success",
        "quant.researchers.factor.status": "success",
        "quant.researchers.ml.status": "partial",
        "quant.researchers.factor_backtest.status": "partial",
        "news.status": "partial",
        "news.overall_direction": "unknown",
        "news.event_strength": None,
        "news.confidence": "low",
    },
}

_GOLDEN_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_GOLDEN_STAMP_ID = re.compile(r"\d{8}T\d{12}Z")
_GOLDEN_VOLATILE_KEYS = frozenset({
    "analysis_id", "snapshot_id", "run_id", "created_at", "created_at_utc", "frozen_at",
    "generated_at_utc", "captured_at_utc", "updated_at_utc", "collection_captured_at",
    "snapshot_created_at", "asof_resolved_from",
})
_GOLDEN_ANCHOR_PATHS = (
    "summary.analysis_status",
    "summary.reporting_status",
    "summary.sections.final_synthesis.may_report_direction",
    "summary.sections.final_synthesis.news_relationship",
    "summary.sections.news.event_bias",
    "summary.sections.news.status",
    "quant.consensus.prob_up",
    "quant.consensus.direction",
    "quant.consensus.confidence",
    "quant.consensus.agreement",
    "quant.consensus.confidence_score",
    "quant.consensus.reporting_status",
    "quant.consensus.predictive_evidence_grade",
    "quant.consensus.probability_basis",
    "quant.researchers.quant.status",
    "quant.researchers.factor.status",
    "quant.researchers.ml.status",
    "quant.researchers.factor_backtest.status",
    "news.status",
    "news.overall_direction",
    "news.event_strength",
    "news.confidence",
)
_GOLDEN_ARTICLES = (
    {"title": "SYN beats quarterly revenue expectations", "published_at": "2025-12-30T13:00:00Z",
     "tickers": ["SYN"], "source": "Example Wire", "url": "https://example.test/a1",
     "summary": "SYN reported revenue above consensus."},
    {"title": "SYN announces $2 billion buyback", "published_at": "2025-12-31T14:30:00Z",
     "tickers": ["SYN"], "source": "Example Wire", "url": "https://example.test/a2"},
    {"title": "Analyst downgrades SYN to hold on valuation", "published_at": "2026-01-02T14:00:00Z",
     "tickers": ["SYN"], "source": "Example Research", "url": "https://example.test/a3"},
    {"title": "SYN CEO says demand remains strong into next quarter", "published_at": "2026-01-02T15:00:00Z",
     "tickers": ["SYN"], "source": "Example Times", "url": "https://example.test/a4"},
)


def _golden_bars(count: int = 260, seed: int = 11) -> list[dict]:
    """Deterministic OHLCV ending on the fixture as-of date, not the wall clock."""
    rng = random.Random(seed)
    days: list[dt.date] = []
    day = dt.date(2026, 1, 2)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    days.reverse()
    price = 100.0
    bars = []
    for session in days:
        price *= math.exp(rng.gauss(0.0006, 0.014))
        bars.append({"date": session.isoformat(), "open": round(price * 0.997, 4),
                     "high": round(price * 1.011, 4), "low": round(price * 0.989, 4),
                     "close": round(price, 4), "volume": 1_200_000})
    return bars


def _golden_responses(with_news: bool) -> dict:
    bars = _golden_bars()
    market = {
        "status": "success", "actual_source": "synthetic-offline",
        "source_timestamp": "2026-01-02T21:00:00Z", "fetched_at_utc": "2026-01-03T00:30:00Z",
        "currency": "USD", "unit": "USD", "adjustment": "unadjusted", "frequency": "1d",
        "source_symbol": "SYN",
        "data": {"currency": "USD", "exchange": "US", "adjustment": "unadjusted", "unit": "USD",
                 "frequency": "1d", "source_symbol": "SYN", "bars": bars,
                 "source_timestamp": "2026-01-02T21:00:00Z", "fetched_at_utc": "2026-01-03T00:30:00Z",
                 "history_window": {"start": bars[0]["date"], "end": bars[-1]["date"],
                                    "selection": "trailing 52 weeks"}},
    }
    domains: dict[str, object] = {"market": market}
    if with_news:
        domains["news"] = {"status": "success", "actual_source": "synthetic-offline",
                           "fetched_at_utc": "2026-01-03T00:30:00Z",
                           "data": {"articles": [dict(article) for article in _GOLDEN_ARTICLES]}}
    return {"domains": domains}


def _golden_normalize(value, key: str | None = None):
    """Fold identity, digests and timings so only contract values are compared."""
    if isinstance(value, dict):
        normalized = {}
        for name, item in value.items():
            if name in _GOLDEN_VOLATILE_KEYS:
                normalized[name] = "<volatile>"
            elif "sha256" in name or name.endswith("_digest"):
                normalized[name] = "<digest>"
            elif name.endswith("_wall_seconds") or name in {"wall_seconds", "peak_memory"}:
                normalized[name] = "<timing>"
            else:
                normalized[name] = _golden_normalize(item, name)
        return normalized
    if isinstance(value, list):
        return [_golden_normalize(item, key) for item in value]
    if isinstance(value, str):
        return _GOLDEN_STAMP_ID.sub("<id>", _GOLDEN_TIMESTAMP.sub("<ts>", value))
    if isinstance(value, float):
        return round(value, 9)
    return value


def _golden_digest(value) -> str:
    canonical = json.dumps(_golden_normalize(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _golden_lookup(container, path: str):
    current = container
    for part in path.split("."):
        if not isinstance(current, dict):
            return "<missing>"
        current = current.get(part)
    return current


def _offline_library_calendar(*_args: object, **_kwargs: object) -> None:
    """Offline stand-in for the optional exchange-calendar probe."""
    return


def _golden_run(with_news: bool) -> tuple[dict[str, str], dict[str, object]]:
    """Run the pipeline once in a temp tree and return normalised digests and anchors."""
    from quant_research import data_checks
    from quant_research.orchestrator import run_analysis
    from quant_research.snapshot import freeze_snapshot
    from quant_research.stock_data_adapter import StockDataAdapter

    work = Path(tempfile.mkdtemp(prefix=f"selfcheck_f20_{'news' if with_news else 'plain'}_"))
    library_calendar = data_checks._library_calendar
    # The golden fixture pins the offline profile: the optional exchange-calendar probe is disabled
    # so recorded digests never depend on which optional libraries happen to be installed.
    data_checks._library_calendar = _offline_library_calendar
    try:
        request = {"ticker": "SYN", "market": "US", "horizon": "5D", "intent": "forecast",
                   "asset_type": "stock", "asof": "2026-01-02T21:00:00Z"}
        receipt = StockDataAdapter().write_receipt(
            request, _golden_responses(with_news)["domains"], work / "collection.json",
            captured_at_utc="2026-01-03T00:35:00Z")
        snapshot = freeze_snapshot(receipt, work / "snapshots")
        analysis = run_analysis(snapshot, work / "research")
        artifacts = {}
        for name in ("report.json", "quant_result.json", "news_result.json", "agent_summary.json"):
            artifacts[name] = _golden_digest(json.loads((analysis / name).read_text(encoding="utf-8")))
        artifacts["final_response.md"] = _golden_digest(
            (analysis / "final_response.md").read_text(encoding="utf-8").splitlines())
        containers = {
            "summary": json.loads((analysis / "agent_summary.json").read_text(encoding="utf-8")),
            "quant": json.loads((analysis / "quant_result.json").read_text(encoding="utf-8")),
            "news": json.loads((analysis / "news_result.json").read_text(encoding="utf-8")),
        }
        anchors = {}
        for path in _GOLDEN_ANCHOR_PATHS:
            root, _, remainder = path.partition(".")
            anchors[path] = _golden_lookup(containers[root], remainder)
        return artifacts, anchors
    finally:
        data_checks._library_calendar = library_calendar
        shutil.rmtree(work, ignore_errors=True)


def check_f20_pipeline_golden() -> None:
    for fixture, with_news in (("plain", False), ("news", True)):
        artifacts, anchors = _golden_run(with_news)
        expected_artifacts = GOLDEN_PIPELINE[fixture]
        expected_anchors = GOLDEN_ANCHORS[fixture]
        if not expected_artifacts or not expected_anchors:
            raise AssertionError(f"fixture {fixture!r} has no recorded golden values")
        changed = sorted(name for name, value in artifacts.items() if expected_artifacts.get(name) != value)
        moved = {path: (expected_anchors.get(path), anchors.get(path))
                 for path in anchors if expected_anchors.get(path) != anchors.get(path)}
        if changed or moved:
            detail = "; ".join(f"{path}: {old!r} -> {new!r}" for path, (old, new) in list(moved.items())[:6])
            raise AssertionError(
                f"pipeline golden drift in fixture {fixture!r}; artifacts changed: {changed or 'none'}; "
                f"anchors changed: {detail or 'none'}")


def check_f21_oos_record_audit() -> None:
    """One direct case per OOS rejection code, plus the ledger-level accounting."""
    from quant_research.validation.probability_quality import _audit_oos_path

    sessions: list[str] = []
    day = dt.date(2026, 1, 5)
    while len(sessions) < 16:
        if day.weekday() < 5:
            sessions.append(day.isoformat())
        day += dt.timedelta(days=1)
    bars = [{"date": session, "close": 100.0 + index} for index, session in enumerate(sessions)]
    manifest = {"ticker": "SYN", "horizon": "5D", "snapshot_id": "snap_1",
                "latest_confirmed_close": {"session_date": sessions[12]},
                "asof_timestamp": sessions[12] + "T21:00:00Z"}

    def row(decision: int, label: int, *, target: int = 1, **extra) -> dict:
        base = {"decision_session": sessions[decision], "label_end_session": sessions[label],
                "target": target, "probability": 0.6,
                "training_label_end_session": sessions[max(0, decision - 2)],
                "target_definition": "close[t+5] / close[t] - 1 > 0"}
        base.update(extra)
        return base

    rows = [
        row(2, 7),                                  # accepted
        row(3, 8), row(3, 8),                       # duplicate decision session
        {"decision_session": sessions[4], "target": 1, "probability": 0.6},
        row(5, 10, target=0),                       # target contradicts frozen closes
        row(8, 13),                                 # label not matured by the as-of
        "not-a-dict",
        row(1, 6, calibration_applied=True, raw_probability=0.55,
            calibration_fit_observations=10, calibration_slope=1.0, calibration_intercept=0.0),
    ]
    accepted, summary = _audit_oos_path({"validation": {"oos_forecasts": rows}}, manifest, bars)
    assert summary["supplied_samples"] == 8, summary
    assert summary["valid_samples"] == 1 and summary["rejected_samples"] == 7, summary
    assert list(accepted) == [sessions[2]], accepted
    assert summary["valid_predictions"] == 1
    assert summary["status"] == "rejected_records"
    assert summary["rejection_reasons"] == {
        "calibrated_probability_mismatch": 1,
        "calibration_fit_set_insufficient_or_invalid": 1,
        "calibration_label_boundary_missing": 1,
        "decision_or_label_end_missing": 2,
        "duplicate_decision_session": 2,
        "invalid_binary_target": 1,
        "label_not_matured_by_snapshot_asof": 1,
        "record_not_object": 1,
        "target_definition_does_not_match_requested_horizon": 2,
        "target_does_not_match_frozen_close_prices": 1,
        "training_label_boundary_missing_for_prediction": 1,
    }, summary["rejection_reasons"]
    assert accepted[sessions[2]]["target_definition"] == "close[t+5] / close[t] - 1 > 0"
    assert [item["record_index"] for item in summary["rejected_examples"]] == [1, 2, 3, 4, 5], summary

    # Without frozen bars the audit must say so rather than imply a session-axis check.
    _, limited = _audit_oos_path({"validation": {"oos_forecasts": [row(2, 7)]}}, manifest, None)
    assert limited["status"] == "validated" and limited["valid_samples"] == 1, limited
    assert limited["structural_validation"] == "limited_no_frozen_session_index"
    assert limited["non_overlapping_prediction_count"] is None

    # Single-mutation cases: one valid matured record, one field broken at a time. Each
    # assertion names the exact reason set, so a rewritten branch that emits a different
    # code (or an extra one) fails here instead of silently widening the audit.
    _omit = object()
    valid = {"decision_session": sessions[2], "label_end_session": sessions[7],
             "target": 1, "probability": 0.6,
             "training_label_end_session": sessions[0],
             "target_definition": "close[t+5] / close[t] - 1 > 0"}

    def case(**overrides) -> dict:
        merged = {**valid, **overrides}
        return {key: value for key, value in merged.items() if value is not _omit}

    def reasons(record, *, frozen=bars, scope=manifest) -> set[str]:
        _, audit = _audit_oos_path({"validation": {"oos_forecasts": [record]}}, scope, frozen)
        return set(audit["rejection_reasons"])

    assert reasons(case()) == set(), "a fully specified matured record must be accepted"
    assert reasons("not-a-dict") == {"record_not_object", "decision_or_label_end_missing",
                                     "invalid_binary_target",
                                     "target_definition_does_not_match_requested_horizon"}, "non-object record"
    assert reasons(case(decision_session=None)) == {"decision_or_label_end_missing"}, "missing decision"
    assert reasons(case(decision_session="2026-01-01")) == {"session_missing_from_frozen_market_bars"}, "off-calendar decision"
    assert reasons(case(), scope={**manifest, "latest_confirmed_close": {"session_date": "2026-01-03"}}) == {
        "snapshot_asof_session_missing_from_frozen_market_bars"}, "as-of outside the frozen axis"
    assert reasons(case(label_end_session=sessions[8])) == {
        "label_horizon_does_not_match_frozen_sessions"}, "label not one horizon out"
    assert reasons(case(decision_session=sessions[8], label_end_session=sessions[13])) == {
        "label_not_matured_by_snapshot_asof"}, "label beyond the as-of"
    assert reasons(case(target=0)) == {"target_does_not_match_frozen_close_prices"}, "target contradicts closes"
    assert reasons(case(decision_session=sessions[9], label_end_session=sessions[14]), frozen=None) == {
        "label_outside_snapshot_time_boundary"}, "no bars, label beyond the as-of"
    assert reasons(case(), frozen=None, scope={"ticker": "SYN", "horizon": "5D",
                                               "snapshot_id": "snap_1", "asof_timestamp": "unknown"}) == {
        "snapshot_asof_session_missing_or_invalid"}, "no bars, no usable as-of"
    assert reasons(case(target=2), frozen=None) == {"invalid_binary_target"}, "target outside {0,1}"
    assert reasons(case(target_definition="close[t+3] / close[t] - 1 > 0")) == {
        "target_definition_does_not_match_requested_horizon"}, "record horizon differs from the request"
    assert reasons(case(probability=1.5)) == {"invalid_probability_value"}, "probability outside [0,1]"
    assert reasons(case(training_label_end_session=12)) == {"invalid_training_label_boundary"}, "non-string training end"
    assert reasons(case(training_label_end_session="2026-01-01")) == {
        "training_label_end_missing_from_frozen_market_bars"}, "training end off the frozen axis"
    assert reasons(case(training_label_end_session=sessions[5])) == {
        "training_label_not_matured_by_decision"}, "training label matures after the decision"
    assert reasons(case(training_label_end_session=_omit)) == {
        "training_label_boundary_missing_for_prediction"}, "prediction without a training boundary"

    assert reasons(case(calibration_applied=True, calibration_fit_observations=45,
                        calibration_slope=1.0, calibration_intercept=0.0,
                        calibration_label_end_session=sessions[0])) == {
        "raw_probability_missing_for_calibrated_record"}, "calibrated record without a raw output"
    assert reasons(case(calibration_applied=True, raw_probability=0.6,
                        calibration_fit_observations=39, calibration_slope=1.0,
                        calibration_intercept=0.0,
                        calibration_label_end_session=sessions[0])) == {
        "calibration_fit_set_insufficient_or_invalid"}, "calibration fit set below the floor"
    assert reasons(case(calibration_applied=True, raw_probability=0.6,
                        calibration_fit_observations=45,
                        calibration_label_end_session=sessions[0])) == {
        "calibration_coefficients_missing"}, "calibrated record without coefficients"
    assert reasons(case(calibration_applied=True, raw_probability=0.55,
                        calibration_fit_observations=45, calibration_slope=1.0,
                        calibration_intercept=0.0,
                        calibration_label_end_session=sessions[0])) == {
        "calibrated_probability_mismatch"}, "probability is not the recorded mapping's output"
    assert reasons(case(calibration_applied=True, raw_probability=0.6,
                        calibration_fit_observations=45, calibration_slope=1.0,
                        calibration_intercept=0.0)) == {
        "calibration_label_boundary_missing"}, "calibrated record without an origin boundary"
    assert reasons(case(calibration_applied=True, raw_probability=0.6,
                        calibration_fit_observations=45, calibration_slope=1.0,
                        calibration_intercept=0.0,
                        calibration_label_end_session="2026-01-01")) == {
        "calibration_label_end_missing_from_frozen_market_bars"}, "calibration origin off the frozen axis"
    assert reasons(case(calibration_applied=True, raw_probability=0.6,
                        calibration_fit_observations=45, calibration_slope=1.0,
                        calibration_intercept=0.0,
                        calibration_label_end_session=sessions[3])) == {
        "calibration_label_not_matured_before_decision"}, "calibration fitted on the decision session"
    assert reasons(case(calibration_applied=False, raw_probability=0.5)) == {
        "uncalibrated_probability_differs_from_raw"}, "uncalibrated marker with a mapped value"
    assert reasons(case(calibration_applied="yes", raw_probability=0.6)) == {
        "invalid_calibration_marker"}, "marker that is neither true nor false"
    assert reasons(case(raw_probability=0.5)) == {
        "calibrated_probability_without_recorded_origin_boundary"}, "retrofitted calibration"

    _, duplicated = _audit_oos_path({"validation": {"oos_forecasts": [case(), case()]}}, manifest, bars)
    assert duplicated["rejection_reasons"] == {"duplicate_decision_session": 2}, duplicated

    # A ledger that declares the horizon once at the validation level is accepted, and the
    # accepted row is backfilled with that definition.
    backfilled, audit = _audit_oos_path(
        {"validation": {"target_definition": "close[t+5] / close[t] - 1 > 0",
                        "oos_forecasts": [case(target_definition=_omit)]}}, manifest, bars)
    assert audit["valid_samples"] == 1, audit
    assert backfilled[sessions[2]]["target_definition"] == "close[t+5] / close[t] - 1 > 0"


def check_f22_daily_bar_timestamp_precision() -> None:
    """A daily bar's period-start label must not be judged as an incomplete latest observation."""
    from quant_research.data_checks import _check_latest_close, _Findings

    last = "2026-01-02"
    close_at = "2026-01-02T21:00:00+00:00"
    bars = [{"date": last, "close": 101.5}]
    sessions = [{"date": last, "close_at": close_at}]
    block = {"currency": "USD", "actual_source": "synthetic-offline",
             "latest_price_observations": [
                 {"price": 101.5, "session_date": last, "price_basis": "raw",
                  "source": "synthetic-offline"}]}
    asof = dt.datetime(2026, 1, 3, 0, 30, tzinfo=dt.timezone.utc)

    def run(source_iso: str, fetch_iso: str) -> tuple[list[str], dict]:
        findings = _Findings()
        latest = _check_latest_close(
            block, bars, sessions, last, asof, "source_supplied_schedule",
            dt.datetime.fromisoformat(source_iso), dt.datetime.fromisoformat(fetch_iso), findings)
        return findings.reason_codes, latest

    codes, latest = run("2026-01-02T14:30:00+00:00", "2026-01-03T00:30:00+00:00")
    assert "LATEST_OBSERVATION_UNRESOLVED" not in codes, "session-open label was treated as missing data"
    assert latest["kind"] == "calendar_verified", f"expected calendar_verified, got {latest['kind']!r}"

    codes, _ = run("2026-01-02T14:30:00+00:00", "2026-01-02T18:00:00+00:00")
    assert "LATEST_OBSERVATION_UNRESOLVED" in codes, "a fetch before the close must stay blocked"

    codes, _ = run("2025-12-30T00:00:00+00:00", "2026-01-03T00:30:00+00:00")
    assert "LATEST_OBSERVATION_UNRESOLVED" in codes, "a bar older than the session must stay blocked"


def check_f23_window_scaled_support() -> None:
    """Support tiers must stay attainable inside the window actually available."""
    from quant_research.consensus import oos_support_sessions, oos_support_thresholds

    nominal = oos_support_thresholds(5)
    assert nominal == {"strong": 38, "acceptable": 20, "weak": 11}, nominal
    scaled = oos_support_thresholds(5, 220)
    assert scaled == {"strong": 33, "acceptable": 18, "weak": 9}, scaled
    for horizon in (1, 2, 3, 5, 10, 20, 60):
        table = oos_support_thresholds(horizon, 220)
        budget = max(1, 220 // (horizon + 1))
        assert 0 < table["weak"] <= table["acceptable"] <= table["strong"] <= budget, (horizon, table, budget)
    # A longer ledger must never lower the bar below the nominal window.
    assert oos_support_thresholds(5, 500) == nominal, oos_support_thresholds(5, 500)
    # Only a near-full window may borrow the relative tiers. A materially shorter study must keep
    # the nominal bar, otherwise a recent listing would earn `strong` on a handful of samples.
    assert oos_support_thresholds(5, 188) == nominal, oos_support_thresholds(5, 188)
    assert oos_support_thresholds(5, 189) == {"strong": 28, "acceptable": 15, "weak": 8}, oos_support_thresholds(5, 189)
    assert oos_support_sessions([{"oos_evidence_audit": {"supplied_samples": 20}},
                                 {"oos_evidence_audit": {"supplied_samples": 220}},
                                 {"oos_evidence_audit": None}]) == 220
    assert oos_support_sessions([{"oos_evidence_audit": None}]) is None


def check_f24_shared_support_ceiling() -> None:
    """Per-path grading and the composite must read one ledger ceiling, never two."""
    from quant_research.consensus import _confidence_composite, _predictive_evidence_grade

    path = {"oos_validation": {"status": "acceptable"}, "probability_calibration": "calibrated", "reason_codes": []}
    quality = {"paths": {"quant": {"all_evaluable_oos_samples": 220}},
               "algorithm_policy_replay": {"non_overlapping_predicted_dates": 10,
                                           "brier_skill_vs_expanding_base_rate": 0.01}}
    kwargs = {"base_score": 70, "path_assessments": [path], "valid_count": 1,
              "oos_probability_quality": quality, "agreement": "high", "diversity": "high",
              "manifest": {"horizon": "5D"}}
    fallback = _confidence_composite(**kwargs)
    explicit = _confidence_composite(support_sessions=220, **kwargs)
    assert fallback["thresholds"] == explicit["thresholds"], (fallback["thresholds"], explicit["thresholds"])
    # 10 replay samples clear the scaled weak bar (9) but miss the nominal one (11).
    assert explicit["component_scores"]["policy_replay"] == 65.0, explicit["component_scores"]
    short = _confidence_composite(support_sessions=120, **kwargs)
    assert short["component_scores"]["policy_replay"] == 20.0, short["component_scores"]
    assert _predictive_evidence_grade(quality, {"horizon": "5D"}, 220) == _predictive_evidence_grade(
        quality, {"horizon": "5D"}), "grade must use the same ceiling as the composite"


def _momentum_pipeline_run(with_benchmark: bool) -> tuple[float, dict, dict]:
    """Run the offline fixture with or without a declared benchmark series."""
    from quant_research import data_checks
    from quant_research.orchestrator import run_analysis
    from quant_research.snapshot import freeze_snapshot
    from quant_research.stock_data_adapter import StockDataAdapter

    work = Path(tempfile.mkdtemp(prefix=f"selfcheck_f25_{'bench' if with_benchmark else 'plain'}_"))
    library_calendar = data_checks._library_calendar
    data_checks._library_calendar = _offline_library_calendar
    try:
        request = {"ticker": "SYN", "market": "US", "horizon": "5D", "intent": "forecast",
                   "asset_type": "stock", "asof": "2026-01-02T21:00:00Z"}
        domains = _golden_responses(False)["domains"]
        if with_benchmark:
            request["instrument"] = {"benchmark": "BMK"}
            domains["market"]["data"]["benchmark"] = {
                "symbol": "BMK", "selection": "declared", "status": "complete",
                "adjustment": "unadjusted", "bars": _golden_bars(260, seed=97),
            }
        receipt = StockDataAdapter().write_receipt(
            request, domains, work / "collection.json", captured_at_utc="2026-01-03T00:35:00Z")
        snapshot = freeze_snapshot(receipt, work / "snapshots")
        analysis = run_analysis(snapshot, work / "research")
        quant = json.loads((analysis / "quant_result.json").read_text(encoding="utf-8"))
        block = quant["researchers"]["quant"]["validation"]["momentum_20d"]
        report = json.loads((analysis / "report.json").read_text(encoding="utf-8"))
        return float(quant["consensus"]["prob_up"]), block, report
    finally:
        data_checks._library_calendar = library_calendar
        shutil.rmtree(work, ignore_errors=True)


def check_f25_composite_momentum() -> None:
    """F25: composite momentum stays bounded, causal, and outside the probability path."""
    from quant_research.momentum import DEFAULT_CONFIG, _benchmark_series, _evaluate, compute

    dates = [bar["date"] for bar in _golden_bars(90, seed=23)]
    rng = random.Random(41)
    prices = [100.0]
    for _ in range(1, 90):
        prices.append(prices[-1] * math.exp(rng.gauss(0.002, 0.012)))
    benchmark: dict[str, float] = {}
    level = 100.0
    for session in dates:
        level *= math.exp(rng.gauss(0.001, 0.008))
        benchmark[session] = level

    block = compute(prices, dates, benchmark, symbol="SYN", benchmark_symbol="BMK")
    if block["score_status"] != "valid":
        raise AssertionError(f"composite momentum should be valid: {block['status_reasons']}")
    keys = ("risk_adjusted", "regression_trend", "relative_strength")
    if abs(math.fsum(block["components"][key]["weight"] for key in keys) - 1.0) > 1e-12:
        raise AssertionError("momentum weights must sum to one")
    contributions = math.fsum(block["components"][key]["contribution"] for key in keys)
    if abs(contributions - block["momentum_score_20d"]) > 1e-9:
        raise AssertionError("component contributions must sum to the composite score")
    if not -100 < block["momentum_score_20d"] < 100:
        raise AssertionError("composite score must stay strictly inside (-100, 100)")
    if not all(-1 < block["components"][key]["normalized_value"] < 1 for key in keys):
        raise AssertionError("tanh transformed factors must stay strictly inside (-1, 1)")
    expected_direction = "flat" if abs(block["return_20d"]) <= 1e-12 else ("positive" if block["return_20d"] > 0 else "negative")
    if block["momentum_direction"] != expected_direction:
        raise AssertionError("absolute direction must follow the sign of the 20-session return")

    # Leakage: appending future sessions must not change the evaluation at a past index.
    series = _benchmark_series(benchmark)
    past = _evaluate(prices, dates, series, 70, DEFAULT_CONFIG)
    truncated = _evaluate(
        prices[:71], dates[:71], _benchmark_series({k: v for k, v in benchmark.items() if k <= dates[70]}), 70, DEFAULT_CONFIG)
    for key in keys:
        if past["components"][key]["raw_value"] != truncated["components"][key]["raw_value"]:
            raise AssertionError(f"factor {key} changed when future sessions were removed")

    # A missing factor is reported, never renormalised; degenerate data is invalid, not zero.
    missing = compute(prices, dates, None, symbol="SYN")
    if missing["momentum_score_20d"] is not None or missing["score_status"] != "unavailable":
        raise AssertionError("a missing benchmark must leave the score unavailable")
    if "relative_strength:benchmark_unavailable" not in missing["status_reasons"]:
        raise AssertionError("the missing benchmark reason must be explicit")
    gap = dict(benchmark)
    del gap[dates[70]]
    gapped = compute(prices, dates, gap, symbol="SYN")
    if gapped["score_status"] != "unavailable":
        raise AssertionError("an incomplete benchmark window must not be forward filled")
    flat = compute([100.0] * 90, dates, benchmark, symbol="SYN")
    if flat["momentum_score_20d"] is not None or flat["score_status"] != "invalid":
        raise AssertionError("degenerate volatility must be invalid, not a zero score")
    identical = compute([benchmark[session] for session in dates], dates, benchmark, symbol="SYN")
    if identical["momentum_score_20d"] is not None:
        raise AssertionError("a benchmark identical to the target must not yield a score")

    # Isolation: declaring a benchmark must not move the probability by even one bit.
    plain_prob, plain_block, _ = _momentum_pipeline_run(False)
    bench_prob, bench_block, _ = _momentum_pipeline_run(True)
    if plain_prob != bench_prob:
        raise AssertionError(f"probability moved when a benchmark was declared: {plain_prob} != {bench_prob}")
    if plain_block["score_status"] != "unavailable" or plain_block["momentum_score_20d"] is not None:
        raise AssertionError("without a benchmark the momentum score must be unavailable")
    if bench_block["score_status"] != "valid" or bench_block["benchmark"]["symbol"] != "BMK":
        raise AssertionError(f"declared benchmark should produce a valid score: {bench_block['status_reasons']}")


def check_f26_momentum_probability_separation() -> None:
    """F26: momentum keeps its own section and one contract-bound verdict line."""
    from quant_research.delivery import validate_response_text
    from quant_research.report import _momentum_relationship_line as verdict
    from quant_research.report import render_final_response

    prob, block, report = _momentum_pipeline_run(True)
    if block["score_status"] != "valid":
        raise AssertionError(f"fixture momentum should be valid: {block['status_reasons']}")
    text = render_final_response(report)
    validate_response_text(report, text)

    header = "### 动量特征（独立诊断，不是上涨概率）"
    if header not in text:
        raise AssertionError("momentum needs its own labelled section")
    quant_block = text.split("### Quant", 1)[1].split(header, 1)[0]
    if "动量" in quant_block:
        raise AssertionError("momentum must not be mixed into the Quant block")
    line = verdict(report)
    if line not in text:
        raise AssertionError("the momentum-vs-probability verdict must be rendered verbatim")
    if not line.startswith("- 动量与上涨概率的关系："):
        raise AssertionError("the verdict line must keep its fixed prefix")

    def probe(momentum_direction: str, prob_up: float, direction: str) -> str:
        copy = json.loads(json.dumps(report))
        copy["researchers"]["quant"]["validation"]["momentum_20d"]["momentum_direction"] = momentum_direction
        copy["consensus"]["prob_up"] = prob_up
        copy["consensus"]["direction"] = direction
        return verdict(copy)

    divergent = probe("positive", 0.31, "bearish")
    if "背离" not in divergent or "下跌更常见" not in divergent:
        raise AssertionError(f"a positive momentum beside a bearish probability is divergent: {divergent}")
    aligned = probe("positive", 0.72, "bullish")
    if "一致（同向）" not in aligned:
        raise AssertionError(f"same-signed momentum and probability must read as aligned: {aligned}")
    flat = probe("flat", 0.55, "bullish")
    if "动量持平" not in flat:
        raise AssertionError(f"a flat momentum must be named as such: {flat}")
    unavailable = json.loads(json.dumps(report))
    unavailable["researchers"]["quant"]["validation"]["momentum_20d"]["score_status"] = "unavailable"
    if "不可比" not in verdict(unavailable) or "也不做默认替代" not in verdict(unavailable):
        raise AssertionError("an unavailable feature must not be silently treated as neutral")
    missing = json.loads(json.dumps(report))
    missing["researchers"]["quant"]["validation"].pop("momentum_20d", None)
    if "不可比" not in verdict(missing):
        raise AssertionError("an absent feature must not be silently treated as neutral")

    # The verdict is contract-bound: a reworded or dropped line must be rejected.
    def rejected(candidate: str) -> bool:
        try:
            validate_response_text(report, candidate)
        except ValueError:
            return True
        return False

    tampered = (text.replace(line, line.replace("背离", "一致")) if "背离" in line
                else text.replace(line, "- 动量与上涨概率的关系：一致（同向）。"))
    for bad_text, label in ((tampered, "reworded-verdict"),
                            (text.replace(line + "\n", ""), "dropped-verdict")):
        if not rejected(bad_text):
            raise AssertionError(f"{label} was not rejected")

    # Rendering the verdict must not touch the probability.
    if float(report["consensus"]["prob_up"]) != prob:
        raise AssertionError("rendering must not modify the reported probability")


# ---------------------------------------------------------------- F28
def check_f28_selection_statistics() -> None:
    """F28: selection evidence is causal, reproducible, and gated by declared rules."""
    from quant_research.selection import (
        MINIMUM_OBSERVATIONS,
        multiple_testing_disclosure,
        overlapping_ic,
        stability_verdict,
    )

    # A factor that is monotone in the label must read as a strong positive IC, and the
    # estimate must not move when data after the selection boundary is appended.
    labels = [0.01 * index for index in range(80)]
    values = [0.5 * label + 0.001 * ((index % 3) - 1) for index, label in enumerate(labels)]
    indices = list(range(10, 70))
    first = overlapping_ic(values, labels, indices, horizon=5, replicates=200, seed=7)
    if first["status"] != "assessed" or first["rank_ic"] < 0.9:
        raise AssertionError(f"a monotone factor must read as a strong IC: {first}")
    extended = overlapping_ic(values + [9.0] * 40, labels + [9.0] * 40, indices, horizon=5, replicates=200, seed=7)
    if abs(extended["rank_ic"] - first["rank_ic"]) > 1e-12:
        raise AssertionError("appending future data must not move a fixed-index estimate")
    repeat = overlapping_ic(values, labels, indices, horizon=5, replicates=200, seed=7)
    if repeat != first:
        raise AssertionError("the block bootstrap must be deterministic for a fixed seed")

    # Too few labels must be reported, never silently scored.
    thin = overlapping_ic(values, labels, list(range(10, 10 + MINIMUM_OBSERVATIONS - 1)), horizon=5)
    if thin["status"] != "insufficient_samples" or thin["rank_ic"] is not None or thin["interval"] is not None:
        raise AssertionError(f"thin evidence must stay unscored: {thin}")

    # The declared rule: point-estimate floor, then directional stability.
    passing = {"status": "assessed", "rank_ic": 0.15, "sign_consistency": 1.0}
    if not stability_verdict(passing, min_abs_rank_ic=0.02, min_sign_consistency=2 / 3)["passed"]:
        raise AssertionError("a stable factor above the IC floor must pass")
    weak = {"status": "assessed", "rank_ic": 0.01, "sign_consistency": 1.0}
    if stability_verdict(weak, min_abs_rank_ic=0.02, min_sign_consistency=2 / 3)["reason"] != "below_minimum_rank_ic":
        raise AssertionError("a factor under the IC floor must be rejected for that reason")
    unstable = {"status": "assessed", "rank_ic": 0.15, "sign_consistency": 0.34}
    if stability_verdict(unstable, min_abs_rank_ic=0.02, min_sign_consistency=2 / 3)["reason"] != "direction_unstable":
        raise AssertionError("a factor flipping sign across sub-windows must be rejected")
    unscorable = {"status": "insufficient_samples", "rank_ic": None, "sign_consistency": None}
    if stability_verdict(unscorable, min_abs_rank_ic=0.02, min_sign_consistency=2 / 3)["passed"]:
        raise AssertionError("unscored evidence must never pass")

    disclosure = multiple_testing_disclosure({"a": 0.01, "b": 0.4, "c": None})
    if disclosure["used_as_selection_gate"] is not False or disclosure["tested_candidates"] != 2:
        raise AssertionError(f"FDR must stay disclosure-only: {disclosure}")


# ---------------------------------------------------------------- F29
def check_f29_reliability_accounting() -> None:
    """F29: the reliability layer reports samples honestly and cannot lower its own bar."""
    from quant_research import reliability
    from quant_research.reliability import (
        QUALIFICATION_THRESHOLDS,
        logistic_shortfall,
        probabilistic_scores,
        qualification,
        sample_diagnostics,
    )

    # 250 bars, 60 warmup, 5-session horizon: the arithmetic behind the 52-week verdict.
    sample = sample_diagnostics(bar_count=250, warmup=60, horizon=5)
    if (sample["raw_sample_count"], sample["nonoverlap_sample_count"]) != (185, 31):
        raise AssertionError(f"52-week sample accounting changed: {sample}")
    # Without dates the counter falls back to legacy spacing; production passes real dates
    # and gets the anchored count, so both bases are asserted rather than assumed.
    if sample["nonoverlap_count_basis"] != "legacy_spacing":
        raise AssertionError(f"the fallback basis must be named: {sample['nonoverlap_count_basis']}")
    anchored = sample_diagnostics(
        bar_count=250, warmup=60, horizon=5,
        dates=[bar["date"] for bar in _golden_bars(250, seed=11)],
    )
    if anchored["nonoverlap_count_basis"] != "anchored_phase" or anchored["nonoverlap_sample_count"] >= 31:
        raise AssertionError(f"the anchored basis must count fewer labels than legacy: {anchored}")
    if abs(sample["sample_coverage"] - 185 / 190) > 1e-12:
        raise AssertionError("coverage must be matured labels over decision dates")
    if sample["overlapping_observations_are_independent"] is not False:
        raise AssertionError("overlapping labels must never be advertised as independent")
    twenty = sample_diagnostics(bar_count=250, warmup=60, horizon=20)
    if twenty["nonoverlap_sample_count"] != 9:
        raise AssertionError(f"20D non-overlapping count changed: {twenty}")

    # A fitted model needs three disjoint stages, so the requirement is their sum.
    withheld = logistic_shortfall(sample["nonoverlap_sample_count"])
    if withheld["status"] != "not_applicable_insufficient_samples":
        raise AssertionError(f"31 labels cannot host a fitted model: {withheld}")
    if withheld["required_non_overlapping_labels"] != sum(QUALIFICATION_THRESHOLDS["logistic"].values()):
        raise AssertionError("the requirement must be the sum of the stages")
    if withheld["shortfall"] != 120 - 31:
        raise AssertionError("the shortfall must be reported, not hidden")

    # Routing is decided by the declared floors only.
    abstained = qualification(nonoverlap_matured=5, bucket_observations=2, buckets_available=1, validation_predictions=3)
    if abstained["route"] != reliability.ROUTE_ABSTAINED:
        raise AssertionError(f"thin evidence must abstain: {abstained}")
    qualified = qualification(nonoverlap_matured=40, bucket_observations=9, buckets_available=3, validation_predictions=15)
    if qualified["route"] != reliability.ROUTE_LEGACY:
        raise AssertionError(f"sufficient evidence must route to the legacy bucket: {qualified}")
    if qualified["thresholds_adjustable_per_symbol"] is not False:
        raise AssertionError("thresholds must not be adjustable per symbol")

    # Scoring: a perfect forecast scores zero Brier and unit skill; a copy of the base rate
    # scores exactly zero skill, which is the definition a reader expects.
    perfect = [(0, 1.0, 0.5, 1), (1, 0.0, 0.5, 0), (2, 1.0, 0.5, 1)]
    scores = probabilistic_scores(perfect, horizon=5)
    if scores["brier"] != 0.0 or abs(scores["brier_skill_vs_base_rate"] - 1.0) > 1e-12:
        raise AssertionError(f"a perfect forecast must score zero Brier: {scores}")
    copied = [(index, 0.4, 0.4, outcome) for index, outcome in enumerate([1, 0, 1, 0, 1, 0])]
    if abs(probabilistic_scores(copied, horizon=5)["brier_skill_vs_base_rate"]) > 1e-12:
        raise AssertionError("copying the baseline cannot earn skill")
    small = probabilistic_scores(copied, horizon=5)
    if small["brier_difference_interval_status"] != "insufficient_samples":
        raise AssertionError("a short pair series must not claim a bootstrap interval")


# ---------------------------------------------------------------- F27
def check_f27_ab_protocol_statistics() -> None:
    """F27: the A/B protocol decides from the registered rule, not from the data."""
    import momentum_ab as ab
    from quant_research.momentum import compute, score_at

    # Ablation must reproduce the frozen formula with one weight set to zero.
    flat = {"risk_adjusted": 50.0, "regression_trend": 30.0, "relative_strength": 20.0}
    for name, value in ab.ablation_scores(flat).items():
        if abs(value - 100.0) > 1e-9:
            raise AssertionError(f"dropping {name} must preserve a flat composite: {value}")
    partial = {"risk_adjusted": 50.0, "regression_trend": 30.0, "relative_strength": 0.0}
    if abs(ab.ablation_scores(partial)["relative_strength"] - 100.0) > 1e-9:
        raise AssertionError("a zero-weight component must vanish under renormalisation")

    # Bootstrap must be reproducible and bracket the sample mean.
    series = [0.02 * math.sin(index / 7.0) + 0.01 for index in range(120)]
    settings = {"block_length": 6, "iterations": 400, "confidence": 0.95, "seed": 12345}
    first = ab.block_bootstrap_mean_ci(series, **settings)
    second = ab.block_bootstrap_mean_ci(series, **settings)
    if first != second:
        raise AssertionError("the bootstrap must be deterministic for a fixed seed")
    mean = math.fsum(series) / len(series)
    if not first[0] < mean < first[1]:
        raise AssertionError(f"interval {first} must contain the mean {mean}")
    wide = ab.block_bootstrap_mean_ci(series, **{**settings, "block_length": 24})
    if (wide[1] - wide[0]) < (first[1] - first[0]) - 1e-12:
        raise AssertionError("longer blocks must not narrow the interval")

    # Verdict rule: every primary horizon must clear zero; report-only horizons never decide.
    def record(delta, interval):
        return {"status": "evaluated", "delta_ic": delta, "ci_delta": interval}

    better = {5: record(0.017, (0.005, 0.030)), 10: record(0.020, (0.002, 0.039)),
              20: record(0.005, (-0.020, 0.030))}
    if ab.decide_verdict(better) != "composite_better":
        raise AssertionError("both primary horizons clearing zero must read as better")
    marginal = {**better, 10: record(0.020, (-0.0002, 0.039))}
    if ab.decide_verdict(marginal) != "indistinguishable":
        raise AssertionError("an interval containing zero must not count as better")
    worse = {5: record(-0.017, (-0.030, -0.005)), 10: record(-0.020, (-0.039, -0.002))}
    if ab.decide_verdict(worse) != "composite_worse":
        raise AssertionError("both primary horizons below zero must read as worse")
    skipped = {5: {"status": "no_usable_sessions"}, 10: record(0.02, (0.01, 0.03))}
    if ab.decide_verdict(skipped) != "indistinguishable":
        raise AssertionError("an unevaluable primary horizon must fail closed")

    # Durability compares the disclosure slice with the registered verdict.
    same = ab.assess_durability("composite_better", {"status": "evaluated", "verdict": "composite_better"})
    if same != "consistent_across_pool_slices":
        raise AssertionError(f"matching slice verdicts must read as consistent: {same}")
    flipped = ab.assess_durability(
        "composite_better", {"status": "evaluated", "verdict": "indistinguishable"}
    )
    if flipped != "pool_dependent":
        raise AssertionError(f"a flipping disclosure slice must read as pool dependent: {flipped}")
    if ab.assess_durability("composite_better", {"status": "not_applicable"}) != "not_assessed":
        raise AssertionError("a missing disclosure slice must be reported as unassessed")

    # The offline point entry must agree with the report path it was extracted from.
    dates = [bar["date"] for bar in _golden_bars(90, seed=29)]
    rng = random.Random(47)
    prices = [100.0]
    for _ in range(1, 90):
        prices.append(prices[-1] * math.exp(rng.gauss(0.001, 0.011)))
    benchmark: dict[str, float] = {}
    level = 100.0
    for session in dates:
        level *= math.exp(rng.gauss(0.0, 0.009))
        benchmark[session] = level
    block = compute(prices, dates, benchmark, symbol="SYN", benchmark_symbol="BMK")
    point = score_at(prices, dates, benchmark, 89)
    if point["status"] != block["score_status"]:
        raise AssertionError("point entry and report path disagree on status")
    if block["momentum_score_20d"] is None or abs(point["score"] - block["momentum_score_20d"]) > 1e-12:
        raise AssertionError("point entry and report path disagree on the score")

    # Panel subsetting must keep the declared benchmark untouched.
    panel = {
        "dates": dates,
        "closes": {"AAA": prices, "BBB": prices, "XLK": prices},
        "benchmark": benchmark,
        "dropped": {},
        "asset_types": {"AAA": "stock", "BBB": "stock", "XLK": "etf"},
    }
    sliced = ab.subset_panel(panel, {"AAA", "BBB"})
    if set(sliced["closes"]) != {"AAA", "BBB"}:
        raise AssertionError("subsetting must drop only the excluded symbols")
    if sliced["benchmark"] is not benchmark:
        raise AssertionError("subsetting must not rewrite the benchmark series")


# ---------------------------------------------------------------- F30
def check_f30_anchored_sampling() -> None:
    """F30: the sampling phase follows the calendar, not the window start."""
    from quant_research.market_data import (
        anchored_non_overlapping,
        business_day_ordinal,
        non_overlapping,
    )

    # The ordinal must advance by exactly one per weekday and be stable per date.
    if business_day_ordinal("2026-01-05") + 1 != business_day_ordinal("2026-01-06"):
        raise AssertionError("consecutive weekdays must differ by one ordinal")
    if business_day_ordinal("2026-01-09") + 1 != business_day_ordinal("2026-01-12"):
        raise AssertionError("the ordinal must skip the weekend without colliding")

    dates = [bar["date"] for bar in _golden_bars(320, seed=11)]
    for horizon in (1, 5, 20):
        kept, provenance = anchored_non_overlapping(dates, list(range(60, len(dates) - horizon)), horizon)
        gaps = [kept[index + 1] - kept[index] for index in range(len(kept) - 1)]
        if gaps and min(gaps) <= horizon:
            raise AssertionError(f"labels overlap at H={horizon}: min gap {min(gaps)}")
        if provenance["candidate_phase_window_independent"] is not True:
            raise AssertionError(f"phase must be window independent: {provenance}")

    # In this holiday-free fixture, sliding the window must not replace interior dates.
    # This is not a general final-membership guarantee; F33 covers holiday collisions.
    # Both sides use the same window length to keep bounds comparable.
    def sampled(window: list[str]) -> set[str]:
        return {window[i] for i in anchored_non_overlapping(window, list(range(60, len(window) - 5)), 5)[0]}

    base = sampled(dates[0:250])
    for shift in (1, 2, 3):
        window = dates[shift:shift + 250]
        shifted = sampled(window)
        warmup_boundary = window[60]
        # A date may leave only by falling out of the warm-up boundary at the old end;
        # an interior date leaving would be the phase drift this fix removes.
        interior_lost = sorted(date for date in (base - shifted) if date >= warmup_boundary)
        if interior_lost:
            raise AssertionError(
                f"shift {shift} dropped {len(interior_lost)} interior sampled dates, e.g. {interior_lost[:2]}"
            )

    # The legacy rule is the control: it does churn, which is why the fix exists.
    legacy_base = {dates[i] for i in non_overlapping(list(range(60, 250 - 5)), 5)}
    legacy_shift = {dates[1:251][i] for i in non_overlapping(list(range(60, 250 - 5)), 5)}
    if not (legacy_base - legacy_shift):
        raise AssertionError("the legacy spacing is expected to churn; if it does not, this test is stale")

    # Deterministic, and a documented fallback when no candidate clears the phase.
    first = anchored_non_overlapping(dates, list(range(60, 245)), 5)[0]
    second = anchored_non_overlapping(dates, list(range(60, 245)), 5)[0]
    if first != second:
        raise AssertionError("anchored sampling must be deterministic")
    _, provenance = anchored_non_overlapping(["2026-01-05"], [0], 5)
    if provenance.get("candidate_phase_window_independent") is not False:
        raise AssertionError(f"an empty candidate set must fall back and say so: {provenance}")


# ---------------------------------------------------------------- F31
def check_f31_qualification_is_authoritative() -> None:
    """F31: an abstained route cannot carry a reportable probability into consensus."""
    from quant_research import consensus
    from quant_research.contracts import researcher_result
    from quant_research.reliability import ROUTE_ABSTAINED

    # A path that contradicts itself must be rejected by consensus even though its own
    # payload says nothing is wrong with the number itself.
    contradictory = researcher_result(
        "factor", "SYN", "5D", "snap", status="success", prob_up=0.61,
        probability_source="out_of_sample_factor_score_bucket_frequency",
        validation={"qualification": {"route": ROUTE_ABSTAINED}},
    )
    assessment = consensus.forecast_assessment(contradictory, {"horizon": "5D", "ticker": "SYN",
                                                              "snapshot_id": "snap"})
    codes = (assessment.get("reason_codes") or []) + list(assessment.get("forecast_exclusion_reasons") or [])
    joined = " ".join(str(item) for item in codes) + " " + " ".join(
        str(item) for item in (assessment.get("warnings") or [])
    )
    if "QUALIFICATION_CONTRACT_VIOLATION" not in joined:
        raise AssertionError(f"consensus must flag the contradiction: {assessment}")
    if assessment.get("forecast_eligible") is not False:
        raise AssertionError(f"a contradicting path must not stay eligible: {assessment}")

    # A healthy path is untouched by the new contract.
    healthy = researcher_result(
        "factor", "SYN", "5D", "snap", status="success", prob_up=0.61,
        probability_source="out_of_sample_factor_score_bucket_frequency",
        validation={"qualification": {"route": "legacy_bucket"},
                    "leakage_audit": {"features_use_data_through_decision_close_only": True,
                                      "future_returns_used_only_as_labels": True}},
    )
    clean = consensus.forecast_assessment(healthy, {"horizon": "5D", "ticker": "SYN", "snapshot_id": "snap"})
    if "QUALIFICATION_CONTRACT_VIOLATION" in str(clean):
        raise AssertionError("a qualified path must not be flagged")

    # A caller-supplied assessment is not trusted either: a stale flag must not keep an
    # abstained path eligible for probability-quality diagnostics.
    abstained = researcher_result(
        "factor", "SYN", "5D", "snap", status="insufficient_data", prob_up=None,
        validation={"qualification": {"route": ROUTE_ABSTAINED}},
    )
    manifest_stub = {"horizon": "5D", "ticker": "SYN", "snapshot_id": "snap"}
    stale = {"factor": {"forecast_eligible": True, "reporting_status": "pass", "reason_codes": []}}
    merged = consensus.build_consensus([abstained], manifest_stub, assessments=stale)
    if merged["path_assessments"]["factor"]["forecast_eligible"] is not False:
        raise AssertionError(
            f"a supplied assessment overrode the abstained route: {merged['path_assessments']['factor']}"
        )
    if merged["status"] != "unavailable" or merged["prob_up"] is not None:
        raise AssertionError(f"an abstained path must not produce a consensus probability: {merged['status']}")


# ---------------------------------------------------------------- F32
def check_f32_candidate_profiles() -> None:
    """F32: candidates are opt-in, and production defaults are unchanged."""
    from pathlib import Path as _Path

    from quant_research.researchers import factor

    if factor.PRODUCTION_SELECTION_PROFILE != "legacy" or factor.PRODUCTION_BUCKET_MODE != "three":
        raise AssertionError("production defaults must stay legacy selection and three buckets")
    legacy = factor.SELECTION_PROFILES["legacy"]
    restricted = factor.SELECTION_PROFILES["restricted"]
    if (legacy["max_factors"], legacy["max_factors_per_family"]) != (5, 2):
        raise AssertionError(f"the incumbent profile changed: {legacy}")
    if (restricted["max_factors"], restricted["max_factors_per_family"]) != (3, 1):
        raise AssertionError(f"the restricted candidate must cap 3 factors, one per family: {restricted}")
    if restricted["metric"] != "overlapping_block":
        raise AssertionError("the restricted candidate must use the dependency-aware estimator")

    bars = synth_bars(250, seed=11)
    manifest, market = ml_snapshot_stub(bars)
    original = factor.load_snapshot
    factor.load_snapshot = lambda _dir, a=manifest, b=market: (a, b)
    try:
        default = factor.run(_Path("x"))
        explicit = factor.run(_Path("x"), selection_profile="legacy", bucket_mode="three")
        two = factor.run(_Path("x"), selection_profile="legacy", bucket_mode="two")
        restricted_run = factor.run(_Path("x"), selection_profile="restricted", bucket_mode="three")
    finally:
        factor.load_snapshot = original

    if default["prob_up"] != explicit["prob_up"]:
        raise AssertionError("passing the production profile explicitly must not change the result")
    if default["validation"]["bucket_mode"] != "three":
        raise AssertionError("the default bucket mode must be declared as three")
    for result, mode in ((two, "two"), (restricted_run, "three")):
        validation = result["validation"]
        if validation["bucket_mode"] != mode:
            raise AssertionError(f"bucket mode not disclosed: {validation['bucket_mode']}")
        if (validation["qualification"] or {}).get("route") not in {"legacy_bucket", "abstained"}:
            raise AssertionError("a candidate run must still report a qualification route")
    if len(two["validation"]["calibration_bucket_counts"]) != 2:
        raise AssertionError(f"the two-bucket candidate must have two buckets: {two['validation']['calibration_bucket_counts']}")
    if len(restricted_run["validation"]["selected_factors"]) > 3:
        raise AssertionError("the restricted candidate selected more than three factors")
    families = [row["family"] for row in restricted_run["validation"]["selected_factors"]]
    if len(families) != len(set(families)):
        raise AssertionError(f"the restricted candidate allowed two factors from one family: {families}")

    # Interval unit: non-overlapping ledgers must not be blocked at horizon + 1 records.
    from quant_research.reliability import probabilistic_scores

    spaced = [(index, 0.6, 0.5, 1) for index in range(0, 60, 6)]
    scores = probabilistic_scores(spaced, horizon=5)
    if scores["block_length"] != 1:
        raise AssertionError(f"non-overlapping predictions must use a one-record block: {scores['block_length']}")
    if scores["block_length_basis"] != "one_record_because_labels_do_not_overlap":
        raise AssertionError("the interval unit must be explained")
    dense = [(index, 0.6, 0.5, 1) for index in range(30)]
    if probabilistic_scores(dense, horizon=5)["block_length"] != 6:
        raise AssertionError("overlapping daily predictions must keep the horizon block")
    if scores["calibration_slope_is_logistic_calibration_slope"] is not False:
        raise AssertionError("the calibration slope must not be described as a logistic calibration slope")


# ---------------------------------------------------------------- F33
def check_f33_holiday_collisions() -> None:
    """F33: holidays cannot break the session gap, and membership invariance is not claimed.

    The weekday ordinal counts Monday-Friday and does not know exchange holidays, so a
    weekday step of ``horizon + 1`` would collapse to ``horizon`` sessions across a holiday
    and force a drop. ``horizon + 2`` absorbs one holiday. Several holidays can still
    collide, which is why the provenance reports the candidate phase as window independent
    but the final membership as not guaranteed: the greedy gap filter restarts at the left
    boundary, so admitting or dropping an early candidate can change a later one. This
    check proves the non-invariance instead of assuming it, and it compares only dates
    inside both windows - a sliding window also introduces genuinely new dates, and
    counting those as churn would make the assertion pass for the wrong reason.
    """
    from quant_research.market_data import anchored_non_overlapping, business_day_ordinal

    dates = [bar["date"] for bar in _golden_bars(500, seed=7)]
    ordinals = [business_day_ordinal(day) for day in dates]
    horizon, step = 5, 7

    # Remove sessions between two adjacent lattice dates so they become 5 sessions apart.
    # The greedy filter must drop the later candidate instead of emitting a short gap.
    pairs = []
    for left in range(60, len(dates) - horizon):
        if ordinals[left] % step:
            continue
        for right in range(left + 1, min(left + 30, len(dates) - horizon)):
            if ordinals[right] % step == 0:
                pairs.append((left, right, right - left))
                break
    if not pairs:
        raise AssertionError("fixture produced no adjacent lattice pair")
    left, _, span = pairs[0]
    removed = {dates[index] for index in range(left + 1, left + 1 + (span - horizon))}
    sparse = [day for day in dates if day not in removed]
    kept, provenance = anchored_non_overlapping(sparse, list(range(60, len(sparse) - horizon)), horizon)
    gaps = [kept[i + 1] - kept[i] for i in range(len(kept) - 1)]
    if provenance["dropped_for_overlap"] < 1:
        raise AssertionError(f"a collapsed span must drop a candidate: {provenance}")
    if gaps and min(gaps) < horizon + 1:
        raise AssertionError(f"the drop did not restore the session gap: {min(gaps)}")
    if provenance["required_session_gap"] != horizon + 1:
        raise AssertionError(f"the declared gap must match the horizon: {provenance}")

    # The gap must hold at every horizon even when closures are dense.
    dense = [day for index, day in enumerate(dates) if index % 9]
    for probe in (1, 2, 3, 5, 10, 20):
        sampled, info = anchored_non_overlapping(dense, list(range(60, len(dense) - probe)), probe)
        spacing = [sampled[i + 1] - sampled[i] for i in range(len(sampled) - 1)]
        if spacing and min(spacing) < probe + 1:
            raise AssertionError(f"H={probe} violated the gap under dense closures: {min(spacing)}")
        if info["required_session_gap"] != probe + 1:
            raise AssertionError(f"declared gap mismatch at H={probe}: {info}")

    # Non-invariance is disclosed as a conservative claim, not a measured rate. At a
    # realistic exchange density (~4% of sessions closed) membership was invariant on this
    # fixture; the cascade becomes observable only in a sparse-data regime. The probe below
    # uses ~11% missing sessions on purpose, to show the mechanism can propagate, and it
    # compares only dates inside both windows so that genuinely new dates cannot be
    # miscounted as churn.
    sparse_calendar = [day for index, day in enumerate(dates) if index % 9]
    probe_horizon = 20
    base_window = sparse_calendar[0:250]
    base = {base_window[i] for i in anchored_non_overlapping(
        base_window, list(range(60, len(base_window) - probe_horizon)), probe_horizon)[0]}
    entered = left_set = 0
    for shift in (2, 3, 5, 7, 11, 13, 17):
        window = sparse_calendar[shift:shift + 250]
        shifted = {window[i] for i in anchored_non_overlapping(
            window, list(range(60, len(window) - probe_horizon)), probe_horizon)[0]}
        shared = set(base_window[60:len(base_window) - probe_horizon]) & set(
            window[60:len(window) - probe_horizon])
        entered += len((shifted - base) & shared)
        left_set += len((base - shifted) & shared)
    if entered == 0 or left_set == 0:
        raise AssertionError(
            "the cascade probe no longer shows both entry and exit; re-derive the disclosure "
            f"before claiming membership is not invariant (entered={entered}, left={left_set})"
        )
    provenance = anchored_non_overlapping(dates, list(range(60, len(dates) - 5)), 5)[1]
    if provenance["candidate_phase_window_independent"] is not True:
        raise AssertionError("the candidate phase is calendar-anchored and must say so")
    if provenance["final_membership_window_independent"] is not False:
        raise AssertionError("final membership must not claim window independence")


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
    ("F20 pipeline golden regression", check_f20_pipeline_golden),
    ("F21 OOS record audit reasons", check_f21_oos_record_audit),
    ("F22 daily bar timestamp precision", check_f22_daily_bar_timestamp_precision),
    ("F23 window-scaled OOS support", check_f23_window_scaled_support),
    ("F24 shared support ceiling", check_f24_shared_support_ceiling),
    ("F25 composite momentum feature", check_f25_composite_momentum),
    ("F26 momentum probability separation", check_f26_momentum_probability_separation),
    ("F27 A/B protocol statistics", check_f27_ab_protocol_statistics),
    ("F28 selection statistics", check_f28_selection_statistics),
    ("F29 reliability accounting", check_f29_reliability_accounting),
    ("F30 anchored sampling phase", check_f30_anchored_sampling),
    ("F31 qualification is authoritative", check_f31_qualification_is_authoritative),
    ("F32 candidate profiles opt-in", check_f32_candidate_profiles),
    ("F33 holiday collisions", check_f33_holiday_collisions),
]

if __name__ == "__main__":
    for name, check in CHECKS:
        case(name, check)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("\nall checks passed")
