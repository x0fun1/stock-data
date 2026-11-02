"""Small adapter helpers; no scoring, sentiment or price-derived processing."""

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit

from ..common.security import safe_url
from ..common.time import iso, parse_time


def identifier(*values) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def instant(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("time_missing")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min, timezone.utc)
    return parse_time(value)


def in_window(value: str | None, start: datetime, end: datetime) -> bool:
    if not value:
        return False
    stamp = instant(value)
    if len(value) == 10:
        # Date-only evidence retains its uncertainty; it is never assigned midnight as publication time.
        return stamp.date() >= start.date() and stamp.date() <= end.date()
    return start < stamp <= end


def intervals(start: datetime, end: datetime, days=7):
    cursor = start
    while cursor < end:
        following = min(cursor + timedelta(days=days), end)
        yield cursor, following
        cursor = following


def interval(start: datetime, end: datetime, reason=None):
    value = {"start": iso(start), "end": iso(end)}
    if reason:
        value["reason"] = reason
    return value


def link(value) -> str:
    """A citation is not a network target; preserve identifying queries but redact secrets."""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return ""
        if parts.port not in {None, 80, 443}:
            return ""
        return safe_url(value) or ""
    except ValueError:
        return ""


def plain(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())


def record(source_id: str, item_id: str, *, title: str, url: str, published_at: str | None, **fields):
    value = {
        "source_id": source_id,
        "source_kind": "official" if source_id in {"sec_submissions", "fed", "fred"} else "unknown",
        "source_item_id": str(item_id),
        "timezone": "America/New_York" if source_id in {"sec_submissions", "fed"} else "UTC",
        "url": link(url),
        "title": title,
        "text": "",
        "text_scope": "headline_only",
        "published_at": published_at,
        "updated_at": None,
        "occurred_at": None,
        "effective_at": None,
        "scheduled_at": None,
        "fetched_at": iso(datetime.now(timezone.utc)),
        "time_precision": "date" if published_at and len(published_at) == 10 else "timestamp",
        "language": "en",
        "origin_id": "",
        "event_key": "",
        "entities": [],
        "category": "company",
        "status": "active",
        "substantive_update": False,
        "original_published_at": published_at,
        "permissions": {"store_text": False, "retention_days": 0},
        "receipt_id": "",
    }
    value.update(fields)
    return value


def result(records=None, *, successful=None, failed=None, exhausted=False, cap_reached=False, cursor=None, limitations=None):
    return {
        "records": records or [],
        "successful_intervals": successful or [],
        "failed_intervals": failed or [],
        "exhausted": exhausted,
        "cap_reached": cap_reached,
        "cursor": cursor,
        "limitations": limitations or [],
        "fetched_count": len(records or []),
    }


def permissions(item: dict) -> dict:
    value = item.get("permissions")
    if not isinstance(value, dict) or value.get("store_text") is not True:
        return {"store_text": False, "retention_days": 0}
    days = value.get("retention_days")
    if type(days) is not int or not 0 < days <= 3650:
        raise ValueError("authorized_retention_days_required")
    return {"store_text": True, "retention_days": days}
