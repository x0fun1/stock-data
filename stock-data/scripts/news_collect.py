#!/usr/bin/env python3
"""Discover and filter 60-day news candidates from SEC, IR RSS and Finnhub.

The collector only discovers and normalizes candidate records. Verification is a
separate step (``news_verify.py``) and per-headline judgement belongs to the host
AI.  Standard library only; no provider SDKs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

TOOL_USER_AGENT = "stock-data-skill/4.0"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
REQUEST_TIMEOUT_SECONDS = 8
NEWS_WINDOW_DAYS = 60
RECENT_WINDOW_DAYS = 30
MAX_ARTICLES = 500
FINNHUB_SEGMENT_DAYS = 2
FINNHUB_RESULT_CAP = 250
FINNHUB_RESULT_CAP_MARGIN = 10
DEFAULT_REFS = Path(__file__).resolve().parent.parent / "references" / "news-sources.json"

Fetch = Callable[[str, dict[str, str]], tuple[int | None, str | None, str | None]]


class RateLimiter:
    """Sliding-window and minimum-interval limiter (standard library only)."""

    def __init__(self, *, max_calls: int, window_seconds: float, min_interval_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.min_interval_seconds = min_interval_seconds
        self.sleep = sleep
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] >= self.window_seconds:
            self.calls.popleft()
        if self.min_interval_seconds > 0 and self.calls:
            delay = self.min_interval_seconds - (now - self.calls[-1])
            if delay > 0:
                self.sleep(delay)
                now = time.monotonic()
        if len(self.calls) >= self.max_calls:
            delay = self.window_seconds - (now - self.calls[0])
            if delay > 0:
                self.sleep(delay)
                now = time.monotonic()
            while self.calls and now - self.calls[0] >= self.window_seconds:
                self.calls.popleft()
        self.calls.append(now)


def _fetch(url: str, headers: dict[str, str]) -> tuple[int | None, str | None, str | None]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed HTTPS provider hosts
            return response.status, response.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"network_error: {type(exc.reason).__name__}"
    except TimeoutError:
        return None, None, "timeout"
    except Exception as exc:  # noqa: BLE001 - one source failure must not abort the run
        return None, None, f"{type(exc).__name__}"


def load_sources(path: Path | None = None) -> dict[str, Any]:
    value = json.loads((path or DEFAULT_REFS).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("news-sources.json must contain an object")
    return value


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm_title(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+[-–—]\s+[^-–—]{1,50}$", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
    return text


def _canonical_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urllib.parse.urlsplit(text)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
    query = [(key, item) for key, item in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                                   urllib.parse.urlencode(query), ""))


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname.lower() if urllib.parse.urlsplit(url).hostname else ""
    except ValueError:
        return ""


def _domain_in(host: str, domains: list[str]) -> bool:
    return any(host == item or host.endswith("." + item) for item in domains)


def classify_source(article: dict[str, Any], refs: dict[str, Any]) -> str:
    source_id = article.get("source_id")
    if source_id in {"sec_edgar", "ir_rss"}:
        return "A_primary"
    tiers = refs.get("source_tiers") or {}
    host = _host(str(article.get("url") or ""))
    if _domain_in(host, list(tiers.get("B_media") or [])):
        return "B_media"
    if _domain_in(host, list(tiers.get("C_aggregator") or [])):
        return "C_aggregator"
    # Unknown third-party publishers stay conservative.
    return "C_aggregator"


def _article(*, article_id: str, title: str, url: str | None, source: str, source_type: str,
             record_type: str, published_at: str, ticker: str, summary: str | None = None,
             source_id: str, time_precision: str = "timestamp", form: str | None = None,
             items: list[str] | None = None, related: list[str] | None = None) -> dict[str, Any]:
    return {
        "article_id": article_id,
        "title": title,
        "url": url,
        "source": source,
        "source_type": source_type,
        "record_type": record_type,
        "published_at": published_at,
        "tickers": [ticker],
        "summary": summary,
        "source_id": source_id,
        "time_precision": time_precision,
        "form": form,
        "items": items or [],
        "related": related or [],
    }


def _new_status() -> dict[str, Any]:
    return {"status": "not_configured", "http_status": None, "count": 0,
            "observed_first": None, "observed_last": None,
            "covered_first": None, "covered_last": None,
            "feed_truncated": False, "partial_window": False, "reason": None}


def _in_window(moment: datetime, start: datetime, end: datetime) -> bool:
    return start < moment <= end


def _finalize_status(status: dict[str, Any], rows: list[dict[str, Any]], start: datetime,
                     *, consider_truncation: bool = True) -> None:
    status["count"] = len(rows)
    if rows:
        moments = sorted(_parse_instant(row["published_at"]) for row in rows)
        moments = [item for item in moments if item is not None]
        if moments:
            status["observed_first"] = _iso(moments[0])
            status["observed_last"] = _iso(moments[-1])
            if consider_truncation and moments[0] > start:
                status["feed_truncated"] = True
                status["partial_window"] = True
                if status["status"] == "ok":
                    status["status"] = "partial_window"
    if status["status"] == "ok" and not rows:
        status["status"] = "empty"


def _sec_forms(refs: dict[str, Any], form: str) -> tuple[bool, bool]:
    forms = refs.get("sec_forms") or {}
    included = forms.get("included") or {}
    count_only = set(forms.get("count_only") or [])
    base = form.split("/")[0]
    if form in count_only or base in count_only:
        return False, True
    if form in included or base in included or (base.startswith("424B") and "424B" in included):
        return True, False
    if base.startswith("SC 13") and any(str(key).startswith("SC 13") for key in included):
        return True, False
    return False, False


def _sec_link(cik: int, accession: str) -> str:
    clean = re.sub(r"[^0-9]", "", accession)
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean}/{accession}-index.htm"


def _collect_sec(ticker: str, market: str, start: datetime, end: datetime, refs: dict[str, Any],
                 env: dict[str, str], fetch: Fetch, limiter: RateLimiter, result: dict[str, Any]) -> None:
    status = result["sources"]["sec_edgar"]
    if market != "US":
        status.update(status="not_applicable", reason="SEC EDGAR covers US issuers only")
        return
    user_agent = env.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        status.update(status="not_configured", reason="SEC_USER_AGENT is not set")
        return
    # SEC asks for a declarative UA with contact details.  Do not send a bare
    # tool identifier when no contact information is present.
    if "@" not in user_agent and "http" not in user_agent.lower():
        status.update(status="not_configured",
                      reason="SEC_USER_AGENT must include contact details")
        return
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    limiter.wait()
    code, body, error = fetch(SEC_TICKERS_URL, headers)
    status["http_status"] = code
    if error or code != 200 or not body:
        status.update(status="error", reason=error or f"HTTP {code}")
        return
    try:
        tickers = json.loads(body)
    except json.JSONDecodeError:
        status.update(status="error", reason="company_tickers.json is not valid JSON")
        return
    cik = None
    if isinstance(tickers, dict):
        for row in tickers.values():
            if isinstance(row, dict) and str(row.get("ticker", "")).upper() == ticker:
                cik = int(row["cik_str"])
                break
    if cik is None:
        status.update(status="empty", reason="ticker has no SEC CIK mapping")
        return
    limiter.wait()
    code, body, error = fetch(SEC_SUBMISSIONS_URL.format(cik=cik), headers)
    status["http_status"] = code
    if error or code != 200 or not body:
        status.update(status="error", reason=error or f"HTTP {code}")
        return
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        status.update(status="error", reason="submissions response is not valid JSON")
        return
    recent = ((payload.get("filings") or {}).get("recent") or {}) if isinstance(payload, dict) else {}
    columns = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument", "items"]
    if not all(isinstance(recent.get(name), list) for name in columns):
        status.update(status="error", reason="filings.recent is missing documented parallel arrays")
        return
    length = min(len(recent[name]) for name in columns)
    rows: list[dict[str, Any]] = []
    dropped = 0
    outside = 0
    form4 = 0
    form144 = 0
    item_map = refs.get("sec_items") or {}
    for index in range(length):
        form = str(recent["form"][index] or "").strip()
        include, count_only = _sec_forms(refs, form)
        if not include and not count_only:
            continue
        acceptance = _parse_instant(recent["acceptanceDateTime"][index])
        precision = "timestamp"
        if acceptance is None:
            filed = str(recent["filingDate"][index] or "")[:10]
            try:
                filed_day = date.fromisoformat(filed)
            except ValueError:
                dropped += 1
                continue
            # Filing dates are US/Eastern calendar days; midnight ET -> UTC.
            acceptance = datetime(filed_day.year, filed_day.month, filed_day.day,
                                  tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            precision = "date"
        if not _in_window(acceptance, start, end):
            outside += 1
            continue
        if count_only:
            form4 += form == "4"
            form144 += form == "144"
            continue
        accession = str(recent["accessionNumber"][index] or "")
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            dropped += 1
            continue
        item_codes = [item.strip() for item in str(recent["items"][index] or "").split(",") if item.strip()]
        item_titles = [item_map.get(code, code) for code in item_codes]
        title = f"{form} filing"
        if item_titles:
            title += " — " + "; ".join(item_titles)
        rows.append(_article(
            article_id=f"sec_{accession}", title=title, url=_sec_link(cik, accession),
            source="SEC EDGAR", source_type="sec", record_type="filing",
            published_at=_iso(acceptance), ticker=ticker, summary=None,
            source_id="sec_edgar", time_precision=precision, form=form, items=item_codes,
        ))
    result["form4_count"] = form4
    result["form144_count"] = form144
    status["dropped_invalid_timestamp"] = dropped
    status["dropped_outside_window"] = outside
    if status["status"] == "not_configured":
        status["status"] = "ok"
    _finalize_status(status, rows, start)
    result["_primary_rows"].extend(rows)


def _xml_text(node: ET.Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        found = node.find(name)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return None


def _collect_ir(ticker: str, market: str, start: datetime, end: datetime, refs: dict[str, Any],
                fetch: Fetch, limiters: dict[str, RateLimiter], result: dict[str, Any]) -> None:
    status = result["sources"]["ir_rss"]
    feeds = (refs.get("ir_rss") or {}).get(ticker) if market == "US" else None
    if not feeds:
        status.update(status="not_configured_for_ticker", reason="no registered IR RSS feed for this ticker")
        return
    headers = {"User-Agent": TOOL_USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    http_status: int | None = None
    dropped = outside = 0
    for feed in feeds:
        url = str(feed.get("url") or "")
        host = _host(url)
        limiter = limiters.setdefault(host, RateLimiter(max_calls=1, window_seconds=1,
                                                        min_interval_seconds=float(feed.get("crawl_delay_seconds", 10))))
        limiter.wait()
        code, body, error = fetch(url, headers)
        http_status = code or http_status
        if error or code != 200 or not body:
            errors.append(f"{host}: {error or f'HTTP {code}'}")
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            errors.append(f"{host}: invalid XML")
            continue
        entries = root.findall(".//item")
        if not entries:
            entries = [node for node in root.findall(".//{*}entry")]
        for entry in entries:
            title = _xml_text(entry, ("title", "{*}title"))
            link = _xml_text(entry, ("link", "{*}link"))
            if link is None:
                link_node = entry.find("{*}link")
                link = link_node.get("href") if link_node is not None else None
            raw_date = _xml_text(entry, ("pubDate", "published", "updated", "{*}pubDate", "{*}published", "{*}updated"))
            moment = None
            if raw_date:
                try:
                    moment = parsedate_to_datetime(raw_date).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    moment = _parse_instant(raw_date)
            if not title or moment is None:
                dropped += 1
                continue
            if not _in_window(moment, start, end):
                outside += 1
                continue
            summary = _xml_text(entry, ("description", "summary", "{*}description", "{*}summary"))
            rows.append(_article(
                article_id=f"ir_{_norm_title(title)[:24]}_{int(moment.timestamp())}",
                title=title, url=link, source=str(feed.get("name") or "Company IR"),
                source_type="ir", record_type="news", published_at=_iso(moment),
                ticker=ticker, summary=summary, source_id="ir_rss",
            ))
    status["http_status"] = http_status
    status["dropped_invalid_timestamp"] = dropped
    status["dropped_outside_window"] = outside
    status["errors"] = errors
    if rows or not errors:
        status["status"] = "ok"
    else:
        status["status"] = "error"
        status["reason"] = "; ".join(errors)
    _finalize_status(status, rows, start)
    result["_primary_rows"].extend(rows)


def _collect_finnhub(ticker: str, start: datetime, end: datetime, refs: dict[str, Any],
                     env: dict[str, str], fetch: Fetch, limiter: RateLimiter,
                     aliases: list[str], result: dict[str, Any]) -> None:
    status = result["sources"]["finnhub_rest"]
    key = env.get("FINNHUB_API_KEY", "").strip()
    if not key:
        status.update(status="not_configured", reason="FINNHUB_API_KEY is not set")
        return
    headers = {"X-Finnhub-Token": key, "User-Agent": TOOL_USER_AGENT, "Accept": "application/json"}
    wanted = {ticker.upper(), *[alias.upper() for alias in aliases if alias]}
    rows: list[dict[str, Any]] = []
    dropped = outside = irrelevant = 0
    segments = 0
    truncated = False
    covered_last: date | None = None
    cursor = start.date()
    last_day = end.date()
    status["status"] = "ok"
    while cursor <= last_day:
        segment_end = min(cursor + timedelta(days=FINNHUB_SEGMENT_DAYS - 1), last_day)
        query = urllib.parse.urlencode({
            "symbol": ticker,
            "from": cursor.isoformat(),
            "to": segment_end.isoformat(),
        })
        segments += 1
        limiter.wait()
        code, body, error = fetch(f"{FINNHUB_URL}?{query}", headers)
        if code is not None:
            status["http_status"] = code
        if code == 429:
            status.update(status="rate_limited",
                          reason=f"HTTP 429 at segment {cursor.isoformat()}..{segment_end.isoformat()}; "
                                 f"covered {start.date().isoformat()}..{covered_last.isoformat() if covered_last else 'none'}; not retried")
            break
        if error or code != 200 or not body:
            status.update(status="error",
                          reason=f"{error or f'HTTP {code}'} at segment {cursor.isoformat()}..{segment_end.isoformat()}; "
                                 f"covered {start.date().isoformat()}..{covered_last.isoformat() if covered_last else 'none'}")
            break
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            status.update(status="error",
                          reason=f"company-news response is not valid JSON at segment {cursor.isoformat()}..{segment_end.isoformat()}; "
                                 f"covered {start.date().isoformat()}..{covered_last.isoformat() if covered_last else 'none'}")
            break
        if not isinstance(payload, list):
            status.update(status="error",
                          reason=f"company-news response is not an array at segment {cursor.isoformat()}..{segment_end.isoformat()}; "
                                 f"covered {start.date().isoformat()}..{covered_last.isoformat() if covered_last else 'none'}")
            break
        if len(payload) >= FINNHUB_RESULT_CAP - FINNHUB_RESULT_CAP_MARGIN:
            truncated = True
        for item in payload:
            if not isinstance(item, dict):
                dropped += 1
                continue
            raw_time = item.get("datetime")
            if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
                dropped += 1
                continue
            try:
                moment = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                dropped += 1
                continue
            if not _in_window(moment, start, end):
                outside += 1
                continue
            related = [part.strip() for part in str(item.get("related") or "").split(",") if part.strip()]
            if related and not ({part.upper() for part in related} & wanted):
                irrelevant += 1
                continue
            title = str(item.get("headline") or "").strip()
            if not title:
                dropped += 1
                continue
            url = str(item.get("url") or "").strip() or None
            article_id = str(item.get("id") or f"fh_{int(moment.timestamp())}_{_norm_title(title)[:20]}")
            rows.append(_article(
                article_id=f"finnhub_{article_id}", title=title, url=url,
                source=str(item.get("source") or "Finnhub"), source_type="news", record_type="news",
                published_at=_iso(moment), ticker=ticker, summary=item.get("summary"),
                source_id="finnhub_rest", related=related,
            ))
        covered_last = segment_end
        status["covered_first"] = start.date().isoformat()
        status["covered_last"] = covered_last.isoformat()
        cursor = segment_end + timedelta(days=1)
    status["segments"] = segments
    status["dropped_invalid_timestamp"] = dropped
    status["dropped_outside_window"] = outside
    status["dropped_irrelevant"] = irrelevant
    if truncated:
        status["feed_truncated"] = True
        status["partial_window"] = True
        if status["status"] == "ok":
            status["status"] = "partial_window"
        status["reason"] = "a segment returned a result count near the provider cap"
    _finalize_status(status, rows, start)
    result["_third_party_rows"].extend(rows)


def _collect_mcp(path: Path | None, ticker: str, start: datetime, end: datetime, refs: dict[str, Any],
                 result: dict[str, Any]) -> None:
    status = result["sources"]["finnhub_mcp"]
    if path is None:
        status.update(status="not_connected", reason="host Finnhub MCP results were not supplied")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        status.update(status="error", reason=f"{type(exc).__name__}")
        return
    items = payload.get("articles") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        status.update(status="error", reason="MCP results must contain an articles array")
        return
    rows: list[dict[str, Any]] = []
    dropped = outside = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        moment = _parse_instant(item.get("published_at") or item.get("datetime_utc"))
        if moment is None:
            dropped += 1
            continue
        if not _in_window(moment, start, end):
            outside += 1
            continue
        title = str(item.get("title") or item.get("headline") or "").strip()
        if not title:
            dropped += 1
            continue
        rows.append(_article(
            article_id=f"mcp_{item.get('id') or _norm_title(title)[:20]}", title=title,
            url=item.get("url"), source=str(item.get("source") or "Finnhub MCP"),
            source_type="news", record_type="news", published_at=_iso(moment),
            ticker=ticker, summary=item.get("summary"), source_id="finnhub_mcp",
        ))
    status.update(status="ok", dropped_invalid_timestamp=dropped, dropped_outside_window=outside)
    _finalize_status(status, rows, start)
    result["_third_party_rows"].extend(rows)


def _deduplicate(rows: list[dict[str, Any]], refs: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted(rows, key=lambda row: (
        0 if classify_source(row, refs) == "A_primary" else 1,
        -_parse_instant(row["published_at"]).timestamp() if _parse_instant(row["published_at"]) else 0,
    ))
    kept: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    merged = 0
    for row in ordered:
        key = (_norm_title(row["title"]), _canonical_url(row.get("url")))
        existing = index.get(key)
        if existing is None:
            row = dict(row)
            row["also_seen_in"] = []
            index[key] = row
            kept.append(row)
            continue
        merged += 1
        label = f"{row.get('source')}"
        if label and label not in existing["also_seen_in"] and label != existing.get("source"):
            existing["also_seen_in"].append(label)
    # A repost carries a different URL, so title+URL dedup alone cannot link it
    # to the issuer's own release.  Merge both third-party rows and duplicate
    # A_primary feeds into the first A_primary row with the same normalized title.
    primary_by_title: dict[str, dict[str, Any]] = {}
    for row in kept:
        if classify_source(row, refs) == "A_primary":
            primary_by_title.setdefault(_norm_title(row["title"]), row)
    remaining: list[dict[str, Any]] = []
    for row in kept:
        primary = primary_by_title.get(_norm_title(row["title"]))
        if primary is not None and primary is not row:
            label = str(row.get("source") or "")
            if label and label not in primary["also_seen_in"] and label != primary.get("source"):
                primary["also_seen_in"].append(label)
            merged += 1
            continue
        remaining.append(row)
    for row in remaining:
        row["source_tier"] = classify_source(row, refs)
    return remaining, merged

def _count_windows(rows: list[dict[str, Any]], end: datetime) -> dict[str, int]:
    recent_start = end - timedelta(days=RECENT_WINDOW_DAYS)
    recent = old = 0
    for row in rows:
        moment = _parse_instant(row["published_at"])
        if moment is None:
            continue
        if moment > recent_start:
            recent += 1
        else:
            old += 1
    return {"recent_30_days": recent, "days_31_60": old}


def collect(*, ticker: str, as_of: str, market: str = "US", aliases: list[str] | None = None,
            sources: tuple[str, ...] = ("sec", "ir", "finnhub"), fetch: Fetch = _fetch,
            env: dict[str, str] | None = None, refs: dict[str, Any] | None = None,
            mcp_results: Path | None = None,
            min_intervals: dict[str, float] | None = None) -> dict[str, Any]:
    end = _parse_instant(as_of)
    if end is None:
        raise ValueError("as_of must be a timezone-aware ISO-8601 timestamp")
    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.^][A-Z0-9._^-]{0,31}", ticker):
        raise ValueError("ticker must use supported symbol characters")
    reference = refs or load_sources()
    environment = dict(env if env is not None else __import__("os").environ)
    start = end - timedelta(days=NEWS_WINDOW_DAYS)
    intervals = min_intervals or {}
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "ticker": ticker,
        "market": market,
        "as_of": _iso(end),
        "window": {"start_exclusive": _iso(start), "end_inclusive": _iso(end), "days": NEWS_WINDOW_DAYS,
                   "recent_window_days": RECENT_WINDOW_DAYS},
        "pit_status": "published_at_only",
        "sources": {
            "sec_edgar": _new_status(),
            "ir_rss": _new_status(),
            "finnhub_rest": _new_status(),
            "finnhub_mcp": _new_status(),
        },
        "articles": [],
        "reference_links": {
            "yahoo_finance_news": (reference.get("reference_links") or {}).get(
                "yahoo_finance_news", "https://finance.yahoo.com/quote/{ticker}/news/").format(ticker=ticker),
            "economist": (reference.get("reference_links") or {}).get("economist", "https://www.economist.com/"),
        },
        "form4_count": 0,
        "form144_count": 0,
        "_primary_rows": [],
        "_third_party_rows": [],
    }
    sec_limiter = RateLimiter(max_calls=8, window_seconds=4,
                              min_interval_seconds=float(intervals.get("sec", 0.5)))
    finnhub_limiter = RateLimiter(max_calls=60, window_seconds=60,
                                  min_interval_seconds=float(intervals.get("finnhub", 1.0)))
    ir_limiters: dict[str, RateLimiter] = {}
    if "sec" in sources:
        _collect_sec(ticker, market, start, end, reference, environment, fetch, sec_limiter, result)
    if "ir" in sources:
        _collect_ir(ticker, market, start, end, reference, fetch, ir_limiters, result)
    if "finnhub" in sources:
        _collect_finnhub(ticker, start, end, reference, environment, fetch, finnhub_limiter,
                         list(aliases or []), result)
    if "mcp" in sources:
        _collect_mcp(mcp_results, ticker, start, end, reference, result)

    raw = list(result.pop("_primary_rows")) + list(result.pop("_third_party_rows"))
    deduped, merged = _deduplicate(raw, reference)
    deduped = deduped[:MAX_ARTICLES]
    for row in deduped:
        row.setdefault("source_tier", classify_source(row, reference))
    result["articles"] = deduped
    result["counts"] = {
        "collected": len(raw),
        "deduplicated": merged,
        "kept": len(deduped),
        **_count_windows(deduped, end),
        "by_tier": {tier: sum(1 for row in deduped if row.get("source_tier") == tier)
                    for tier in ("A_primary", "B_media", "C_aggregator")},
    }
    configured = [row for row in result["sources"].values()
                  if row.get("status") not in {"not_configured", "not_configured_for_ticker", "not_applicable", "not_connected"}]
    if not configured or all(row.get("status") in {"error", "rate_limited"} for row in configured):
        result["status"] = "not_assessed"
        result["reason"] = "all sources failed or were not configured"
    elif any(row.get("status") in {"error", "rate_limited"} for row in configured):
        result["status"] = "partial"
    else:
        result["status"] = "complete" if deduped else "empty"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--market", choices=("US", "HK"), default="US")
    parser.add_argument("--company-aliases", default="", help="comma-separated company/name aliases from yfinance or the caller")
    parser.add_argument("--sources", default="sec,ir,finnhub",
                        help="comma-separated subset of sec,ir,finnhub,mcp")
    parser.add_argument("--mcp-results", type=Path, help="optional host Finnhub MCP result JSON")
    parser.add_argument("--sources-json", type=Path, help="override references/news-sources.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    aliases = [item.strip() for item in args.company_aliases.split(",") if item.strip()]
    try:
        reference = load_sources(args.sources_json)
        result = collect(ticker=args.ticker, as_of=args.as_of, market=args.market, aliases=aliases,
                         sources=tuple(item.strip() for item in args.sources.split(",") if item.strip()),
                         refs=reference, mcp_results=args.mcp_results)
    except Exception as exc:  # noqa: BLE001 - CLI guard
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "kept": len(result["articles"]),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
