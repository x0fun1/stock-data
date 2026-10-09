"""Automatic legacy Yahoo fallback mappings; network-free contract tests."""
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stock-data/scripts"))
from yahoo_compat_fallback import _route, fallback
from yfinance_provider import YahooProvider


def test_history_fallback_normalizes_local_session_and_preserves_unknowns():
    legacy = ModuleType("global_stock_data")
    legacy.stock_kline_yahoo = lambda *args, **kwargs: {
        "bars": [{"date": "2024-01-02", "timestamp_utc": "2024-01-02T05:00:00Z",
                  "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}],
        "meta": {"symbol": "AAPL", "currency": "USD", "exchangeTimezoneName": "America/New_York"},
        "adjclose": [{"adjclose": [9]}], "events": {},
    }
    with patch.dict(sys.modules, {"global_stock_data": legacy}):
        result = fallback("yahoo_history", {
            "symbol": "AAPL", "start": "2024-01-02", "end": "2024-01-03",
            "interval": "1d", "price_basis": "adjusted"}, {"category": "NetworkError"})
    assert result["fallback_used"] is True
    assert result["fallback_route"] == "stock_kline_yahoo"
    assert result["fallback_reason"]["category"] == "NetworkError"
    assert result["source_timestamp_kind"] == "bar_label_not_trade_time"
    assert result["unit"] is None
    assert result["data"]["bars"][0]["date"] == "2024-01-02"
    assert result["data"]["bars"][0]["close"] == 9
    assert result["status"] == "partial"


def test_news_fallback_maps_only_legacy_publication_and_ticker_evidence():
    legacy = ModuleType("global_stock_data")
    legacy.stock_news = lambda keyword, count: [{
        "article_id": "story-1", "title": "Reported result", "publisher": "Publisher",
        "link": "https://news.example/story-1", "publish_time": 1704067200,
        "tickers": ["AAPL"],
    }]
    with patch.dict(sys.modules, {"global_stock_data": legacy}):
        result = fallback("yahoo_news", {"symbol": "AAPL", "count": 5}, {"category": "DataUnavailable"})
    article = result["data"]["articles"][0]
    assert result["fallback_used"] is True
    assert result["fallback_route"] == "stock_news"
    assert result["status"] == "partial"
    assert result["fetched_at_utc"].endswith("Z")
    assert article["tickers"] == ["AAPL"]
    assert article["published_at"] == "2024-01-01T00:00:00Z"


def test_generic_fallback_records_route_and_actual_search_source():
    legacy = ModuleType("global_stock_data")
    calls = []

    def quote_summary(**kwargs):
        calls.append(kwargs)
        return {"price": {"regularMarketPrice": {"raw": 10}}}

    legacy.FUNCTIONS = {"yahoo_quote_summary": quote_summary}
    with patch.dict(sys.modules, {"global_stock_data": legacy}):
        quote = fallback("yahoo_quote", {"symbol": "AAPL"}, {"category": "NetworkError"})
    assert calls == [{"symbol": "AAPL", "modules": ["price", "summaryDetail", "financialData", "defaultKeyStatistics"]}]
    assert quote["fallback_attempted"] is True
    assert quote["fallback_used"] is True
    assert quote["fallback_route"] == "yahoo_quote_summary"
    assert quote["fallback_reason"]["category"] == "NetworkError"

    legacy.FUNCTIONS = {"stock_search": lambda **kwargs: [{"symbol": "AAPL"}]}
    with patch.dict(sys.modules, {"global_stock_data": legacy}):
        search = fallback("yahoo_search", {"query": "Apple", "count": 3}, {"category": "DataUnavailable"})
    assert search["actual_source"] == "Eastmoney search"
    assert search["fallback_route"] == "stock_search"
    assert search["warnings"]


def test_failed_legacy_attempt_records_target_route():
    legacy = ModuleType("global_stock_data")
    legacy.FUNCTIONS = {"yahoo_quote_summary": lambda **kwargs: (_ for _ in ()).throw(RuntimeError("private detail"))}
    with patch.dict(sys.modules, {"global_stock_data": legacy}):
        result = fallback("yahoo_quote", {"symbol": "AAPL"}, {"category": "NetworkError"})
    assert result["status"] == "error"
    assert result["fallback_attempted"] is True
    assert result["fallback_used"] is False
    assert result["fallback_route"] == "yahoo_quote_summary"
    assert result["error"]["category"] == "LegacyFallbackFailed"
    assert "private detail" not in result["error"]["message"]


def test_unmappable_new_capability_reports_missing_legacy_route():
    result = fallback("yahoo_screen", {"query": {"operator": "EQ", "operands": ["region", "us"]}},
                      {"category": "NetworkError"})
    assert result["status"] == "error"
    assert result["fallback_used"] is False
    assert result["fallback_attempted"] is True
    assert result["fallback_route"] is None
    assert result["error"]["category"] == "NoLegacyRoute"

    typed_search = fallback("yahoo_search", {"query": "Apple", "lookup_type": "equity"},
                            {"category": "UnsupportedCapability"})
    assert typed_search["error"]["category"] == "NoLegacyRoute"
    assert typed_search["fallback_route"] is None


def test_every_registered_capability_is_mapped_or_explicitly_unmappable():
    expected = {
        "yahoo_history": "stock_kline_yahoo",
        "yahoo_history_batch": "stock_kline_yahoo",
        "yahoo_quote": "yahoo_quote_summary",
        "yahoo_profile": "yahoo_quote_summary",
        "yahoo_financials": "financial_statements_yahoo",
        "yahoo_news": "stock_news",
        "yahoo_search": "stock_search",
        "yahoo_statistics": "key_statistics",
        "yahoo_analysis": "analyst_estimates",
        "yahoo_holders": "institutional_holders",
        "yahoo_options": "options_chain",
        "yahoo_earnings": "yahoo_quote_summary",
        "yahoo_funds": "yahoo_quote_summary",
    }
    params = {
        "symbol": "AAPL", "symbols": ["AAPL"], "query": "Apple", "count": 8,
        "interval": "1d", "period": "1y",
    }
    assert set(expected) | {"yahoo_screen"} == YahooProvider.NAMES
    for capability, legacy_name in expected.items():
        assert _route(capability, params)[0] == legacy_name
    assert _route("yahoo_screen", params) is None
    assert _route("yahoo_search", {**params, "lookup_type": "etf"}) is None
