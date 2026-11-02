#!/usr/bin/env python3
"""Verify news candidates with TinyFish Search/Fetch (standard library only).

The program filters result domains and dates; it never decides whether two
headlines describe the same event.  The host AI confirms that in
``news_ai_assessment.json`` and supplies the evidence link.  Fetch text and
search snippets are deliberately discarded.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
TOOL_USER_AGENT = "stock-data-skill/4.0"
REQUEST_TIMEOUT_SECONDS = 10
MAX_VERIFY_ITEMS = 30
MAX_SEARCH_PER_ITEM = 1
MAX_FETCH_PER_ITEM = 3
DEFAULT_REFS = Path(__file__).resolve().parent.parent / "references" / "news-sources.json"

# These domains may appear in Search results, but must never be fetched.
FORBIDDEN_FETCH_DOMAINS = (
    "finance.yahoo.com", "yahoo.com", "economist.com", "reuters.com",
    "apnews.com", "news.google.com", "google.com",
)

FetchJson = Callable[[str, str, dict[str, str], dict[str, str] | None],
                     tuple[int | None, dict[str, Any] | None, str | None]]


class RateLimiter:
    def __init__(self, *, max_calls: int, window_seconds: float, min_interval_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.min_interval_seconds = min_interval_seconds
        self.sleep = sleep
        self.calls: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        self.calls = [item for item in self.calls if now - item < self.window_seconds]
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
            self.calls = [item for item in self.calls if now - item < self.window_seconds]
        self.calls.append(now)


def _json_request(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None,
                  timeout: int = REQUEST_TIMEOUT_SECONDS):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed TinyFish HTTPS hosts
            return response.status, json.loads(response.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, None, f"network_error: {type(exc.reason).__name__}"
    except TimeoutError:
        return None, None, "timeout"
    except json.JSONDecodeError:
        return None, None, "invalid_json"
    except Exception as exc:  # noqa: BLE001 - one failure must not abort the run
        return None, None, f"{type(exc).__name__}"


def load_sources(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or DEFAULT_REFS).read_text(encoding="utf-8"))


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_SUFFIXES = ("co.uk", "com.au", "co.jp", "com.cn", "com.hk", "co.kr", "com.sg")


def _registrable_domain(host: str) -> str:
    labels = [part for part in host.lower().split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _domain_in(host: str, domains: list[str]) -> bool:
    return any(host == item or host.endswith("." + item) for item in domains)


def _tier_for_domain(host: str, refs: dict[str, Any]) -> str:
    tiers = refs.get("source_tiers") or {}
    if _domain_in(host, list(tiers.get("A_primary") or [])):
        return "A_primary"
    if _domain_in(host, list(tiers.get("B_media") or [])):
        return "B_media"
    if _domain_in(host, list(tiers.get("C_aggregator") or [])):
        return "C_aggregator"
    return "unclassified"


def fetch_allowed(url: str, refs: dict[str, Any]) -> bool:
    """Fetch only SEC, registered IR and B_media; hard-reject forbidden domains."""
    host = _host(url)
    if not host or _domain_in(host, list(FORBIDDEN_FETCH_DOMAINS)):
        return False
    if _domain_in(host, list((refs.get("source_tiers") or {}).get("A_primary") or [])):
        return True
    if _domain_in(host, list((refs.get("source_tiers") or {}).get("B_media") or [])):
        return True
    return False


def _search_query(candidate: dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(candidate.get("title") or "")).strip()
    return title[:300]


def _relative_date(value: str, now: datetime) -> datetime | None:
    text = re.sub(r"\s+", " ", value.strip().casefold())
    match = re.fullmatch(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        seconds = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800,
                   "month": 2592000, "year": 31536000}[unit]
        return now - timedelta(seconds=amount * seconds)
    if text == "today":
        return now
    if text == "yesterday":
        return now - timedelta(days=1)
    return None


def _result_date(result: dict[str, Any], now: datetime) -> tuple[datetime | None, str]:
    for key in ("date", "published_at", "published_date"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        moment = _parse_instant(text)
        if moment is not None:
            precision = "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else "timestamp"
            return moment, precision
        relative = _relative_date(text, now)
        if relative is not None:
            return relative, "relative"
        return None, "date_unknown"
    return None, "date_unknown"


def _search(candidate: dict[str, Any], *, key: str, as_of: datetime, now: datetime,
            refs: dict[str, Any], fetch_json: FetchJson, limiter: RateLimiter) -> dict[str, Any]:
    published = _parse_instant(candidate.get("published_at")) or as_of
    after = (published - timedelta(days=1)).date().isoformat()
    before = as_of.date().isoformat()
    params = {
        "query": _search_query(candidate),
        "domain_type": "news",
        "after_date": after,
        "before_date": before,
    }
    limiter.wait()
    code, payload, error = fetch_json("GET", SEARCH_URL + "?" + urllib.parse.urlencode(params),
                                      {"X-API-Key": key, "User-Agent": TOOL_USER_AGENT,
                                       "Accept": "application/json"}, None)
    if code in {402, 429}:
        return {"status": "rate_limited", "http_status": code, "error": error or "quota_or_rate_limit",
                "results": []}
    if error or code != 200 or not isinstance(payload, dict):
        return {"status": "error", "http_status": code, "error": error or f"HTTP {code}", "results": []}
    raw_results = payload.get("results")
    results = []
    if isinstance(raw_results, list):
        for row in raw_results:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            moment, precision = _result_date(row, now)
            # Search date filters are calendar-day bounds; compare by date so a
            # date-only result on the after_date day is not dropped.  A result
            # whose date cannot be resolved is retained for disclosure but is not
            # usable verification evidence.
            if moment is not None and not (published.date() - timedelta(days=1) <= moment.date() <= as_of.date()):
                continue
            host = _host(str(row["url"]))
            results.append({
                "url": str(row["url"]),
                "domain": host,
                "registrable_domain": _registrable_domain(host),
                "tier": _tier_for_domain(host, refs),
                "title": str(row.get("title") or ""),
                "date": _iso(moment) if moment else None,
                "date_precision": precision,
                "usable": moment is not None,
            })
    return {"status": "ok", "http_status": code, "error": None, "results": results}


def _fetch_evidence(urls: list[str], *, key: str, refs: dict[str, Any], fetch_json: FetchJson,
                    limiter: RateLimiter, max_urls: int) -> dict[str, Any]:
    allowed = []
    for url in urls:
        if len(allowed) >= max_urls:
            break
        if fetch_allowed(url, refs) and url not in allowed:
            allowed.append(url)
    if not allowed:
        return {"status": "skipped", "requested": 0, "results": [], "errors": []}
    limiter.wait()
    code, payload, error = fetch_json("POST", FETCH_URL,
                                      {"X-API-Key": key, "User-Agent": TOOL_USER_AGENT,
                                       "Content-Type": "application/json"},
                                      {"urls": allowed, "format": "markdown", "ttl": 0})
    if code in {402, 429}:
        return {"status": "rate_limited", "http_status": code, "requested": len(allowed),
                "results": [], "errors": [error or "quota_or_rate_limit"]}
    if error or code != 200 or not isinstance(payload, dict):
        return {"status": "error", "http_status": code, "requested": len(allowed),
                "results": [], "errors": [error or f"HTTP {code}"]}
    results = []
    for row in payload.get("results") or []:
        if isinstance(row, dict) and row.get("url"):
            results.append({"url": str(row.get("final_url") or row["url"]), "domain": _host(str(row["url"]))})
    errors = [{"url": str(row.get("url")), "error": str(row.get("error"))}
              for row in payload.get("errors") or [] if isinstance(row, dict)]
    return {"status": "ok", "http_status": code, "requested": len(allowed),
            "results": results, "errors": errors}


def _norm_title(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+[-–—]\s+[^-–—]{1,50}$", "", text)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _status_from_evidence(candidate: dict[str, Any], results: list[dict[str, Any]],
                          refs: dict[str, Any]) -> str:
    usable = [row for row in results if row.get("usable")]
    if any(row["tier"] == "A_primary" for row in usable):
        return "primary_confirmed"
    original_domain = _registrable_domain(_host(str(candidate.get("url") or "")))
    candidate_title = _norm_title(candidate.get("title"))
    matched_original = next(
        (row for row in usable
         if (original_domain and row["registrable_domain"] == original_domain)
         or (candidate_title and _norm_title(row.get("title")) == candidate_title)),
        None,
    )
    media_domains = {row["registrable_domain"] for row in usable
                     if row["tier"] == "B_media" and row["registrable_domain"] != original_domain}
    if len(media_domains) >= 2:
        return "multi_source"
    if matched_original is not None:
        other_domains = media_domains - {matched_original["registrable_domain"]}
        if not other_domains:
            return "single_source"
    return "unverified"


def _eligible_for_verification(candidate: dict[str, Any]) -> bool:
    relevance = str(candidate.get("relevance") or "").casefold()
    if relevance in {"unrelated", "unassessed", "indirect", "not_explicitly_matched"}:
        return False
    ticker_relevance = str(candidate.get("ticker_relevance") or "").casefold()
    return ticker_relevance not in {"unrelated", "unassessed", "indirect"}


def verify(*, candidates: dict[str, Any], as_of: str | None = None, key: str | None = None,
           refs: dict[str, Any] | None = None,
           fetch_json: FetchJson = _json_request,
           max_items: int = MAX_VERIFY_ITEMS,
           search_limiter: RateLimiter | None = None,
           fetch_limiter: RateLimiter | None = None,
           today: date | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    reference = refs or load_sources()
    ticker = str(candidates.get("ticker") or "").upper()
    as_of_text = as_of or candidates.get("as_of")
    as_of_moment = _parse_instant(as_of_text)
    if as_of_moment is None:
        raise ValueError("as_of must be a timezone-aware ISO-8601 timestamp")
    now_moment = now or datetime.now(timezone.utc)
    run_day = today or now_moment.date()
    verification_pit = "live" if as_of_moment.date() >= run_day else "post_hoc"
    articles = candidates.get("articles") if isinstance(candidates.get("articles"), list) else []
    source_rows = [row for row in articles if isinstance(row, dict)]
    # SEC/IR are primary evidence by construction; no request is made.
    items: list[dict[str, Any]] = []
    third_party: list[dict[str, Any]] = []
    for row in source_rows:
        source_id = row.get("source_id")
        base = {
            "article_id": row.get("article_id"),
            "title": row.get("title"),
            "url": row.get("url"),
            "published_at": row.get("published_at"),
            "source": row.get("source"),
            "via": row.get("via"),
            "origin_source": row.get("origin_source"),
            "relevance": row.get("relevance"),
            "ticker_relevance": row.get("ticker_relevance"),
            "source_id": source_id,
            "source_tier": row.get("source_tier"),
            "verification_pit": verification_pit,
            "requires_host_confirmation": True,
        }
        if source_id in {"sec_edgar", "ir_rss"} or row.get("source_tier") == "A_primary":
            base.update(status="primary_confirmed", evidence_links=[{
                "url": row.get("url"), "domain": _host(str(row.get("url") or "")),
                "tier": "A_primary", "date": row.get("published_at")}],
                search_status="not_requested", fetch_status="not_requested", reason=None)
            items.append(base)
        else:
            base.update(status="not_assessed", evidence_links=[], search_status="pending",
                        fetch_status="pending", reason=None)
            third_party.append(base)
    items.extend(third_party)

    if not key:
        for item in third_party:
            item.update(status="not_assessed", search_status="not_assessed",
                        fetch_status="not_assessed", reason="TINYFISH_API_KEY is not set")
        return _envelope(candidates, ticker, as_of_moment, verification_pit, "not_assessed",
                         items, requests={"search": 0, "fetch": 0}, limits=max_items)

    # Relevance filtering happens before the fixed-order selection.  Items that
    # are not directly relevant are disclosed as not assessed, never searched.
    direct: list[dict[str, Any]] = []
    for item in third_party:
        if _eligible_for_verification(item):
            direct.append(item)
        else:
            item.update(status="not_assessed", search_status="not_assessed",
                        fetch_status="not_requested", reason="not_directly_relevant")
    rank = {"B_media": 0, "C_aggregator": 1, "unclassified": 2}
    ordered = sorted(direct, key=lambda item: (
        rank.get(str(item.get("source_tier")), 2),
        -(_parse_instant(item.get("published_at")).timestamp() if _parse_instant(item.get("published_at")) else 0),
    ))
    search_limiter = search_limiter or RateLimiter(max_calls=30, window_seconds=60,
                                                   min_interval_seconds=1.0)
    fetch_limiter = fetch_limiter or RateLimiter(max_calls=30, window_seconds=60,
                                                  min_interval_seconds=1.0)
    search_requests = fetch_requests = 0
    rate_limited = False
    for index, item in enumerate(ordered):
        if rate_limited:
            item.update(status="not_assessed", search_status="not_assessed",
                        fetch_status="not_requested", reason="rate_limited")
            continue
        if index >= max_items:
            item.update(status="not_assessed", search_status="not_assessed",
                        fetch_status="not_requested", reason=f"verification cap {max_items} reached")
            continue
        search_requests += MAX_SEARCH_PER_ITEM
        search = _search(item, key=key, as_of=as_of_moment, now=now_moment, refs=reference,
                         fetch_json=fetch_json, limiter=search_limiter)
        item["search_status"] = search["status"]
        if search["status"] == "rate_limited":
            rate_limited = True
            item.update(status="not_assessed", fetch_status="not_requested", reason="rate_limited")
            continue
        if search["status"] != "ok":
            item.update(status="not_assessed", fetch_status="not_requested",
                        reason=search.get("error") or search["status"])
            continue
        evidence = search["results"]
        usable = [row for row in evidence if row.get("usable")]
        item["date_unknown_results"] = len(evidence) - len(usable)
        status = _status_from_evidence(item, usable, reference)
        if status == "unverified" and not usable:
            item.update(status="unverified", evidence_links=[], fetch_status="not_requested",
                        reason="search returned no usable dated result")
            continue
        fetch = _fetch_evidence([row["url"] for row in usable], key=key, refs=reference,
                                fetch_json=fetch_json, limiter=fetch_limiter, max_urls=MAX_FETCH_PER_ITEM)
        fetch_requests += fetch.get("requested", 0)
        item["fetch_status"] = fetch["status"]
        item["evidence_links"] = usable[:MAX_FETCH_PER_ITEM]
        item["status"] = status
        item["reason"] = None
        if fetch["status"] == "rate_limited":
            rate_limited = True
            item["reason"] = "rate_limited"
    tinyfish_status = "ok"
    if rate_limited:
        assessed = any(item.get("status") in {"primary_confirmed", "multi_source", "single_source", "unverified"}
                       for item in items)
        tinyfish_status = "partial" if assessed else "rate_limited"
    return _envelope(candidates, ticker, as_of_moment, verification_pit, tinyfish_status,
                     items, requests={"search": search_requests, "fetch": fetch_requests},
                     limits=max_items)


def _envelope(candidates: dict[str, Any], ticker: str, as_of: datetime, pit: str, tinyfish_status: str,
              items: list[dict[str, Any]], *, requests: dict[str, int], limits: int) -> dict[str, Any]:
    counts = {status: sum(1 for item in items if item.get("status") == status)
              for status in ("primary_confirmed", "multi_source", "single_source", "unverified", "not_assessed")}
    return {
        "schema_version": "1.0",
        "ticker": ticker,
        "as_of": _iso(as_of),
        "verification_pit": pit,
        "tinyfish_status": tinyfish_status,
        "limits": {"max_items": limits, "max_search_per_item": MAX_SEARCH_PER_ITEM,
                   "max_fetch_per_item": MAX_FETCH_PER_ITEM},
        "requests": requests,
        "counts": counts,
        "items": items,
        "limits_note": (
            "multi_source counts distinct registrable whitelist domains; syndicated copies cannot be "
            "detected, and post_hoc items must not be rated strong."
        ),
        "source_note": "Search snippets and Fetch text are not stored.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", help="override the candidates as-of (must not move past it)")
    parser.add_argument("--sources-json", type=Path)
    args = parser.parse_args(argv)
    try:
        candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
        if args.as_of and _parse_instant(args.as_of) is None:
            raise ValueError("--as-of must be a timezone-aware ISO-8601 timestamp")
        result = verify(candidates=candidates, as_of=args.as_of,
                        key=__import__("os").environ.get("TINYFISH_API_KEY"),
                        refs=load_sources(args.sources_json))
    except Exception as exc:  # noqa: BLE001 - CLI guard
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"status": result["tinyfish_status"], "counts": result["counts"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
