"""Offline self-check battery for the news-focused stock-data runtime.

Runs entirely offline with synthetic data (no network, no yfinance dependency).
Prediction-path fixtures were removed with the v4 forecast removal; the retained
cases cover credential redaction, market validation, Yahoo transport boundaries,
cleanup/retention, and the News/AI-lean delivery contracts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
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


def _news_input(bars: list[dict], articles: list[dict], ticker: str = "MU"):
    from quant_research.news.contracts import NewsInput

    return NewsInput(
        ticker=ticker, market="US", horizon="60D",
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
    core = ("lexicon_diagnostic_direction", "confidence", "event_strength", "market_confirmation", "status")

    risk_ids = {row["event_id"] for row in baseline["lexicon_diagnostic_risks"]}
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
    assert lean["status"] == "partial"
    assert [item["news_id"] for item in lean["bullish"]] == [bullish_id]
    assert [item["news_id"] for item in lean["bearish"]] == [bearish_id]
    assert lean["bullish"][0]["reason"] == "标题称指引上调"
    assert lean["display_only"] is True
    assert lean["coverage"]["candidate_count"] == len(candidates) and lean["coverage"]["assessed_count"] == 3
    for key in core:
        assert scored[key] == baseline[key], f"lean must not change News {key}"
    assert [row["direction"] for row in scored["major_events"]] == [row["direction"] for row in baseline["major_events"]]
    assert {row["event_id"] for row in scored["lexicon_diagnostic_risks"]} == risk_ids
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


def check_t26_denial_is_not_unknown_and_no_number_is_not_negation() -> None:
    """N5: a denial is a fact, not missing evidence; "No. 1" is not a negation."""

    from quant_research.news.classify import direction_for

    if direction_for("Company denies the rumour") != "neutral":
        raise AssertionError("a denial of a rumour is a factual statement, not unknown")
    if direction_for("No. 1 vendor in the segment") == "unknown":
        raise AssertionError("a bare 'No.' must not be read as negation or uncertainty")
    if direction_for("Company may miss estimates") != "unknown":
        raise AssertionError("a genuine hedge must stay unknown")
    if direction_for("Reports unconfirmed bid approach") != "unknown":
        raise AssertionError("an unconfirmed rumour must stay unknown")
    if direction_for("MU misses estimates as demand weak") != "negative":
        raise AssertionError("confirmed negative news must stay negative")


def check_t27_event_timing_and_volatility_regime() -> None:
    """N6/N7/N8: timing bucket, non-directional volatility state, priced-in state."""

    from quant_research.news.timing import (
        aggregate_priced_in,
        event_time_bucket,
        priced_in_assessment,
    )

    closes_map = {"2026-01-05": "2026-01-05T20:00:00Z"}
    assert event_time_bucket("2026-01-05T10:00:00Z", "2026-01-05", closes_map, market="US")["bucket"] == "pre_market"
    assert event_time_bucket("2026-01-05T15:00:00Z", "2026-01-05", closes_map, market="US")["bucket"] == "intraday"
    assert event_time_bucket("2026-01-05T22:00:00Z", "2026-01-05", closes_map, market="US")["bucket"] == "after_close"
    assert event_time_bucket(None, "2026-01-05", closes_map, market="US")["bucket"] == "unknown"

    # HK close-only calendars cannot supply the session open, so no US-clock guess.
    hk_closes = {"2026-01-05": "2026-01-05T08:00:00Z"}
    for published in ("2026-01-05T00:30:00Z", "2026-01-05T03:00:00Z", "2026-01-05T09:00:00Z"):
        hk_unknown = event_time_bucket(published, "2026-01-05", hk_closes, market="HK")
        assert hk_unknown["bucket"] == "unknown", hk_unknown
        assert "US session hours are not applied" in hk_unknown["reason"], hk_unknown
    hk_opens = {"2026-01-05": "2026-01-05T01:30:00Z"}
    assert event_time_bucket("2026-01-05T00:30:00Z", "2026-01-05", hk_closes, market="HK", session_opens=hk_opens)["bucket"] == "pre_market"
    assert event_time_bucket("2026-01-05T03:00:00Z", "2026-01-05", hk_closes, market="HK", session_opens=hk_opens)["bucket"] == "intraday"
    assert event_time_bucket("2026-01-05T09:00:00Z", "2026-01-05", hk_closes, market="HK", session_opens=hk_opens)["bucket"] == "after_close"

    # 25 quiet sessions, a +2% event session, then a flat continuation window.
    closes = [100.0]
    for index in range(24):
        closes.append(closes[-1] * (1 + (0.001 if index % 2 else -0.001)))
    closes.append(closes[-1] * 1.02)
    closes.extend([closes[-1]] * 6)
    reaction = {"status": "confirmed_positive", "daily_close_return": 0.02}
    priced_in = priced_in_assessment(
        reaction=reaction, closes=closes, session_index=25, event_direction="positive", horizon=5,
    )
    assert priced_in["state"] == "likely_priced_in", priced_in
    assert priced_in["probability_effect"] == "none"
    early = priced_in_assessment(
        reaction=reaction, closes=closes[:28], session_index=25, event_direction="positive", horizon=5,
    )
    assert early["state"] == "not_assessable", early
    aggregate = aggregate_priced_in([
        {"already_priced_in": priced_in} for _ in range(3)
    ])
    assert aggregate["state"] == "likely_priced_in", aggregate


def check_t28_ai_lean_identity_fields_cannot_be_forged() -> None:
    """N9: a supplied headline/source/url must match the analysed record."""

    from quant_research.news import AI_ASSESSMENT_SCHEMA_VERSION, analyze_news

    bars = synth_bars(60, seed=7)
    news_input = _news_input(bars, _f17_articles(bars))
    baseline = analyze_news(news_input)
    candidate = baseline["ai_lean_candidates"][0]
    artifact = {
        "schema_version": AI_ASSESSMENT_SCHEMA_VERSION, "ticker": "MU",
        "asof_timestamp": news_input.asof_timestamp,
        "items": [{
            "news_id": candidate["news_id"], "lean": "bullish", "reason": "标题上调",
            "headline": "Forged headline that was never published",
        }],
    }
    rejected = analyze_news(news_input, None, artifact)
    assert rejected["ai_lean"]["status"] == "invalid", rejected["ai_lean"]
    good = analyze_news(news_input, None, {
        **artifact,
        "items": [{"news_id": candidate["news_id"], "lean": "bullish", "reason": "标题上调",
                   "headline": candidate["headline"]}],
    })
    assert good["ai_lean"]["status"] == "partial", good["ai_lean"]


def check_t41_partial_ai_news_can_be_delivered_when_disclosed() -> None:
    """T41: partial coverage is deliverable only with reconciling counts and a warning."""
    from quant_research.delivery import require_ai_news_coverage

    candidates = [{"news_id": "n1"}, {"news_id": "n2"}]

    def news(status: str, assessed: int, items: int, warnings: list[str] | None = None) -> dict:
        return {"ai_lean_candidates": candidates, "warnings": warnings or [],
                "ai_lean": {"status": status,
                            "coverage": {"candidate_count": len(candidates), "assessed_count": assessed},
                            "items": [{"news_id": f"n{index + 1}"} for index in range(items)]}}

    require_ai_news_coverage(news("assessed", 2, 2))
    require_ai_news_coverage(news("partial", 1, 1, ["1 candidate not assessed"]))
    for case, label in (
        (news("partial", 1, 1), "partial without a disclosed reason"),
        (news("assessed", 1, 1), "assessed with a missing item"),
        (news("not_assessed", 0, 0), "unassessed artifact"),
        (news("partial", 3, 3, ["x"]), "assessed count above the candidate count"),
    ):
        try:
            require_ai_news_coverage(case)
        except ValueError:
            continue
        raise AssertionError(f"{label} was accepted")


def check_t42_presentation_warnings_do_not_degrade_news() -> None:
    """T42: truncation is disclosed without pretending the evidence set shrank."""
    from quant_research.news.pipeline import _finalize_warnings
    from quant_research.news.status import warning_details, warning_severity_counts

    warnings = ["12 news headline(s) were truncated to 400 characters",
                "3 news record(s) were dropped before normalization"]
    details = warning_details(warnings)
    counts = warning_severity_counts(warnings)
    if details[0]["severity"] != "presentation" or details[1]["severity"] != "evidence_affecting":
        raise AssertionError(details)
    if counts != {"evidence_affecting": 1, "presentation": 1}:
        raise AssertionError(counts)
    if warning_severity_counts(["3 news headline(s) were truncated to 400 characters"])["evidence_affecting"]:
        raise AssertionError("a truncation-only warning must not degrade the status")

    result = {"status": "complete"}
    _finalize_warnings(result, ["2 news headline(s) were truncated to 400 characters"], assessed_any=True)
    if result["status"] != "complete":
        raise AssertionError("presentation-only warnings must keep the assessed status complete")
    result = {"status": "complete"}
    _finalize_warnings(result, ["2 canonical news headline(s) were not assessed by the host AI"], assessed_any=True)
    if result["status"] != "partial":
        raise AssertionError("unassessed canonical headlines must degrade the assessed status")


# ---------------------------------------------------------------- v4 contracts
def _fake_delivery_result(news_status: str, **overrides) -> dict:
    result = {
        "snapshot": {
            "ticker": "SYN", "market": "US", "asset_type": "stock",
            "asof_timestamp": "2026-01-02T21:00:00Z",
            "policy": {"policy_version": "news_60d_v1"},
            "snapshot_id": "SYN_TEST",
        },
        "news_result": {"status": news_status},
    }
    result.update(overrides)
    return result


def check_t25_news_partial_is_assessed_not_unassessed() -> None:
    """Partial is assessed evidence and the warning count must be shown."""

    from quant_research.delivery import validate_response_text
    from quant_research.news import news_assessed, news_status_label
    from quant_research.report import render_final_response, required_response_lines

    assert news_assessed("partial") and news_assessed("complete") and news_assessed("valid")
    assert not news_assessed("not_assessed") and not news_assessed(None) and not news_assessed("corrupted")
    assert news_status_label("partial") == "partial"

    partial = _fake_delivery_result("partial")
    partial["news_result"].update({"warning_count": 3, "warnings": ["a", "b", "c"],
                                   "overall_direction": "mixed",
                                   "expected_volatility_regime": {"regime": "elevated"}})
    line = required_response_lines(partial)["news"]
    if "已采集" not in line or "3 条警告" not in line or "AI逐条未评估" not in line:
        raise AssertionError(f"collection and AI assessment must have distinct statuses: {line}")
    if "未提供" in line:
        raise AssertionError(f"collected evidence must not be described as absent: {line}")
    text = render_final_response(partial)
    if "新闻：未提供" in text:
        raise AssertionError("a collected run must not be described as absent")
    validate_response_text(partial, text)

    absent = _fake_delivery_result("not_assessed")
    line = required_response_lines(absent)["news"]
    if "未提供" not in line:
        raise AssertionError(f"a genuinely unassessed News stage must say so: {line}")
    validate_response_text(absent, render_final_response(absent))


def check_t32_headline_newlines_cannot_inject_report_lines() -> None:
    """Provider text stays one line and is escaped in Markdown."""

    from quant_research.news.normalize import normalize_articles
    from quant_research.report import render_markdown

    articles, _excluded, _warnings = normalize_articles(
        {"articles": [{
            "title": "MU raises guidance\n## Injected heading\n- injected item",
            "published_at": "2026-01-05T13:00:00Z", "source": "Reuters",
            "url": "https://example.com/a", "tickers": ["MU"],
        }]},
        asof_timestamp="2026-01-09T00:00:00Z",
    )
    assert articles, "the fixture article must survive normalization"
    assert "\n" not in articles[0]["title"], articles[0]["title"]

    result = _fake_delivery_result("complete")
    result["news_result"] = {
        "status": "complete", "warning_count": 0, "overall_direction": "positive",
        "major_events": [{"headline": "MU raises guidance\n## Injected heading", "direction": "positive",
                          "eligible_evidence": True, "age_sessions": 1}],
    }
    rendered = render_markdown(result)
    if any(line.strip().startswith("## Injected heading") for line in rendered.splitlines()):
        raise AssertionError("a headline must not be able to create a new Markdown heading")


def check_t35_ai_news_counts_and_links() -> None:
    """Every canonical headline is offered to AI; direct counts and major links are auditable."""

    from quant_research.news import AI_ASSESSMENT_SCHEMA_VERSION, analyze_news
    from quant_research.delivery import require_ai_news_coverage
    from quant_research.report import render_final_response, render_markdown, required_response_lines

    bars = synth_bars(60, seed=7)
    articles = _f17_articles(bars) + [{
        "article_id": "macro-1", "title": "Fed rate decision this week", "url": "https://example.com/macro",
        "source": "Wire", "source_type": "news", "record_type": "unknown",
        "published_at": f"{bars[-1]['date']}T14:00:00Z", "tickers": [], "summary": None,
    }, {
        "article_id": "roundup-1", "title": "Weekly market roundup", "url": "https://example.com/roundup",
        "source": "Wire", "source_type": "news", "record_type": "unknown",
        "published_at": f"{bars[-1]['date']}T15:00:00Z", "tickers": [], "summary": None,
    }]
    news_input = _news_input(bars, articles)
    baseline = analyze_news(news_input)
    candidates = baseline["ai_lean_candidates"]
    assert any(item["record_kind"] == "unclassified" for item in candidates)
    items = []
    for candidate in candidates:
        headline = candidate["headline"]
        if "raises guidance" in headline:
            lean, relevance, importance = "bullish", "direct", "major"
        elif "misses estimates" in headline:
            lean, relevance, importance = "bearish", "direct", "major"
        elif "Fed rate" in headline:
            lean, relevance, importance = "bullish", "indirect", "major"
        else:
            lean, relevance, importance = "neutral", "unclear", "routine"
        items.append({"news_id": candidate["news_id"], "lean": lean, "reason": "标题直接陈述该事项",
                      "target_relevance": relevance, "importance": importance, "evidence_grade": "headline_only"})
    assessed = analyze_news(news_input, None, {"schema_version": AI_ASSESSMENT_SCHEMA_VERSION,
                                                "ticker": "MU", "asof_timestamp": news_input.asof_timestamp,
                                                "items": items})
    ai = assessed["ai_lean"]
    assert ai["status"] == "assessed" and ai["coverage"]["assessed_count"] == len(candidates)
    counts = ai["coverage"]["direct_label_counts"]
    assert counts["bullish"] == 1 and counts["bearish"] == 1, counts
    assert len(ai["major_direct_items"]) == 2
    require_ai_news_coverage(assessed)
    try:
        require_ai_news_coverage(baseline)
    except ValueError:
        pass
    else:
        raise AssertionError("delivery must reject collected headlines without AI assessment")
    result = _fake_delivery_result("complete")
    result["news_result"] = assessed
    line = required_response_lines(result)["news"]
    assert "利多1条" in line and "利空1条" in line and "AI逐条已完成" in line, line
    final = render_final_response(result)
    report = render_markdown(result)
    assert "https://example.com/a1" in final and "https://example.com/a2" in final
    assert "https://example.com/macro" not in final
    assert "https://example.com/macro" in report


def check_t49_news_coverage_is_disclosed() -> None:
    """A seven-day sample must disclose its real span, not claim a covered 90 days."""
    from quant_research.delivery import validate_response_text
    from quant_research.news import analyze_news
    from quant_research.report import render_final_response, required_response_lines

    bars = synth_bars(60, seed=7)
    last = dt.date.fromisoformat(bars[-1]["date"])
    first = last - dt.timedelta(days=6)
    articles = [{
        "article_id": "short-1", "title": "MU raises guidance on strong demand",
        "url": "https://example.com/short-1", "source": "Reuters", "source_type": "news",
        "record_type": "news", "published_at": f"{first.isoformat()}T13:00:00Z",
        "tickers": ["MU"], "summary": None,
    }, {
        "article_id": "short-2", "title": "MU misses estimates as demand weak",
        "url": "https://example.com/short-2", "source": "Bloomberg", "source_type": "news",
        "record_type": "news", "published_at": f"{last.isoformat()}T13:00:00Z",
        "tickers": ["MU"], "summary": None,
    }]
    news = analyze_news(_news_input(bars, articles))
    coverage = news["coverage"]
    if coverage.get("observed_publication_span_days") != 7 or coverage.get("coverage_short") is not True:
        raise AssertionError(f"a seven-calendar-day sample must be disclosed as short: {coverage}")
    if not any("NEWS_COVERAGE_SHORT" in warning for warning in news.get("warnings") or []):
        raise AssertionError("short coverage must emit NEWS_COVERAGE_SHORT")
    if news.get("status") != "partial":
        raise AssertionError(f"short coverage must degrade to partial: {news.get('status')}")

    result = _fake_delivery_result("complete")
    result["news_result"] = news
    line = required_response_lines(result)["news"]
    if first.isoformat() not in line or last.isoformat() not in line or "（7 天" not in line:
        raise AssertionError(f"the news line must carry the actual observed span: {line}")
    text = render_final_response(result)
    validate_response_text(result, text)
    try:
        validate_response_text(result, text + "\n新闻：已采集（90天已覆盖）\n")
    except ValueError:
        pass
    else:
        raise AssertionError("claiming a 90-day covered window must fail validation")


def check_t50_ai_lean_is_the_only_user_direction() -> None:
    """A bullish lexicon must not override an AI bearish judgement."""
    from quant_research.news import AI_ASSESSMENT_SCHEMA_VERSION, analyze_news
    from quant_research.report import required_response_lines

    bars = synth_bars(60, seed=7)
    last = bars[-1]["date"]
    articles = [{
        "article_id": "pos-1", "title": "MU raises guidance on strong demand",
        "url": "https://example.com/pos-1", "source": "Reuters", "source_type": "news",
        "record_type": "news", "published_at": f"{last}T13:00:00Z",
        "tickers": ["MU"], "summary": None,
    }]
    news_input = _news_input(bars, articles)
    baseline = analyze_news(news_input)
    if baseline["lexicon_diagnostic_direction"] != "positive":
        raise AssertionError(f"the lexicon fixture should be bullish: {baseline['lexicon_diagnostic_direction']}")
    if baseline["overall_direction"] is not None:
        raise AssertionError("without AI, the user-facing direction must be null, not the lexicon value")
    if "catalysts" in baseline or "risks" in baseline:
        raise AssertionError("lexicon catalysts/risks must be named as diagnostics")

    candidate = baseline["ai_lean_candidates"][0]
    assessed = analyze_news(news_input, None, {
        "schema_version": AI_ASSESSMENT_SCHEMA_VERSION, "ticker": "MU",
        "asof_timestamp": news_input.asof_timestamp,
        "items": [{"news_id": candidate["news_id"], "lean": "bearish", "reason": "AI 判断为利空",
                   "target_relevance": "direct", "importance": "major"}],
    })
    if assessed["overall_direction"] != "bearish":
        raise AssertionError(f"the user-facing direction must follow AI, not the lexicon: {assessed['overall_direction']}")
    if assessed["lexicon_diagnostic_direction"] != "positive":
        raise AssertionError("the lexicon diagnostic must stay separate and unchanged")

    result = _fake_delivery_result("complete")
    result["news_result"] = assessed
    line = required_response_lines(result)["news"]
    if "利多0条" not in line or "利空1条" not in line:
        raise AssertionError(f"the delivered counts must come from AI lean: {line}")


def check_forecast_removed() -> None:
    """Old forecast calls must fail with FORECAST_REMOVED and a non-zero exit."""
    import importlib.util

    from quant_research.contracts import normalize_request
    from quant_research.forecast_policy import PolicyError

    for request in ({"ticker": "AAPL", "intent": "forecast"},
                    {"ticker": "AAPL", "horizon": "5D"}):
        try:
            normalize_request(request)
        except PolicyError as exc:
            assert exc.code == "FORECAST_REMOVED", exc.code
        else:
            raise AssertionError(f"legacy forecast request was accepted: {request}")
    assert normalize_request({"ticker": "AAPL"})["intent"] == "news"

    cli_path = Path(__file__).with_name("quant_research.py")
    spec = importlib.util.spec_from_file_location("quant_research_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    work = Path(tempfile.mkdtemp(prefix="selfcheck_forecast_removed_"))
    try:
        request_path = work / "request.json"
        output_path = work / "normalized.json"
        request_path.write_text(json.dumps({"ticker": "AAPL", "intent": "forecast"}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["request", "--input", str(request_path), "--output", str(output_path)])
        if code == 0:
            raise AssertionError("intent=forecast must return a non-zero exit code")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_news_collect_fixtures() -> None:
    """FIX-13: recorded fixtures for SEC/IR/Finnhub parsing, window and status."""
    import unittest

    tests_dir = str(SCRIPTS.parent / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_news_collect  # noqa: PLC0415 - fixture suite lives next to the package

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_news_collect)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not outcome.wasSuccessful():
        raise AssertionError(stream.getvalue()[-1200:])


def check_news_verify_fixtures() -> None:
    """FIX-14: TinyFish verification fixtures, 30-item cap and Fetch whitelist."""
    import unittest

    tests_dir = str(SCRIPTS.parent / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_news_verify  # noqa: PLC0415 - fixture suite lives next to the package

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_news_verify)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not outcome.wasSuccessful():
        raise AssertionError(stream.getvalue()[-1200:])


def check_news_summary_fixtures() -> None:
    """FIX-15/19: strength_v2 matrix, delivery mutations and post_hoc rejection."""
    import unittest

    tests_dir = str(SCRIPTS.parent / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_news_summary  # noqa: PLC0415 - fixture suite lives next to the package

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_news_summary)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not outcome.wasSuccessful():
        raise AssertionError(stream.getvalue()[-1200:])


def check_descriptive_window_fixtures() -> None:
    """FIX-21: the news snapshot keeps at most the latest 52 weeks of bars."""
    import unittest

    tests_dir = str(SCRIPTS.parent / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_snapshot_window  # noqa: PLC0415 - fixture suite lives next to the package

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_snapshot_window)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not outcome.wasSuccessful():
        raise AssertionError(stream.getvalue()[-1200:])


def check_yahoo_collect_metadata() -> None:
    """FIX-16: GOOGL/QQQ currency evidence, missing unit not ineligible, CALENDAR_LIB_MISSING."""
    import unittest

    tests_dir = str(SCRIPTS.parent / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_yahoo_collect  # noqa: PLC0415 - fixture suite lives next to the package

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_yahoo_collect)
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not outcome.wasSuccessful():
        raise AssertionError(stream.getvalue()[-1200:])


def check_docs_convergence() -> None:
    """FIX-16: entry docs route to the four task classes and contain no dead links."""
    root = SCRIPTS.parent
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    lowered = skill.casefold()
    for forbidden in ("duck" + "duckgo", "bai" + "du", "ji" + "na", "news" + "now", "ak" + "share",
                      "yahoo " + "rss", "economist " + "rss"):
        if forbidden in lowered:
            raise AssertionError(f"SKILL.md still routes to {forbidden}")
    for path in ("references/news-analysis.md", "references/news-impact-rubric.md",
                 "references/source-policies.md", "references/collection-adapter.md",
                 "references/snapshot-schema.md", "references/upstream-provenance.md"):
        if not (root / path).is_file():
            raise AssertionError(f"missing converged doc: {path}")
    docs = "\n".join([(root / "SKILL.md").read_text(encoding="utf-8"),
                       (root.parent / "README.md").read_text(encoding="utf-8"),
                       (root / "references" / "collection-adapter.md").read_text(encoding="utf-8")])
    for dead in ("probability-policy.md", "quant-paths.md", "reliability-gates.md",
                 "forward-ledger.md", "consensus-protocol.md", "momentum-model.md"):
        if dead in docs:
            raise AssertionError(f"dead reference remains: {dead}")
    for source in ("SEC EDGAR", "IR RSS", "Finnhub", "TinyFish"):
        if source not in (root / "references" / "source-policies.md").read_text(encoding="utf-8"):
            raise AssertionError(f"source-policies.md is missing {source}")
    if re.search(r"(?<![\w/])python \S+stock-data/", skill):
        raise AssertionError("SKILL.md command examples must use python3")


def check_t43_registry_accounts_for_every_check() -> None:
    """No check function may exist without being registered or explicitly retired."""
    defined = {name for name, value in globals().items() if name.startswith("check_") and callable(value)}
    registered = [fn.__name__ for _, fn in CHECKS]
    if len(registered) != len(set(registered)):
        raise AssertionError("a check is registered more than once")
    retired = set(RETIRED_CHECKS)
    if set(registered) & retired:
        raise AssertionError("a check cannot be both registered and retired")
    if any(not str(reason).strip() for reason in RETIRED_CHECKS.values()):
        raise AssertionError("every retired check needs a reason")
    missing = sorted(defined - set(registered) - retired)
    if missing:
        raise AssertionError(f"defined but neither registered nor retired: {missing}")


def check_t44_documented_versions_match_the_runtime() -> None:
    """SKILL, package and README must not advertise different versions."""
    root = SCRIPTS.parent
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    metadata = re.search(r"metadata:\s*\{(.*?)\}", skill, re.DOTALL)
    if not metadata:
        raise AssertionError("SKILL.md has no metadata block")
    skill_version = re.search(r'(?:^|[\s,{])version:\s*"([^"]+)"', metadata.group(1))
    if not skill_version:
        raise AssertionError("SKILL.md has no metadata version")
    runtime_binding = re.search(r'runtime:\s*"([^"]+)"', metadata.group(1))
    if not runtime_binding:
        raise AssertionError("SKILL.md does not declare the runtime version it ships")
    from quant_research import __version__

    if runtime_binding.group(1) != __version__:
        raise AssertionError(f"SKILL runtime {runtime_binding.group(1)} != package {__version__}")
    if skill_version.group(1) != "4.0.0":
        raise AssertionError(f"SKILL version {skill_version.group(1)} != 4.0.0")

    readme_path = root.parent / "README.md"
    if not readme_path.is_file():
        return
    documented = re.search(r"当前 Skill `([^`]+)`，research runtime `([^`]+)`", readme_path.read_text(encoding="utf-8"))
    if not documented:
        raise AssertionError("README.md has no version line")
    if documented.group(1) != skill_version.group(1):
        raise AssertionError(f"README Skill {documented.group(1)} != SKILL {skill_version.group(1)}")
    if documented.group(2) != __version__:
        raise AssertionError(f"README runtime {documented.group(2)} != package {__version__}")


RETIRED_CHECKS: dict[str, str] = {}

CHECKS = [
    ("F03 credential redaction", check_f03_redaction),
    ("F07 malformed market fields", check_f07_malformed_fields),
    ("F08 preflight and fallback counting", check_f08_preflight_and_counting),
    ("F11 options expiry Unix seconds", check_f11_options_expiry_unix_seconds),
    ("F12 cache_dir PathLike normalized", check_f12_cache_dir_pathlike),
    ("F17 host AI lean layer", check_f17_ai_lean_layer),
    ("F18 orphan scratch sweep", check_f18_orphan_sweep),
    ("F19 retention and cache pruning", check_f19_retention_gc),
    ("F22 daily bar timestamp precision", check_f22_daily_bar_timestamp_precision),
    ("T25 news partial is assessed", check_t25_news_partial_is_assessed_not_unassessed),
    ("T26 denial is not unknown", check_t26_denial_is_not_unknown_and_no_number_is_not_negation),
    ("T27 event timing and volatility regime", check_t27_event_timing_and_volatility_regime),
    ("T28 AI lean identity cannot be forged", check_t28_ai_lean_identity_fields_cannot_be_forged),
    ("T32 headline cannot inject report lines", check_t32_headline_newlines_cannot_inject_report_lines),
    ("T35 AI news counts and links", check_t35_ai_news_counts_and_links),
    ("T41 partial AI news delivery", check_t41_partial_ai_news_can_be_delivered_when_disclosed),
    ("T42 presentation warnings do not degrade news", check_t42_presentation_warnings_do_not_degrade_news),
    ("FIX13 news collector fixtures", check_news_collect_fixtures),
    ("FIX14 news verification fixtures", check_news_verify_fixtures),
    ("FIX15 news summary fixtures", check_news_summary_fixtures),
    ("FIX16 yahoo collection metadata", check_yahoo_collect_metadata),
    ("FIX21 descriptive window fixtures", check_descriptive_window_fixtures),
    ("FIX16 docs convergence", check_docs_convergence),
    ("T43 registry accounts for every check", check_t43_registry_accounts_for_every_check),
    ("T44 documented versions match the runtime", check_t44_documented_versions_match_the_runtime),
    ("T49 news coverage is disclosed", check_t49_news_coverage_is_disclosed),
    ("T50 AI lean is the only user direction", check_t50_ai_lean_is_the_only_user_direction),
    ("FIX12 forecast removed", check_forecast_removed),
]

if __name__ == "__main__":
    for name, check in CHECKS:
        case(name, check)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        raise SystemExit(1)
    print("\nall checks passed")
