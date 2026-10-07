"""Synthetic sessions and observations. This is NOT a real market calendar/feed."""
from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo


ASOF = "2024-03-15T21:00:00Z"


def collection(count=756, *, market="US", ticker="INTC"):
    days, cursor = [], date(2024, 3, 15)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    bars, previous = [], 100.0
    for i, day in enumerate(days):
        close = previous * (1 + 0.0003 + 0.006 * math.sin(i / 7) + 0.003 * math.cos(i / 23))
        bars.append({"date": day.isoformat(), "open": previous, "high": max(previous, close) * 1.01, "low": min(previous, close) * 0.99, "close": close, "volume": 10000 + int(500 * math.sin(i / 5)) + i})
        previous = close
    zone = "America/New_York" if market == "US" else "Asia/Hong_Kong"
    sessions = [{"date": day.isoformat(), "close_at": datetime.combine(day, time(16), ZoneInfo(zone)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")} for day in days]
    block = {"status": "complete", "actual_source": "SYNTHETIC", "source_timestamp": sessions[-1]["close_at"], "fetched_at_utc": ASOF,
             "currency": "USD" if market == "US" else "HKD", "unit": "shares", "adjustment": "adjusted", "last_bar_closed": True,
             "adjustment_evidence": {"source": "SYNTHETIC", "price_basis": "adjusted", "checked_through": days[-1].isoformat(), "actions": []},
             "session_calendar": {"source": "SYNTHETIC, NOT AN ACTUAL EXCHANGE CALENDAR", "timezone": zone, "coverage_start": days[0].isoformat(), "coverage_end": "2024-03-15", "sessions": sessions},
             "data": {"symbol": ticker, "frequency": "1d", "history_window": {"start": days[0].isoformat(), "end": days[-1].isoformat(), "selection": "fixed synthetic test window"}, "bars": bars},
             "gateway_envelope": {"is_success": True, "data": {"candles": [{"c": row["close"]} for row in bars]}, "next_actions": ["UNTRUSTED TEST TEXT"]}}
    return {"request": {"ticker": ticker, "market": market, "horizon": "5D", "asof": ASOF, "mode": "strict", "asset_type": "stock"}, "captured_at_utc": ASOF, "domains": {"market": block, "news": {"status": "complete", "actual_source": "SYNTHETIC", "source_timestamp": ASOF, "fetched_at_utc": ASOF, "data": {"articles": [article()]}}}}


def article(**overrides):
    return {"article_id": "synthetic-story", "title": "INTC raises guidance on strong demand", "source": "SYNTHETIC WIRE", "published_at": "2024-03-14T22:00:00Z", "tickers": ["INTC"], "record_type": "event", "url": "https://fixture.invalid/story", **overrides}
