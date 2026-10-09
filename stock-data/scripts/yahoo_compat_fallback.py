"""Automatic, provenance-preserving fallback to the pre-yfinance Yahoo routes.

This module imports the compatibility gateway only after a yfinance request has
failed. It never imports yfinance and never hides which interface supplied data.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def _has_data(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value)
    return value is not None and value != ""


def _range_for(params: dict[str, Any]) -> str:
    allowed = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
    period = params.get("period")
    if period in allowed:
        return period
    if period in {"7d", "14d", "30d", "60d", "90d"}:
        return "1mo" if period in {"7d", "14d", "30d"} else "3mo"
    start, end = params.get("start"), params.get("end")
    if start and end:
        try:
            days = max(0, (date.fromisoformat(end) - date.fromisoformat(start)).days)
            for limit, value in ((1, "1d"), (5, "5d"), (31, "1mo"), (93, "3mo"),
                                 (186, "6mo"), (366, "1y"), (732, "2y"),
                                 (1827, "5y"), (3653, "10y")):
                if days <= limit:
                    return value
        except (TypeError, ValueError):
            pass
    return "max" if start and end else "1mo"


def _adjusted_values(payload: dict[str, Any]) -> list[Any] | None:
    value = payload.get("adjclose")
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = value[0].get("adjclose")
    elif isinstance(value, dict):
        value = value.get("adjclose")
    return value if isinstance(value, list) else None


def _legacy_history(gateway: Any, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
    from yfinance_normalize import normalize_history

    interval = params.get("interval", "1d")
    raw = gateway.stock_kline_yahoo(symbol, interval=interval, range_=_range_for(params), include_metadata=True)
    if not isinstance(raw, dict):
        raw = {"bars": raw if isinstance(raw, list) else [], "meta": {}}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    rows = raw.get("bars") if isinstance(raw.get("bars"), list) else []
    adjusted = _adjusted_values(raw)
    native_rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["timestamp"] = row.get("timestamp_utc", row.get("timestamp", row.get("date")))
        if adjusted is not None and index < len(adjusted):
            item["Adj Close"] = adjusted[index]
        native_rows.append(item)
    basis = params.get("price_basis", "provider")
    if basis not in {"provider", "adjusted"}:
        basis = "provider"
    if basis == "adjusted" and adjusted is None:
        basis = "provider"
    normalized = normalize_history(native_rows, meta, price_basis=basis, symbol=symbol, interval=interval)
    bars = normalized["data"]["bars"]
    start, end = params.get("start"), params.get("end")
    if start or end:
        bars = [bar for bar in bars if (not start or bar["date"] >= start) and (not end or bar["date"] < end)]
        normalized["data"]["bars"] = bars
        normalized["data"]["history_window"] = {
            "start": min((bar["date"] for bar in bars), default=None),
            "end": max((bar["date"] for bar in bars), default=None),
            "selection_rule": "legacy range window filtered to requested dates; coverage may be incomplete",
        }
    return {
        "status": "empty" if not bars else "partial",
        "data": normalized["data"],
        "actual_source": "Yahoo Finance",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_symbol": normalized.get("source_symbol"),
        "currency": normalized.get("currency"),
        "unit": None,
        "frequency": interval,
        "source_timestamp": bars[-1].get("timestamp_utc") if bars else None,
        "source_timestamp_kind": "bar_label_not_trade_time",
        "last_bar_closed": None,
        "adjustment": basis,
        "adjustment_evidence": normalized.get("adjustment_evidence"),
        "provider_library": "legacy_compat",
        "provider_version": "stock-data gateway",
        "fallback_attempted": True,
        "fallback_used": True,
        "fallback_route": "stock_kline_yahoo",
        "fallback_reason": None,
        "warnings": ["Legacy chart fallback uses coarse range windows; date filtering does not prove complete requested coverage.",
                     "Legacy Yahoo source does not confirm unit or bar closure; existing gates remain in force."]
                    + ([] if adjusted is not None or params.get("price_basis", "provider") != "adjusted"
                       else ["Legacy chart response omitted adjusted-close values; provider OHLC basis retained."]),
        "provenance": {"library": "legacy_compat", "route": "stock_kline_yahoo",
                       "raw_http_payload_captured": False},
    }


def _legacy_news(gateway: Any, symbol: str | None, query: str | None, count: int) -> dict[str, Any]:
    from yfinance_normalize import normalize_news

    keyword = symbol or query
    rows = gateway.stock_news(keyword, count=count)
    native = [{"uuid": row.get("article_id"), "title": row.get("title"),
               "publisher": row.get("publisher"), "link": row.get("link"),
               "providerPublishTime": row.get("publish_time"),
               "relatedTickers": row.get("tickers")}
              for row in rows if isinstance(row, dict)]
    normalized = normalize_news(native, kind="search", query_symbol=symbol)
    return {"status": "empty" if not normalized["articles"] else "partial",
            "data": normalized, "actual_source": "Yahoo Finance",
            "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provider_library": "legacy_compat", "provider_version": "stock-data gateway",
            "fallback_attempted": True,
            "fallback_used": True, "fallback_route": "stock_news",
            "fallback_reason": None,
            "warnings": ["Legacy news compatibility route used; historical PIT coverage is not established."] + normalized["warnings"],
            "provenance": {"library": "legacy_compat", "route": "stock_news",
                           "raw_http_payload_captured": False}}


def _route(function: str, params: dict[str, Any]) -> tuple[str, str, Any, list[str]] | None:
    """Return legacy function, source, arguments, and warnings for a capability."""
    symbol = params.get("symbol")
    if function in {"yahoo_history", "yahoo_history_batch"}:
        return "stock_kline_yahoo", "Yahoo Finance", None, []
    if function in {"yahoo_quote", "yahoo_profile"}:
        modules = (["price", "summaryDetail", "financialData", "defaultKeyStatistics"]
                   if function == "yahoo_quote" else ["assetProfile", "summaryProfile", "price"])
        return "yahoo_quote_summary", "Yahoo Finance", {"symbol": symbol, "modules": modules}, ["Legacy quoteSummary structure is returned without the yfinance field projection."]
    if function == "yahoo_financials":
        frequency = params.get("frequency", "yearly")
        return "financial_statements_yahoo", "Yahoo Finance", {
            "symbol": symbol, "quarterly": frequency == "quarterly"}, (
            ["Legacy compatibility route cannot provide a trailing-twelve-month statement."] if frequency in {"trailing", "ttm"} else [])
    if function == "yahoo_news":
        return "stock_news", "Yahoo Finance", None, []
    if function == "yahoo_search":
        if params.get("lookup_type") is not None:
            return None
        return "stock_search", "Eastmoney search", {"keyword": params.get("query"), "count": params.get("count", 8)}, ["Search fallback uses the legacy Eastmoney search route."]
    if function == "yahoo_statistics":
        return "key_statistics", "Yahoo Finance", {"symbol": symbol}, []
    if function == "yahoo_analysis":
        return "analyst_estimates", "Yahoo Finance", {"symbol": symbol}, []
    if function == "yahoo_holders":
        return "institutional_holders", "Yahoo Finance", {"symbol": symbol}, []
    if function == "yahoo_options":
        return "options_chain", "Yahoo Finance", None, ["Legacy options route supports U.S. tickers and Unix expiry values only."]
    if function == "yahoo_earnings":
        modules = ["calendarEvents", "earnings", "earningsHistory", "earningsTrend"]
        return "yahoo_quote_summary", "Yahoo Finance", {"symbol": symbol, "modules": modules}, ["Legacy quoteSummary earnings fields have no module-level completeness guarantee."]
    if function == "yahoo_funds":
        modules = ["fundProfile", "summaryProfile", "topHoldings", "fundPerformance"]
        return "yahoo_quote_summary", "Yahoo Finance", {"symbol": symbol, "modules": modules}, ["Legacy quoteSummary fund modules may be unavailable for this ticker."]
    return None


def _call_legacy(gateway: Any, function: str, params: dict[str, Any]) -> tuple[Any, str, str, list[str]]:
    if function == "yahoo_history":
        return _legacy_history(gateway, params["symbol"], params), "Yahoo Finance", "stock_kline_yahoo", []
    if function == "yahoo_history_batch":
        symbols = params.get("symbols") or []
        symbols_result = {}
        for symbol in symbols:
            try:
                symbols_result[symbol] = _legacy_history(gateway, symbol, params)
            except Exception as exc:
                symbols_result[symbol] = {"status": "failed", "error": {"category": type(exc).__name__}}
        any_data = any(_has_data(item.get("data")) for item in symbols_result.values())
        status = "partial" if any_data else "empty"
        return {"status": status, "data": {"symbols": symbols_result}}, "Yahoo Finance", "stock_kline_yahoo", ["Legacy batch fallback performs bounded per-symbol compatibility calls."]
    if function == "yahoo_news":
        data = _legacy_news(gateway, params.get("symbol"), params.get("query"), params.get("count", 10))
        return data, "Yahoo Finance", "stock_news", []
    if function == "yahoo_options":
        symbol, expiration = params.get("symbol"), params.get("expiration")
        if expiration is None:
            data = gateway.options_chain(symbol)
        else:
            target = date.fromisoformat(expiration)
            listing = gateway.options_chain(symbol)
            expiries = listing.get("expiration_dates", []) if isinstance(listing, dict) else []
            matches = [value for value in expiries if datetime.fromtimestamp(value, timezone.utc).date() == target]
            if not matches:
                raise ValueError("Legacy options route did not list the requested expiration.")
            data = gateway.options_chain(symbol, expiration=matches[0])
        return data, "Yahoo Finance", "options_chain", ["Legacy option-chain field names and coverage differ from yfinance."]
    route = _route(function, params)
    if route is None:
        raise LookupError("No legacy compatibility route is registered for this yfinance capability.")
    name, source, mapped, warnings = route
    if mapped is None:
        if function == "yahoo_news":
            raise AssertionError("handled above")
        raise LookupError("Legacy compatibility parameters cannot be mapped safely.")
    data = gateway.FUNCTIONS[name](**mapped)
    return data, source, name, warnings


def fallback(function: str, params: dict[str, Any], reason: dict[str, Any]) -> dict[str, Any]:
    """Try one registered compatibility route after a yfinance failure."""
    from datetime import datetime, timezone
    special_routes = {"yahoo_history", "yahoo_history_batch", "yahoo_news", "yahoo_options"}
    if function not in special_routes and _route(function, params) is None:
        return {"status": "error", "data": None, "actual_source": "Yahoo Finance",
                "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "fallback_attempted": True, "fallback_used": False,
                "fallback_route": None, "fallback_reason": reason,
                "error": {"category": "NoLegacyRoute", "exception_type": "LookupError",
                          "message": "No semantically equivalent legacy compatibility route is registered."},
                "warnings": ["Automatic legacy fallback was evaluated; no equivalent route is available."]}
    try:
        import global_stock_data as gateway
        data, source, route, warnings = _call_legacy(gateway, function, params)
        if isinstance(data, dict) and data.get("fallback_used") is True:
            result = dict(data)
            result["fallback_reason"] = reason
            result.setdefault("warnings", []).extend(warnings)
            return result
        if isinstance(data, dict) and "status" in data and "data" in data:
            status, payload = data["status"], data["data"]
            status = "empty" if status == "empty" or payload is None else "partial"
        else:
            status, payload = ("empty" if not _has_data(data) else "partial"), data
        return {"status": status, "data": payload if status != "empty" else None,
                "actual_source": source, "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "provider_library": "legacy_compat", "provider_version": "stock-data gateway",
                "fallback_attempted": True,
                "fallback_used": True, "fallback_route": route, "fallback_reason": reason,
                "warnings": ["yfinance request failed; legacy compatibility route was used."] + warnings,
                "provenance": {"library": "legacy_compat", "route": route,
                               "raw_http_payload_captured": False}}
    except Exception as exc:
        category = "NoLegacyRoute" if isinstance(exc, LookupError) else "LegacyFallbackFailed"
        if function in {"yahoo_history", "yahoo_history_batch"}:
            attempted_route = "stock_kline_yahoo"
        elif function == "yahoo_news":
            attempted_route = "stock_news"
        elif function == "yahoo_options":
            attempted_route = "options_chain"
        else:
            mapped_route = _route(function, params)
            attempted_route = mapped_route[0] if mapped_route else None
        return {"status": "error", "data": None, "actual_source": "Yahoo Finance",
                "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "fallback_attempted": True, "fallback_used": False,
                "fallback_route": attempted_route, "fallback_reason": reason,
                "error": {"category": category, "exception_type": type(exc).__name__,
                          "message": "Legacy compatibility fallback did not return data; details redacted."},
                "warnings": ["yfinance request failed; automatic legacy fallback was attempted."]}
