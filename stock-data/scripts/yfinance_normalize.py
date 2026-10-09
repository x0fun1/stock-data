"""Pure Yahoo/yfinance mappings. No imports of yfinance, sessions, or network I/O.

Native frames are represented as indexed rows (not purported HTTP payloads).
Missing values never become zero. Source identity/calendar evidence must be supplied
by the caller; this module does not infer either from a requested ticker.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import math
from zoneinfo import ZoneInfo


def json_safe(value):
    """Return standard-JSON values, including lossless frame axes/row structure."""
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, Decimal)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "iterrows") and hasattr(value, "columns"):
        return {"columns": [json_safe(c) for c in value.columns],
                "rows": [{"index": json_safe(i), "values": [json_safe(row[c]) for c in value.columns]}
                         for i, row in value.iterrows()]}
    if isinstance(value, Mapping):
        # Date/MultiIndex keys are serialized explicitly; never leave Timestamp keys.
        return {str(json_safe(k)): json_safe(v) for k, v in value.items()}
    if hasattr(value, "items") and hasattr(value, "index"):
        return {"rows": [{"index": json_safe(k), "value": json_safe(v)} for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((json_safe(v) for v in value), key=str)
    if hasattr(value, "item"):
        return json_safe(value.item())
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _number(value):
    value = json_safe(value)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _datetime(value):
    if value is None or type(value).__name__ in {"NaTType", "NAType"}:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _utc(value):
    stamp = _datetime(value)
    if stamp is None or stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rows(frame):
    if frame is None:
        return []
    if hasattr(frame, "iterrows"):
        return [(index, dict(row.items())) for index, row in frame.iterrows()]
    if isinstance(frame, list):
        return [(row.get("timestamp", row.get("date", row.get("index"))), row) for row in frame]
    raise TypeError("Expected DataFrame or indexed row records")


_PRICE_FIELDS = {"open": "Open", "high": "High", "low": "Low", "close": "Close"}
_ACTION_FIELDS = {"dividends": "Dividends", "stock_splits": "Stock Splits", "capital_gains": "Capital Gains"}


def normalize_history(frame, metadata, *, price_basis="provider", symbol=None, interval="1d"):
    """Map history to bars; timestamps are bar labels, not trade/close times.

    Metadata must be a selected plain Mapping, not yfinance's lazy metadata object.
    No closure assertion is made here. Explicit source_index in batch helpers is
    required to identify alignment-only rows reliably.
    """
    if price_basis not in {"provider", "adjusted"}:
        raise ValueError("price_basis must be provider or adjusted")
    metadata = dict(metadata or {})
    tz_name = metadata.get("exchangeTimezoneName")
    warnings, bars, actions, ratios = [], [], [], []
    try:
        exchange_tz = ZoneInfo(tz_name) if tz_name else None
    except (ValueError, KeyError):
        exchange_tz = None
        warnings.append("invalid_exchange_timezone")
    if exchange_tz is None:
        warnings.append("exchange_timezone_unconfirmed")
    gaps = 0
    for index, row in _rows(frame):
        stamp = _datetime(index)
        if stamp is None:
            warnings.append("invalid_bar_timestamp")
            gaps += 1
            continue
        if stamp.tzinfo is None and exchange_tz is not None:
            stamp = stamp.replace(tzinfo=exchange_tz)
        local = stamp.astimezone(exchange_tz) if stamp.tzinfo and exchange_tz else stamp
        raw = {key: _number(row.get(column, row.get(key))) for key, column in _PRICE_FIELDS.items()}
        adj = _number(row.get("Adj Close", row.get("adj_close")))
        close = raw["close"]
        ratio = adj / close if adj is not None and close is not None and close > 0 and adj > 0 else None
        if ratio is not None and (not math.isfinite(ratio) or ratio <= 0):
            ratio = None
        # Preserve a conventional timestamp field as well as the explicit UTC
        # alias. The forecast pipeline uses `date` as its daily session key.
        bar = {"date": local.date().isoformat(), "timestamp": _utc(stamp), "timestamp_utc": _utc(stamp),
               "timezone": tz_name, "provider_ohlc": raw, "adj_close": adj,
               "volume": _number(row.get("Volume", row.get("volume"))),
               "adjustment_ratio": ratio, "price_basis": price_basis,
               "missing_fields": [key for key, val in raw.items() if val is None]}
        bar.update(raw if price_basis == "provider" else {
            key: json_safe(val * ratio) if val is not None and ratio is not None else None
            for key, val in raw.items()})
        bar.update({key: _number(row.get(column, row.get(key))) for key, column in _ACTION_FIELDS.items()})
        if "Repaired?" in row:
            bar["repaired_flag"] = json_safe(row["Repaired?"])
        if price_basis == "adjusted" and ratio is None:
            bar["adjustment_gap"] = "invalid_or_missing_adj_close_ratio"
            gaps += 1
        if raw["close"] is None:
            gaps += 1
        if stamp.tzinfo is None:
            warnings.append("bar_timezone_unconfirmed")
        ratios.append({"date": bar["date"], "timestamp_utc": bar["timestamp_utc"], "ratio": ratio})
        for key in _ACTION_FIELDS:
            if bar[key] is not None and bar[key] != 0:
                actions.append({"date": bar["date"], "kind": key, "value": bar[key]})
        bars.append(bar)
    if price_basis == "adjusted" and any(bar.get("adjustment_gap") for bar in bars):
        warnings.append("adjusted_view_has_ratio_gaps")
    dates = [bar["date"] for bar in bars]
    source_times = [bar["timestamp_utc"] for bar in bars if bar["timestamp_utc"]]
    window = {"start": min(dates, default=None), "end": max(dates, default=None),
              "selection_rule": "actual_returned_bars; closure not asserted"}
    evidence = {"source": "Yahoo Finance", "price_basis": price_basis,
                "checked_through": max(dates, default=None), "actions": actions,
                "action_coverage_confirmed": False,
                "coverage_note": "observed actions only; empty list does not prove complete action coverage",
                "transformation": "Adj Close / Close applied to OHLC; volume unchanged" if price_basis == "adjusted" else "none",
                "ratios": ratios}
    # Selected chart metadata `symbol` or a caller-supplied, source-confirmed
    # `source_symbol` establishes identity. Never use the requested symbol or
    # arbitrary `identity` objects to fill a missing source confirmation.
    source_symbol = metadata.get("source_symbol") or metadata.get("symbol")
    return json_safe({"status": "empty" if not bars else "partial" if gaps else "success",
        "actual_source": "Yahoo Finance", "requested_symbol": symbol,
        "source_symbol": source_symbol, "currency": metadata.get("currency"),
        "frequency": interval, "adjustment": price_basis, "adjustment_evidence": evidence,
        "source_timestamp": max(source_times, default=None), "source_timestamp_semantics": "bar_label_utc",
        "last_bar_closed": None, "metadata": metadata,
        "data": {"bars": bars, "history_window": window, "symbol": source_symbol,
                 "source_symbol": source_symbol, "currency": metadata.get("currency"), "frequency": interval},
        "warnings": sorted(set(warnings)),
        "native_payload_kind": "yfinance_processed_result_not_raw_http"})


def repair_diff(original, repaired, original_metadata=None, repaired_metadata=None):
    """Compare all values, including Adj Close/actions; flag alone is insufficient."""
    before = {str(json_safe(index)): json_safe(row) for index, row in _rows(original)}
    after = {str(json_safe(index)): json_safe(row) for index, row in _rows(repaired)}
    differences = []
    for index in sorted(before.keys() | after.keys()):
        left, right = before.get(index, {}), after.get(index, {})
        for field in sorted(left.keys() | right.keys()):
            if left.get(field) != right.get(field) or (field in left) != (field in right):
                differences.append({"index": index, "field": field, "before": left.get(field), "after": right.get(field)})
    currency_before = (original_metadata or {}).get("currency")
    currency_after = (repaired_metadata or {}).get("currency")
    return {"changed": bool(differences) or currency_before != currency_after,
            "differences": differences, "currency_before": currency_before, "currency_after": currency_after,
            "original": json_safe(original), "repaired": json_safe(repaired)}


def normalize_history_batch(frames, metadata=None, *, price_basis="provider", interval="1d", source_indices=None):
    """Normalize per-symbol frames. Unknown all-null rows remain source-ambiguous.

    Caller splits MultiIndex by column labels before this function. A source_indices
    mapping, if actually known, distinguishes alignment-created slots from source NA.
    """
    results = {}
    for symbol, frame in frames.items():
        rows, excluded = [], []
        known = (source_indices or {}).get(symbol)
        known = {str(json_safe(i)) for i in known} if known is not None else None
        for index, row in _rows(frame):
            label = str(json_safe(index))
            if known is not None and label not in known:
                excluded.append(label)
                continue
            rows.append({**row, "timestamp": index})
        result = normalize_history(rows, (metadata or {}).get(symbol, {}), price_basis=price_basis, symbol=symbol, interval=interval)
        valid = [bar for bar in result["data"]["bars"] if bar["close"] is not None]
        if not valid:
            result["status"] = "empty"
            result["warnings"].append("no_valid_prices; empty_reason_unconfirmed")
        result["alignment"] = {"source_index_known": known is not None, "excluded_slots": excluded,
                               "null_origin": "source" if known is not None else "source_or_batch_alignment_unconfirmed"}
        result["bar_count"] = len(result["data"]["bars"])
        results[symbol] = result
    ok = [row for row in results.values() if row["status"] == "success"]
    has_data = any(any(bar["close"] is not None for bar in row["data"]["bars"]) for row in results.values())
    return {"status": "success" if results and len(ok) == len(results) else "partial" if has_data else "empty", "symbols": results}


def normalize_financials(statements, frequency, currency=None):
    if frequency not in {"annual", "yearly", "quarterly", "ttm", "trailing"}:
        raise ValueError("Unsupported financial frequency")
    output, statuses = [], {}
    for statement, frame in statements.items():
        if frequency in {"ttm", "trailing"} and statement in {"balance_sheet", "balance", "balancesheet"}:
            statuses[statement] = "unsupported"
            continue
        count = 0
        if frame is not None and hasattr(frame, "columns"):
            for account, row in _rows(frame):
                for period, value in row.items():
                    end = _datetime(period)
                    output.append({"statement": statement, "period_end": end.date().isoformat() if end else None,
                                   "frequency": frequency, "account": str(account), "value": _number(value),
                                   "currency": currency, "unit": None, "published_at": None, "available_at": None})
                    count += _number(value) is not None
        statuses[statement] = "success" if count else "empty"
    success = sum(status == "success" for status in statuses.values())
    return {"status": "success" if success and success == len(statuses) else "partial" if success else "empty",
            "statements": statuses, "records": output, "currency": currency,
            "warnings": ["period_end_is_not_disclosure_date", "units_not_confirmed"]}


def _tags(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        tag = item.get("symbol") if isinstance(item, Mapping) else item
        if isinstance(tag, str) and tag.strip():
            result.append(tag.strip())
    return sorted(set(result))


def normalize_news(items, *, kind="ticker", query_symbol=None):
    """Two independent schemas; query symbol is never fabricated as article tag."""
    if kind not in {"ticker", "search"}:
        raise ValueError("news kind must be ticker or search")
    articles, warnings = [], []
    for item in items or []:
        if not isinstance(item, Mapping):
            warnings.append("invalid_news_record")
            continue
        content = item.get("content", {}) if kind == "ticker" else item
        if not isinstance(content, Mapping):
            warnings.append("invalid_ticker_content")
            continue
        content_type = content.get("contentType", item.get("type"))
        if str(content_type).upper() in {"AD", "ADVERTISEMENT", "SPONSORED"} or item.get("isAd") is True:
            warnings.append("advertisement_excluded")
            continue
        if kind == "ticker":
            published = _utc(content.get("pubDate")) or _utc(content.get("displayTime"))
            updated = _utc(content.get("updatedAt", content.get("lastModified")))
            provider = content.get("provider") or {}
            publisher = provider.get("displayName") if isinstance(provider, Mapping) else provider
            canonical = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            url = canonical.get("url") if isinstance(canonical, Mapping) else canonical
            tags = _tags(content.get("relatedTickers", item.get("relatedTickers")))
        else:
            published = _utc(item.get("providerPublishTime"))
            updated = _utc(item.get("updatedAt", item.get("lastModified")))
            publisher, url = item.get("publisher"), item.get("link")
            tags = _tags(item.get("relatedTickers"))
        article = {"id": item.get("id", item.get("uuid", content.get("id"))),
                   "title": content.get("title"), "summary": content.get("summary"),
                   "publisher": publisher, "provider": publisher, "source": publisher,
                   "url": url, "published_at": published, "updated_at": updated,
                   "tickers": tags, "query_symbol": query_symbol,
                   "record_type": "news" if str(content_type).upper() in {"STORY", "ARTICLE"} else "unknown",
                   "content_type": content_type, "news_kind": kind,
                   "strict_news_eligible": published is not None,
                   "source_record": json_safe(item)}
        if published is None:
            warnings.append("missing_reliable_publication_time; exclude_from_strict_news")
        if not tags:
            warnings.append("article_related_tickers_unconfirmed")
        articles.append(article)
    return {"articles": articles, "warnings": sorted(set(warnings)),
            "coverage": {"returned_count": len(articles), "historical_coverage_confirmed": False,
                         "full_text_available": False}}


def normalize_options(chain, *, expiration=None, underlying=None):
    """Map native OptionChain or calls/puts dict. No Greeks or trade direction."""
    output = {}
    for side in ("calls", "puts"):
        frame = chain.get(side) if isinstance(chain, Mapping) else getattr(chain, side, None)
        rows = []
        for _, row in _rows(frame):
            normalized = json_safe(row)
            normalized["lastTradeDate"] = _utc(row.get("lastTradeDate"))
            rows.append(normalized)
        output[side] = rows
    return {"expiration": expiration, "underlying": json_safe(underlying), **output,
            "warnings": ["no_greeks_or_trade_direction_inferred"]}


def normalize_funds(fields):
    """Accept already-selected funds_data fields; never access lazy properties."""
    data = json_safe(fields)
    return {"data": data, "holdings_as_of": fields.get("holdings_as_of"),
            "warnings": [] if fields.get("holdings_as_of") else ["holdings_date_not_provided; current_holdings_not_historical"]}
