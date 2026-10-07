"""Conservatively merge repeated reports without counting copies as new events."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..contracts import parse_timestamp
from .normalize import record_role


def _clean_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().lower() or None
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def _title_key(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _exact_keys(article: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    source = str(article.get("source", "unknown")).strip().casefold()
    if article.get("article_id"):
        keys.add(f"id:{source}:{str(article['article_id']).strip().casefold()}")
    url = _clean_url(article.get("url"))
    if url:
        keys.add(f"url:{url}")
    return keys


def deduplicate_articles(articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Return report clusters and the number of records absorbed as duplicates.

    Exact IDs/URLs are removed first. Remaining same-class headlines are merged
    only when they are close in wording and published within 72 hours.
    """
    exact_seen: dict[str, set[tuple[Any, ...]]] = {}
    unique: list[dict[str, Any]] = []
    duplicate_count = 0
    for article in sorted(articles, key=lambda row: row.get("published_at", "")):
        role = record_role(article)
        identities = {f"{role}:{identity}" for identity in _exact_keys(article)}
        fingerprint = (str(article.get("title", "")).casefold(), str(article.get("summary") or "").casefold(), tuple(article.get("tickers", [])), article.get("occurred_at"), article.get("published_at"))
        if any(fingerprint in exact_seen.get(identity, set()) for identity in identities):
            duplicate_count += 1
            continue
        for identity in identities:
            exact_seen.setdefault(identity, set()).add(fingerprint)
        unique.append(dict(article))

    clusters: list[dict[str, Any]] = []
    for article in unique:
        title_key = _title_key(str(article.get("title", "")))
        published = parse_timestamp(article["published_at"])
        role = record_role(article)
        match: dict[str, Any] | None = None
        for cluster in reversed(clusters):
            if cluster["role"] != role:
                continue
            previous = cluster["articles"][0]
            if set(article.get("tickers", [])) != set(previous.get("tickers", [])):
                continue
            if article.get("occurred_at") and previous.get("occurred_at") and article["occurred_at"][:10] != previous["occurred_at"][:10]:
                continue
            if article.get("canonical_event_id") and article.get("canonical_event_id") == previous.get("canonical_event_id"):
                match = cluster
                break
            delta = abs((published - cluster["first_published"]).total_seconds())
            if delta > timedelta(hours=72).total_seconds():
                continue
            if not title_key or not cluster["title_key"]:
                continue
            similarity = SequenceMatcher(None, title_key, cluster["title_key"]).ratio()
            if title_key == cluster["title_key"] or similarity >= 0.88:
                match = cluster
                break
        if match is None:
            clusters.append({
                "role": role,
                "first_published": published,
                "title_key": title_key,
                "articles": [article],
            })
        else:
            match["articles"].append(article)
            match["first_published"] = min(match["first_published"], published)
            duplicate_count += 1

    result: list[dict[str, Any]] = []
    for cluster in clusters:
        rows = cluster["articles"]
        first_published = min(parse_timestamp(row["published_at"]) for row in rows)
        representative = max(rows, key=lambda row: len(str(row.get("title", ""))) + len(str(row.get("summary") or "")))
        identity_seed = f"{first_published.isoformat()}|{cluster['title_key']}|{cluster['role']}"
        result.append({
            "cluster_id": "cluster_" + hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:12],
            "first_published_at": first_published.isoformat().replace("+00:00", "Z"),
            "representative": representative,
            "articles": rows,
            "sources": sorted({str(row.get("source", "unknown")) for row in rows}),
            "independent_reporting_ids": sorted({row["original_reporting_id"] for row in rows if row.get("original_reporting_id")}),
            "article_count": len(rows),
            "role": cluster["role"],
            "is_opinion": cluster["role"] == "opinion",
        })
    result.sort(key=lambda item: item["first_published_at"], reverse=True)
    return result, duplicate_count
