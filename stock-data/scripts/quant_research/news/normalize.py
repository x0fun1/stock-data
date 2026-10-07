"""Normalize timestamped news records and enforce the shared as-of boundary."""

from __future__ import annotations

import math
from typing import Any

from ..contracts import parse_timestamp
from ..security import bounded_text, safe_url


EVENT_SCORE_FIELDS = ("event_strength", "relevance", "magnitude", "novelty", "source_quality")
OPINION_TYPES = {"opinion", "analyst_opinion", "social", "social_post", "commentary"}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = bounded_text(value, 1800)
    return normalized or None


def extract_article_records(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("articles"), list):
        records = data["articles"]
    else:
        return [], ["news.data must be an array or an object with an articles array"]
    return [item for item in records if isinstance(item, dict)], []


def normalize_source_sentiment(
    data: Any, *, available_at: Any, asof_timestamp: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Preserve a provider aggregate only when its point-in-time is verifiable."""
    if not isinstance(data, dict) or not isinstance(data.get("source_sentiment"), dict):
        return None, []
    value = data["source_sentiment"]
    try:
        available = parse_timestamp(value.get("available_at") or available_at)
    except (TypeError, ValueError):
        return None, ["provider sentiment aggregate excluded because no timezone-aware availability timestamp was supplied"]
    if available > parse_timestamp(asof_timestamp):
        return None, ["provider sentiment aggregate excluded because it was fetched or timestamped after the snapshot as-of"]
    result: dict[str, Any] = {
        "source": str(value.get("source", "unknown")),
        "period": value.get("period"),
        "sentiment_source": value.get("sentiment_source"),
        "available_at": available.isoformat().replace("+00:00", "Z"),
    }
    warnings = []
    percent_scale = value.get("percent_scale", "fraction")
    upper = 1 if percent_scale == "fraction" else 100 if percent_scale == "percent" else None
    result["percent_scale"] = percent_scale
    for field in ("sentiment_score", "bullish_percent", "bearish_percent"):
        raw = value.get(field)
        try:
            score = float(raw) if raw is not None and not isinstance(raw, bool) else None
        except (TypeError, ValueError):
            score = None
        valid = score is not None and math.isfinite(score)
        if field != "sentiment_score":
            valid = valid and upper is not None and 0 <= score <= upper
        elif value.get("sentiment_score_scale") == "minus_one_to_one":
            valid = valid and -1 <= score <= 1
        result[field] = score if valid else None
        if raw is not None and not valid:
            warnings.append(f"provider {field} excluded: invalid or unspecified scale/range")
    if upper is not None and result["bullish_percent"] is not None and result["bearish_percent"] is not None and result["bullish_percent"] + result["bearish_percent"] > upper + 1e-6:
        result["bullish_percent"] = result["bearish_percent"] = None
        warnings.append("provider bullish/bearish shares exceed the declared scale")
    result["ticker"] = _optional_text(value.get("ticker"))
    return result, warnings


def normalize_articles(
    data: Any,
    *,
    ticker: str,
    asof_timestamp: str,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    records, warnings = extract_article_records(data)
    cutoff = parse_timestamp(asof_timestamp)
    normalized: list[dict[str, Any]] = []
    excluded = {"invalid_record": 0, "missing_title": 0, "missing_or_invalid_published_at": 0, "after_asof": 0, "updated_after_asof": 0}
    truncated_titles = 0
    truncated_summaries = 0
    if len(records) > 500:
        warnings.append("article analysis bounded to 500 most recently published records")
        records = sorted(records, key=lambda row: str(row.get("published_at", "")), reverse=True)[:500]
    for record in records:
        raw_title = record.get("title")
        title = _optional_text(raw_title) or ""
        if raw_title is not None and len(str(raw_title).strip()) > 400:
            truncated_titles += 1
        if not title:
            excluded["missing_title"] += 1
            continue
        try:
            published = parse_timestamp(record.get("published_at"))
        except (TypeError, ValueError):
            excluded["missing_or_invalid_published_at"] += 1
            continue
        if published > cutoff:
            excluded["after_asof"] += 1
            continue
        updated_at = record.get("updated_at")
        updated = None
        if updated_at not in (None, ""):
            try:
                updated = parse_timestamp(updated_at)
            except (TypeError, ValueError):
                excluded["missing_or_invalid_published_at"] += 1
                continue
            if updated > cutoff:
                excluded["updated_after_asof"] += 1
                continue
        times = {}
        invalid_time = False
        for field in ("occurred_at", "announcement_at", "scheduled_at"):
            if record.get(field) in (None, ""):
                times[field] = None
                continue
            try:
                stamp = parse_timestamp(record[field])
                if field != "scheduled_at" and stamp > cutoff:
                    raise ValueError("occurred/announcement after as-of")
                times[field] = stamp.isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError):
                invalid_time = True
        if invalid_time:
            excluded["missing_or_invalid_published_at"] += 1
            continue
        scores: dict[str, float | None] = {}
        for field in EVENT_SCORE_FIELDS:
            raw_score = record.get(field)
            try:
                score = float(raw_score) if raw_score is not None and not isinstance(raw_score, bool) else None
            except (TypeError, ValueError):
                score = None
            scores[field] = score if score is not None and math.isfinite(score) and 0 <= score <= 1 else None
        raw_sentiment = record.get("sentiment_score")
        try:
            sentiment_score = float(raw_sentiment) if raw_sentiment is not None and not isinstance(raw_sentiment, bool) else None
        except (TypeError, ValueError):
            sentiment_score = None
        scores["sentiment_score"] = sentiment_score if sentiment_score is not None and math.isfinite(sentiment_score) and -1 <= sentiment_score <= 1 else None
        record_type = (_optional_text(record.get("record_type")) or "unknown").lower()
        if record_type not in OPINION_TYPES | {"event", "report", "news", "unknown"}:
            warnings.append(f"ignored unsupported record_type {record_type!r}; treated as unknown")
            record_type = "unknown"
        tickers = record.get("tickers", [])
        if not isinstance(tickers, list):
            tickers = []
        raw_summary = record.get("summary")
        if raw_summary is not None and len(str(raw_summary).strip()) > 1800:
            truncated_summaries += 1
        normalized.append({
            "article_id": _optional_text(record.get("article_id")),
            "url": safe_url(record.get("url")),
            "title": title[:400],
            "summary": _optional_text(raw_summary),
            "published_at": published.isoformat().replace("+00:00", "Z"),
            "updated_at": updated.isoformat().replace("+00:00", "Z") if updated else None,
            **times,
            "source": _optional_text(record.get("source")) or "unknown",
            "source_type": (_optional_text(record.get("source_type")) or "news").lower(),
            "record_type": record_type,
            "tickers": sorted({str(item).strip().upper() for item in tickers if str(item).strip()}),
            "event_type_hint": (_optional_text(record.get("event_type", record.get("event_type_hint"))) or "").upper() or None,
            "canonical_event_id": _optional_text(record.get("canonical_event_id")),
            "original_reporting_id": _optional_text(record.get("original_reporting_id")),
            "event_group": record.get("event_group") if record.get("event_group") in {"company", "industry", "macro"} else None,
            "exposure_tickers": sorted({str(item).upper() for item in record.get("exposure_tickers", [])}) if isinstance(record.get("exposure_tickers"), list) else [],
            "exposure_basis": _optional_text(record.get("exposure_basis")),
            **scores,
        })
    if any(excluded.values()):
        summary = ", ".join(f"{key}={value}" for key, value in excluded.items() if value)
        warnings.append(f"excluded news records at the point-in-time boundary: {summary}")
    if truncated_titles:
        warnings.append(f"{truncated_titles} news headline(s) were truncated to 400 characters")
    if truncated_summaries:
        warnings.append(f"{truncated_summaries} news summary/summaries were truncated to 1,800 characters")
    if not normalized and records:
        warnings.append("no news records remained after timestamp and schema validation")
    return normalized, excluded, warnings


def is_opinion_record(article: dict[str, Any]) -> bool:
    source_type = str(article.get("source_type", "")).lower()
    return article.get("record_type") in OPINION_TYPES or source_type in {
        "social", "social_media", "stocktwits", "reddit", "opinion", "analyst_opinion"
    }


def record_role(article: dict[str, Any]) -> str:
    if is_opinion_record(article):
        return "opinion"
    if article.get("record_type") in {"event", "report", "news"}:
        return "event"
    return "unknown"
