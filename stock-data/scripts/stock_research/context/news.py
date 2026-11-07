"""News/event context: deduplication, evidence gates and seven-grade direction."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..common.hashing import canonical_hash
from ..common.timeutil import ensure_utc, now_utc, parse_date, parse_dt
from ..contracts import ContextEvent, EvidenceScope, Grade, Market, SourceKind

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "ref", "cmpid"}

BULLISH = {
    "beat": 2,
    "beats": 2,
    "raise guidance": 3,
    "raised guidance": 3,
    "raises guidance": 3,
    "guidance raise": 3,
    "buyback": 2,
    "repurchase": 2,
    "dividend increase": 2,
    "raises dividend": 2,
    "approval": 2,
    "approved": 2,
    "contract win": 2,
    "wins contract": 2,
    "record revenue": 2,
    "record profit": 2,
    "upgrade": 2,
    "upgraded": 2,
    "outperform": 2,
    "partnership": 1,
    "launch": 1,
    "expansion": 1,
    "strong demand": 1,
    "profit growth": 1,
    "revenue growth": 1,
}

BEARISH = {
    "miss": -2,
    "misses": -2,
    "cut guidance": -3,
    "cuts guidance": -3,
    "lower guidance": -3,
    "guidance cut": -3,
    "lawsuit": -2,
    "investigation": -2,
    "probe": -2,
    "recall": -2,
    "downgrade": -2,
    "downgraded": -2,
    "underperform": -2,
    "bankruptcy": -4,
    "default": -3,
    "fraud": -4,
    "restatement": -3,
    "resignation": -1,
    "layoffs": -2,
    "weak demand": -1,
    "profit warning": -3,
    "impairment": -2,
}

MAGNITUDE = {"record": 1, "major": 1, "significant": 1, "substantial": 1, "minor": -1, "slight": -1, "small": -1}


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def body_hash(text: str) -> str:
    normalised = re.sub(r"\s+", " ", (text or "").strip().lower())
    return canonical_hash(normalised)


def event_id_for(event: ContextEvent) -> str:
    return "evt-" + canonical_hash(
        {
            "url": canonical_url(event.url),
            "title": event.title.strip().lower(),
            "published_at": event.published_at.isoformat() if event.published_at else "",
            "security_ids": sorted(event.security_ids),
        }
    )[:16]


def _scope_allowed(scope: EvidenceScope, allowed: Iterable[str]) -> bool:
    allowed_set = {str(x) for x in allowed}
    return scope.value in allowed_set or scope.value in {"full_text", "official_excerpt"}


def _score(text: str) -> tuple[float, list[str]]:
    lowered = (text or "").lower()
    score = 0.0
    hits: list[str] = []
    for phrase, weight in {**BULLISH, **BEARISH}.items():
        if phrase in lowered:
            score += weight
            hits.append(phrase)
    magnitude = sum(weight for phrase, weight in MAGNITUDE.items() if phrase in lowered)
    return score + (magnitude if score > 0 else -magnitude if score < 0 else 0), hits


def grade_event(event: ContextEvent, policy: Mapping | None = None) -> ContextEvent:
    """Deterministic lexicon grading, gated by admissible evidence scope.

    Headline-only or metadata-only records are never graded above 证据不足.
    A host-provided grade is preserved when it is already set.
    """
    policy = policy or {}
    allowed = policy.get("min_scope_for_grade", ["full_text", "official_excerpt", "numeric"])
    version = str(policy.get("grade_version", "lexicon_v1"))
    if event.grade is not Grade.INSUFFICIENT and event.grade_method not in {"", "insufficient_evidence"}:
        return event
    if not _scope_allowed(event.evidence_scope, allowed):
        event.grade = Grade.INSUFFICIENT
        event.grade_method = "insufficient_evidence"
        event.grade_version = version
        event.grade_reason = f"evidence_scope={event.evidence_scope.value}"
        return event
    text = event.body or event.fact_summary or event.title
    if not text.strip():
        event.grade = Grade.INSUFFICIENT
        event.grade_method = "insufficient_evidence"
        event.grade_version = version
        event.grade_reason = "empty_text"
        return event
    score, hits = _score(text)
    if score >= 5:
        grade = Grade.VERY_BULLISH
    elif score >= 3:
        grade = Grade.BULLISH
    elif score >= 1:
        grade = Grade.MODERATELY_BULLISH
    elif score <= -5:
        grade = Grade.VERY_BEARISH
    elif score <= -3:
        grade = Grade.BEARISH
    elif score <= -1:
        grade = Grade.MODERATELY_BEARISH
    else:
        grade = Grade.NEUTRAL
    event.grade = grade
    event.grade_method = "lexicon"
    event.grade_version = version
    event.grade_reason = "hits=" + ",".join(sorted(set(hits))) if hits else "no_recognized_terms"
    return event


def dedupe_events(events: Iterable[ContextEvent]) -> list[ContextEvent]:
    kept: list[ContextEvent] = []
    seen: dict[str, str] = {}
    for event in sorted(events, key=lambda e: (e.published_at or datetime.min.replace(tzinfo=now_utc().tzinfo))):
        keys = []
        if event.url:
            keys.append("url:" + canonical_url(event.url))
        if event.body:
            keys.append("body:" + body_hash(event.body))
        if event.event_cluster:
            keys.append("cluster:" + event.event_cluster)
        duplicate_of = next((seen[k] for k in keys if k in seen), None)
        if duplicate_of:
            event.permissions = {**event.permissions, "duplicate_of": duplicate_of}
            continue
        event_id = event.event_id or event_id_for(event)
        event.event_id = event_id
        for key in keys:
            seen[key] = event_id
        kept.append(event)
    return kept


def build_context(
    events: Iterable[ContextEvent],
    cutoff: datetime | date | str,
    lookback_days: int,
    policy: Mapping | None = None,
    limit: int = 50,
) -> list[ContextEvent]:
    cutoff_dt = ensure_utc(parse_dt(cutoff))
    start = cutoff_dt - timedelta(days=max(1, int(lookback_days)))
    selected = []
    for event in events:
        known = event.available_at or event.received_at or event.published_at
        if known is None or known > cutoff_dt:
            continue
        if event.published_at is not None and event.published_at < start:
            continue
        selected.append(event)
    graded = [grade_event(e, policy) for e in dedupe_events(selected)]
    graded.sort(key=lambda e: (e.published_at or datetime.min.replace(tzinfo=cutoff_dt.tzinfo)), reverse=True)
    for event in graded:
        event.in_model = False
    return graded[:limit]
