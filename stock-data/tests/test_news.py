from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from stock_research.context.news import build_context, canonical_url, dedupe_events, grade_event
from stock_research.contracts import ContextEvent, EvidenceScope, Grade, SourceKind

UTC = timezone.utc


def event(title, body="", scope="full_text", url="https://example.com/a", published="2024-06-03T12:00:00+00:00"):
    return ContextEvent(
        event_id="",
        market="US",
        security_ids=["US:XNAS:X"],
        title=title,
        published_at=published,
        available_at=published,
        received_at=published,
        source_id="test",
        source_kind=SourceKind.USER_IMPORT,
        evidence_scope=scope,
        url=url,
        body=body,
    )


def test_canonical_url_removes_tracking_parameters():
    assert canonical_url("https://Example.com/a?utm_source=x&b=2#frag") == "https://example.com/a?b=2"


def test_headline_only_is_insufficient():
    e = grade_event(event("Company beats estimates", body="", scope="title"), {})
    assert e.grade is Grade.INSUFFICIENT
    assert e.grade_method == "insufficient_evidence"


def test_full_text_can_be_positive_or_negative():
    positive = grade_event(event("Results", body="The company beats estimates and raises guidance significantly."), {})
    negative = grade_event(event("Results", body="The company misses estimates and cuts guidance."), {})
    assert positive.grade in {Grade.BULLISH, Grade.VERY_BULLISH}
    assert negative.grade in {Grade.BEARISH, Grade.VERY_BEARISH}


def test_duplicate_urls_and_bodies_are_clustered():
    a = event("Same", body="Same body", url="https://example.com/a?utm_source=x")
    b = event("Same", body="Same body", url="https://example.com/a")
    c = event("Other", body="Other body", url="https://example.com/b")
    kept = dedupe_events([a, b, c])
    assert len(kept) == 2
    assert any("duplicate_of" in e.permissions for e in [a, b])


def test_context_excludes_future_events():
    old = event("Old", body="beats estimates", published="2024-06-01T12:00:00+00:00")
    future = event("Future", body="beats estimates", published="2024-06-10T12:00:00+00:00")
    selected = build_context([old, future], datetime(2024, 6, 5, tzinfo=UTC), lookback_days=30, policy={})
    assert [e.title for e in selected] == ["Old"]
    assert selected[0].in_model is False


def test_no_recognized_terms_is_neutral_not_bullish():
    e = grade_event(event("Update", body="The company issued a routine administrative notice."), {})
    assert e.grade is Grade.NEUTRAL
    assert e.grade_reason == "no_recognized_terms"
