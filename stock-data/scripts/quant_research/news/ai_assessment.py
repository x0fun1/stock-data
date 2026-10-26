"""Validate and attach a host-supplied AI headline-level news lean.

The Skill package stays offline and deterministic: it never calls a model itself.
The host agent (which already reads the frozen News input) may produce a
structured ``news_ai_assessment`` artifact, exactly like the collection adapter
receives provider DATA. This module only validates that artifact against the
records actually analyzed and attaches it as *display evidence*.

Hard invariants enforced here:

* The artifact may only reference news ids that exist in this run's candidate
  set; invented headlines/ids are rejected.
* Only allow-listed fields are accepted, and any Quant vocabulary inside string
  values rejects the artifact, so a model cannot smuggle a probability or a
  direction into the News result.
* The merged lean never touches ``direction``, ``overall_direction``,
  ``confidence``, ``sentiment``, ``market_confirmation``, ``event_strength`` or
  ``effective_event_score``. It cannot change Quant ``P(up)`` either, because the
  News stage never sees a Quant result.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..contracts import parse_timestamp
from ..security import bounded_text, safe_url

AI_ASSESSMENT_SCHEMA_VERSION = "1.0"
AI_LEAN_BASIS = "headline_level_ai_screen_v1"
AI_LEAN_LAYER = "host_supplied_headline_lean_v1"

LEAN_LABELS = ("bullish", "bearish", "mixed", "neutral", "unclear")
EVIDENCE_GRADES = (
    "reported_event",
    "analyst_opinion",
    "headline_only",
    "rumour",
    "unclear",
)

_MAX_ITEMS = 500
_REASON_LIMIT = 400
_HEADLINE_LIMIT = 300

_TOP_KEYS = {"schema_version", "ticker", "asof_timestamp", "basis", "model", "items"}
_ITEM_KEYS = {
    "news_id",
    "headline",
    "source",
    "published_at",
    "url",
    "lean",
    "reason",
    "evidence_grade",
    "relevance_basis",
    "target_relevance",
    "importance",
}
_MODEL_KEYS = {"id", "prompt_sha256", "generated_at_utc"}

# A model must not be able to write Quant-facing vocabulary into the News stage.
_FORBIDDEN_TOKENS = (
    "prob_up",
    "p(up)",
    "probability",
    "probabilities",
    "alignment",
    "consensus",
    "quant_result",
    "confidence_score",
    "calibrated_probability",
    "报告概率",
    "上涨概率",
)

TARGET_RELEVANCE = ("direct", "indirect", "unrelated", "unclear")
IMPORTANCE = ("major", "routine")


def _reject(message: str) -> None:
    raise ValueError(message)


def _text(value: Any, *, field: str, limit: int, required: bool = True) -> str | None:
    if value is None:
        if required:
            _reject(f"ai assessment {field} is required")
        return None
    if not isinstance(value, str):
        _reject(f"ai assessment {field} must be a string")
    text = bounded_text(value, limit)
    if required and not text:
        _reject(f"ai assessment {field} must not be empty")
    return text or None


def _scan_forbidden(value: str, *, field: str) -> None:
    folded = value.casefold()
    for token in _FORBIDDEN_TOKENS:
        if token in folded:
            _reject(f"ai assessment {field} contains Quant vocabulary {token!r}; the lean layer must not carry Quant fields")


def _lean_identity(assessment: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        repr(sorted(assessment.items())).encode("utf-8", "replace")
    ).hexdigest()[:12]
    return f"ai_lean_{digest}"


def _identity_mismatch(provided: str | None, recorded: Any, *, field: str) -> str | None:
    """Return a rejection reason when a supplied identity field contradicts the record.

    A headline is truncated to 300 characters, so a value that is a prefix of the
    recorded headline is accepted as the same text; anything else is a forgery
    attempt and rejects the item.  This is the difference between "the host
    repeated the headline" and "the host invented one".
    """

    if provided is None:
        return None
    recorded_text = str(recorded or "").strip()
    if not recorded_text:
        return f"ai assessment {field} was supplied but the analysed record has no {field} to verify it against"
    provided_text = str(provided).strip()
    if provided_text and (provided_text == recorded_text or recorded_text.startswith(provided_text)):
        return None
    return f"ai assessment {field} does not match the analysed record; identity fields must be copied, not authored"


def candidate_index(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map every analyzable news id to its record, including pre-gate records."""
    index: dict[str, dict[str, Any]] = {}
    for row in candidates:
        news_id = row.get("news_id")
        if isinstance(news_id, str) and news_id and news_id not in index:
            index[news_id] = row
    return index


