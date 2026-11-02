"""Finnhub date-segmented company news, without claiming undocumented completeness."""

import os
import re
from datetime import datetime, timezone

from ..common.time import iso
from .base import identifier, in_window, interval, intervals, permissions, record, result
from .transport import SourceError


def collect(client, item, start, end):
    token = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not token or any(ord(char) < 32 for char in token):
        raise SourceError("finnhub_api_key_not_configured", unavailable=True)
    symbol = item["symbol"]
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9.^_-]{1,32}", symbol):
        raise SourceError("finnhub_invalid_symbol", unavailable=True)
    permission = permissions(item)
    rows, failed, queried = [], [], []
    capped = False
    for left, right in intervals(start, end):
        try:
            payload = client.json("finnhub.io", "/api/v1/company-news", params={
                "symbol": symbol, "from": left.date().isoformat(), "to": right.date().isoformat(),
            }, headers={"X-Finnhub-Token": token})
            if not isinstance(payload, list):
                raise SourceError("finnhub_invalid_news_response")
            for article in payload:
                if not isinstance(article, dict) or type(article.get("datetime")) not in {int, float}:
                    raise SourceError("finnhub_invalid_article_time")
                publication = iso(datetime.fromtimestamp(article["datetime"], timezone.utc))
                if not in_window(publication, left, right):
                    continue
                title, url = article.get("headline"), article.get("url")
                if not isinstance(title, str) or not isinstance(url, str):
                    raise SourceError("finnhub_invalid_article_metadata")
                text = article.get("summary", "") if permission["store_text"] else ""
                if not isinstance(text, str):
                    raise SourceError("finnhub_invalid_summary")
                rows.append(record(
                    "finnhub", str(article.get("id") or identifier(url, publication)),
                    title=title, url=url, published_at=publication, entities=[symbol],
                    text=text, text_scope="abstract" if text else "headline_only", permissions=permission,
                ))
            queried.append(interval(left, right))
            failed.append(interval(left, right, "finnhub_exhaustion_and_truncation_not_verifiable"))
        except (SourceError, ValueError, OverflowError, OSError) as exc:
            reason = exc.reason if isinstance(exc, SourceError) else "finnhub_invalid_news_value"
            failed.append(interval(left, end, reason))
            capped = getattr(exc, "cap_reached", False)
            break
    output = result(rows, failed=failed, cap_reached=capped,
                    limitations=["Finnhub HTTP success confirms only the query, not complete history, absence of caps, HK entitlement or storage rights."])
    output["queried_intervals"] = queried
    return output
