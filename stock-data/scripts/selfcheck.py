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
        "report.json": "33fd2d3d2de4a8d609dd64241fb77e0d4aeb9b587d9ca426751ce1365adce29c",
        "quant_result.json": "bc368d58fc53668a3957d20d25f198f799cfd68359d0f1cb13bd49a149c9e7f4",
        "news_result.json": "44bfc39702eb43481a15ae736378576384f1ea4b59b84d6a1138111bc7521171",
        "agent_summary.json": "d7aac30a17e2f5150e647c1e8cc3a97b82772d7a61b1009a04727d953a1b28f6",
        "final_response.md": "190dba292e90bca7704e512492eaf4ddc136c65940f8fb3110272f2d55a95d3b",
    },
    "news": {
        "report.json": "1e216d9e7fd242a23587febd37baed262a8e7fed0a377861deceb03f5df6b6de",
        "quant_result.json": "414193ebba75e79dc310bc1204c2d043e2a17ea37c1a7afc6e457d5ab0ef8137",
        "news_result.json": "23b3cac4bab741d4cc621731e4b5ca0955f7e18f93c6c362388b8df5a343f02a",
        "agent_summary.json": "6eb3281fd5425f788250f079444692009ace3414afdfab2583551bf7a48b1e35",
        "final_response.md": "5e5141885de1cf5fe0f6ac46a0c1c10258dcfeafcc1ca4b7413db585fd293f22",
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
        "quant.consensus.prob_up": 0.5583357792180278,
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
        "quant.consensus.prob_up": 0.5583357792180278,
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
]

if __name__ == "__main__":
    for name, check in CHECKS:
        case(name, check)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("\nall checks passed")