def validate_ai_assessment(
    assessment: Any,
    *,
    ticker: str,
    asof_timestamp: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the normalized lean items or raise ValueError.

    The caller turns a rejection into an ``invalid`` lean block without touching
    the deterministic News result, so a bad host artifact cannot erase evidence.
    """
    if not isinstance(assessment, dict):
        _reject("ai assessment must be a JSON object")
    unknown = sorted(set(assessment) - _TOP_KEYS)
    if unknown:
        _reject(f"ai assessment has unsupported field(s): {', '.join(unknown)}")
    schema_version = assessment.get("schema_version")
    if schema_version != AI_ASSESSMENT_SCHEMA_VERSION:
        _reject(f"ai assessment schema_version must be {AI_ASSESSMENT_SCHEMA_VERSION!r}")
    if assessment.get("ticker") != ticker:
        _reject("ai assessment ticker does not match the analyzed ticker")
    if assessment.get("asof_timestamp") != asof_timestamp:
        _reject("ai assessment as-of does not match the snapshot as-of")
    basis = assessment.get("basis")
    if basis is not None and basis != AI_LEAN_BASIS:
        _reject(f"ai assessment basis must be {AI_LEAN_BASIS!r} when supplied")
    model = assessment.get("model")
    if model is not None:
        if not isinstance(model, dict):
            _reject("ai assessment model must be an object")
        model_unknown = sorted(set(model) - _MODEL_KEYS)
        if model_unknown:
            _reject(f"ai assessment model has unsupported field(s): {', '.join(model_unknown)}")
        for key in sorted(set(model)):
            _text(model[key], field=f"model.{key}", limit=200)
    raw_items = assessment.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        _reject("ai assessment items must be a non-empty array")
    if len(raw_items) > _MAX_ITEMS:
        _reject(f"ai assessment may carry at most {_MAX_ITEMS} items")

    known = candidate_index(candidates)
    if not known:
        _reject("ai assessment was supplied but this run has no analyzable news records")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            _reject(f"ai assessment item {position} must be an object")
        item_unknown = sorted(set(raw) - _ITEM_KEYS)
        if item_unknown:
            _reject(f"ai assessment item {position} has unsupported field(s): {', '.join(item_unknown)}")
        news_id = _text(raw.get("news_id"), field=f"item {position} news_id", limit=120)
        if news_id not in known:
            _reject(f"ai assessment item {position} references unknown news id {news_id!r}")
        if news_id in seen:
            _reject(f"ai assessment repeats news id {news_id!r}")
        seen.add(news_id)
        lean = raw.get("lean")
        if lean not in LEAN_LABELS:
            _reject(f"ai assessment item {position} lean must be one of {', '.join(LEAN_LABELS)}")
        evidence_grade = raw.get("evidence_grade", "unclear")
        if evidence_grade not in EVIDENCE_GRADES:
            _reject(f"ai assessment item {position} evidence_grade must be one of {', '.join(EVIDENCE_GRADES)}")
        target_relevance = raw.get("target_relevance", "unclear")
        if target_relevance not in TARGET_RELEVANCE:
            _reject(f"ai assessment item {position} target_relevance must be one of {', '.join(TARGET_RELEVANCE)}")
        importance = raw.get("importance", "routine")
        if importance not in IMPORTANCE:
            _reject(f"ai assessment item {position} importance must be one of {', '.join(IMPORTANCE)}")
        headline = _text(raw.get("headline"), field=f"item {position} headline", limit=_HEADLINE_LIMIT, required=False)
        reason = _text(raw.get("reason"), field=f"item {position} reason", limit=_REASON_LIMIT)
        source = _text(raw.get("source"), field=f"item {position} source", limit=120, required=False)
        published_at = _text(raw.get("published_at"), field=f"item {position} published_at", limit=40, required=False)
        relevance_basis = _text(raw.get("relevance_basis"), field=f"item {position} relevance_basis", limit=60, required=False)
        url = raw.get("url")
        if url is not None:
            url = safe_url(_text(url, field=f"item {position} url", limit=600))
        for field, value in (("headline", headline), ("reason", reason), ("source", source)):
            if value:
                _scan_forbidden(value, field=f"item {position} {field}")
        record = known[news_id]
        if headline is not None:
            # A headline reaches report.md; newlines would break the fixed block.
            headline = headline.replace("\n", " ").replace("\r", " ")
        for field, value, recorded in (
            ("headline", headline, record.get("headline")),
            ("source", source, record.get("source")),
            ("url", url, record.get("url")),
        ):
            mismatch = _identity_mismatch(value, recorded, field=f"item {position} {field}")
            if mismatch:
                _reject(mismatch)
        if published_at is not None:
            recorded_at = record.get("published_at") or record.get("first_published_at")
            try:
                same_instant = bool(recorded_at) and parse_timestamp(published_at) == parse_timestamp(recorded_at)
            except (TypeError, ValueError):
                same_instant = False
            if not same_instant:
                _reject(f"ai assessment item {position} published_at does not match the analysed record")
        items.append({
            "news_id": news_id,
            "lean": lean,
            "reason": reason,
            "evidence_grade": evidence_grade,
            # Display identity is always the analysed record's own text; the
            # artifact is only allowed to confirm it, never to rewrite it.
            "headline": record.get("headline"),
            "source": record.get("source"),
            "published_at": record.get("published_at") or record.get("first_published_at"),
            "url": record.get("url"),
            "ticker_relevance": record.get("ticker_relevance"),
            "record_kind": record.get("record_kind"),
            "eligible_evidence": record.get("eligible_evidence"),
            "target_relevance": target_relevance,
            "importance": importance,
            "relevance_basis": relevance_basis or "source_recorded",
            "display_only": True,
        })
    return {
        "schema_version": AI_ASSESSMENT_SCHEMA_VERSION,
        "basis": AI_LEAN_BASIS,
        "lean_id": _lean_identity(assessment),
        "model": dict(model) if isinstance(model, dict) else None,
        "candidate_count": len(known),
        "items": items,
    }


def _bucket(items: list[dict[str, Any]], labels: tuple[str, ...]) -> list[dict[str, Any]]:
    return [item for item in items if item["lean"] in labels]


def build_ai_lean_block(
    validated: dict[str, Any] | None,
    *,
    candidates: list[dict[str, Any]],
    status: str,
    reason: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the reportable lean block; never touches any aggregated News field."""
    items = list((validated or {}).get("items") or [])
    blocks = {
        "bullish": _bucket(items, ("bullish",)),
        "bearish": _bucket(items, ("bearish",)),
        "mixed": _bucket(items, ("mixed",)),
        "neutral_or_unclear": _bucket(items, ("neutral", "unclear")),
    }
    direct = [item for item in items if item.get("target_relevance") == "direct"]
    direct_counts = {label: sum(item["lean"] == label for item in direct) for label in LEAN_LABELS}
    major = [item for item in direct if item.get("importance") == "major"
             and item["lean"] in {"bullish", "bearish"} and item.get("url")]
    limitations = [
        "Lean is a headline-level AI judgement by the host model, not a calibrated sentiment model and not an event fact.",
        "Several provider feeds supply headlines without body text; a lean cannot verify the underlying report.",
        "Headline judgements and importance are qualitative; repeated coverage may describe the same underlying event.",
    ]
    if any(item.get("ticker_relevance") == "not_explicitly_matched" for item in items):
        limitations.append("Some judged records did not carry explicit ticker evidence in the headline; relevance rests on the provider query symbol and stays unverified.")
    block = {
        "status": status,
        "basis": AI_LEAN_BASIS,
        "layer": AI_LEAN_LAYER,
        "reason": reason,
        "model": (validated or {}).get("model"),
        "lean_id": (validated or {}).get("lean_id"),
        "display_only": True,
        "probability_effect": "none: lean never enters aggregation, confidence, cross-validation or Quant P(up)",
        "coverage": {
            "candidate_count": len(candidates),
            "assessed_count": len(items),
            "unassessed_count": max(0, len(candidates) - len(items)),
            "label_counts": {label: len(bucket) for label, bucket in blocks.items()},
            "direct_count": len(direct),
            "direct_label_counts": direct_counts,
        },
        "items": [dict(item) for item in items],
        "major_direct_items": [dict(item) for item in major],
        "bullish": [dict(item) for item in blocks["bullish"][:10]],
        "bearish": [dict(item) for item in blocks["bearish"][:10]],
        "mixed": [dict(item) for item in blocks["mixed"][:10]],
        "neutral_or_unclear": [dict(item) for item in blocks["neutral_or_unclear"][:10]],
        "limitations": limitations,
    }
    if warnings:
        block["warnings"] = list(warnings)
    return block


def lean_candidates(
    events: list[dict[str, Any]],
    opinions: list[dict[str, Any]],
    unclassified: list[dict[str, Any]] | None = None,
    *,
    limit: int = _MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Every canonical headline, including unclassified and pre-gate records.

    The eligibility gate is a bias gate, not a relevance filter for reading a
    headline: a record excluded for weak ticker evidence is still a record a
    human reviewer would want judged, as long as the exclusion is disclosed.
    """
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.append({  # noqa: PERF401 - two record kinds share one list; a comprehension would duplicate this 12-key literal
            "news_id": event.get("event_id"),
            "record_kind": "event",
            "headline": event.get("headline"),
            "summary": event.get("summary"),
            "source": (event.get("sources") or [None])[0],
            "published_at": event.get("first_published_at"),
            "url": event.get("url"),
            "direction": event.get("direction"),
            "event_type": event.get("event_type"),
            "ticker_relevance": event.get("ticker_relevance"),
            "eligible_evidence": event.get("eligible_evidence"),
            "exclusion_reasons": event.get("exclusion_reasons"),
        })
    for opinion in opinions:
        rows.append({  # noqa: PERF401 - same shared list as the event rows above
            "news_id": opinion.get("opinion_id"),
            "record_kind": "opinion",
            "headline": opinion.get("headline"),
            "summary": opinion.get("summary"),
            "source": (opinion.get("sources") or [None])[0],
            "published_at": opinion.get("published_at"),
            "url": opinion.get("url"),
            "direction": opinion.get("sentiment"),
            "event_type": None,
            "ticker_relevance": None,
            "eligible_evidence": opinion.get("eligible_evidence"),
            "exclusion_reasons": opinion.get("exclusion_reasons"),
        })
    for record in unclassified or []:
        rows.append({
            "news_id": record.get("news_id"),
            "record_kind": "unclassified",
            "headline": record.get("headline"),
            "summary": record.get("summary"),
            "source": (record.get("sources") or [None])[0],
            "published_at": record.get("published_at"),
            "url": record.get("url"),
            "direction": "unknown",
            "event_type": None,
            "ticker_relevance": record.get("ticker_relevance"),
            "eligible_evidence": False,
            "exclusion_reasons": [record.get("reason")],
        })
    rows = [row for row in rows if isinstance(row.get("news_id"), str) and row["news_id"]]
    rows.sort(key=lambda row: (row.get("published_at") or row.get("first_published_at") or ""), reverse=True)
    return rows[:limit]


def attach_lean_to_records(result: dict[str, Any], block: dict[str, Any]) -> None:
    """Copy lean fields onto matching cards; leave every aggregate field untouched."""
    by_id = {str(item.get("news_id")): item for item in block.get("items") or []}
    if not by_id:
        return
    for key in ("major_events", "opinion_records", "background_records"):
        for record in result.get(key) or []:
            identifier = record.get("event_id") or record.get("opinion_id")
            item = by_id.get(str(identifier))
            if item:
                record["ai_lean"] = item.get("lean")
                record["ai_lean_reason"] = item.get("reason")
                record["ai_lean_evidence_grade"] = item.get("evidence_grade")
