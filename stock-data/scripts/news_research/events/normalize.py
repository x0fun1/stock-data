"""Normalize articles, preserve versions and group events without sentiment scores."""

from __future__ import annotations

import re
from datetime import timedelta

from ..common.jsonio import digest
from ..common.security import safe_url, sanitize_data
from ..common.time import bounds, iso, now, parse_time
from ..contracts import window

TIME_FIELDS = ("published_at", "updated_at", "occurred_at", "effective_at", "scheduled_at", "original_published_at")
SCOPES = {"full_text", "official_excerpt", "abstract", "headline_only"}
KINDS = {"official", "company", "independent_media", "opinion", "rumor", "unknown"}


def normalize_records(raw_records: list[dict]) -> tuple[list[dict], list[dict]]:
    records = {}
    rejected = []
    for index, source_record in enumerate(raw_records):
        try:
            if not isinstance(source_record, dict):
                raise TypeError("record must be an object")
            raw = sanitize_data(source_record)
            source = raw.get("source_id")
            source_item = raw.get("source_item_id") or raw.get("url")
            if not isinstance(source, str) or not source or not source_item:
                raise ValueError("source_id and source_item_id or URL required")
            item = {key: raw.get(key) for key in TIME_FIELDS}
            item.update({"source_id": source, "source_item_id": str(source_item), "url": safe_url(raw.get("url")),
                         "title": str(raw.get("title") or ""), "text": str(raw.get("text") or ""),
                         "text_scope": raw.get("text_scope", "headline_only"),
                         "time_precision": raw.get("time_precision", "date" if len(str(raw.get("published_at", ""))) == 10 else "timestamp"), "timezone": raw.get("timezone"),
                         "language": raw.get("language", "unknown"), "origin_id": str(raw.get("origin_id") or ""),
                         "event_key": str(raw.get("event_key") or ""), "entities": raw.get("entities", []),
                         "category": raw.get("category", "company"), "status": raw.get("status", "active"),
                         "data_type": raw.get("data_type", "news"),
                         "substantive_update": raw.get("substantive_update") is True,
                         "ongoing": raw.get("ongoing") is True, "source_kind": raw.get("source_kind", "unknown"),
                         "evidence_locator": str(raw.get("evidence_locator") or "provided text"),
                         "receipt_id": raw.get("receipt_id"), "fetched_at": raw.get("fetched_at") or now()})
            if item["text_scope"] not in SCOPES or item["source_kind"] not in KINDS:
                raise ValueError("invalid text_scope or source_kind")
            if item["category"] not in {"company", "industry", "macro"} or item["status"] not in {"active", "corrected", "withdrawn", "denied"}:
                raise ValueError("invalid event category or status")
            if item["data_type"] not in {"news", "financial", "exposure"}:
                raise ValueError("invalid record data_type")
            if item["time_precision"] not in {"timestamp", "date"}:
                raise ValueError("invalid time precision")
            if not isinstance(item["entities"], list) or any(not isinstance(entity, str) for entity in item["entities"]):
                raise ValueError("entities must be security identifiers")
            item["entities"] = sorted({entity.upper() for entity in item["entities"]})
            for field in TIME_FIELDS:
                if item[field]:
                    bounds(item[field], precision="date" if len(item[field]) == 10 else "timestamp", timezone_name=item["timezone"])
            parse_time(item["fetched_at"])
            permissions = raw.get("permissions", {})
            keep = permissions.get("store_text") is True
            retention = permissions.get("retention_days", 0)
            if type(retention) is not int or retention < 0:
                raise ValueError("retention_days must be a nonnegative integer")
            item["permissions"] = {"store_text": keep and retention > 0, "retention_days": retention}
            item["content_hash"] = digest({"title": item["title"], "text": item["text"]})
            item["expires_at"] = iso(parse_time(item["fetched_at"]) + timedelta(days=retention)) if keep and retention > 0 else None
            if not keep or not retention or (item["expires_at"] and parse_time(item["expires_at"]) <= parse_time(now())):
                item["text"] = ""
                item["text_scope"] = "headline_only"
                item["local_status"] = "content_not_retained"
            elif not item["text"]:
                item["text_scope"] = "headline_only"
            else:
                item["local_status"] = "available"
            number_items = raw.get("confirmed_numbers", [])
            if not isinstance(number_items, list):
                raise TypeError("confirmed_numbers must be an array")
            item["confirmed_numbers"] = []
            for number in number_items:
                if not isinstance(number, dict) or not {"value", "unit", "subject"} <= set(number):
                    raise ValueError("confirmed number needs value, unit and subject")
                if str(number["value"]) not in item["text"] or str(number["unit"]) not in item["text"] or str(number["subject"]) not in item["text"]:
                    raise ValueError("confirmed number is not supported by the provided text")
                item["confirmed_numbers"].append({key: number[key] for key in ("value", "unit", "subject")})
            version = {key: value for key, value in item.items() if key not in {"fetched_at", "expires_at", "receipt_id", "local_status"}}
            item["record_id"] = "rec-" + digest(version)[:24]
            records[item["record_id"]] = item
        except (ValueError, TypeError, OverflowError, KeyError) as exc:  # noqa: PERF203 - one invalid record must not discard other sources
            rejected.append({"record_index": index, "reason": str(exc)})
    return list(records.values()), rejected


def _time(item: dict, field: str) -> tuple:
    return bounds(item[field], precision="date" if len(item[field]) == 10 else "timestamp", timezone_name=item["timezone"])


def build_events(records: list[dict], request: dict) -> dict:
    start, end = map(parse_time, window(request))
    accepted = []
    exclusions = []
    evidence = []
    for record in records:
        reason = None
        if not record.get("published_at"):
            reason = "publication time missing (fetch time is not publication time)"
        elif _time(record, "published_at")[1] > end:
            reason = "publication after as_of or date-only publication not yet fully knowable"
        elif record.get("updated_at") and _time(record, "updated_at")[1] > end:
            reason = "only a post-as_of revision is available"
        elif _time(record, "published_at")[0] <= start and record["data_type"] == "news" and not (record["ongoing"] and record.get("effective_at") and _time(record, "effective_at")[1] <= end):
            reason = "outside requested window; not evidenced ongoing background"
        if reason:
            exclusions.append({"record_id": record["record_id"], "reason": reason})
            continue
        copy = dict(record)
        # Scheduled future matters are watch items, never completed events.
        copy["future_event"] = bool(record.get("scheduled_at") and _time(record, "scheduled_at")[0] > end)
        eligible = bool(record["url"] and record["text"] and record["text_scope"] in {"official_excerpt", "full_text", "abstract"}
                        and record["source_kind"] in {"official", "company", "independent_media"})
        ev = {"evidence_id": "ev-" + record["record_id"][4:], "record_id": record["record_id"], "quote": record["text"],
              "locator": record["evidence_locator"], "content_hash": record["content_hash"], "eligible": eligible,
              "major_eligible": eligible and record["text_scope"] in {"official_excerpt", "full_text"},
              "confirmed_numbers": record["confirmed_numbers"], "expires_at": record["expires_at"]}
        copy["evidence_id"] = ev["evidence_id"]
        accepted.append(copy)
        evidence.append(ev)
    groups: dict[str, list] = {}
    for record in accepted:
        if record["data_type"] != "news":
            continue
        # A stable provider/host event key or original-story identity joins bilingual versions and rewrites.
        # Without one we only merge exact normalized titles or URLs, never guess semantic equivalence.
        key = record["event_key"] or record["origin_id"] or record["url"] or re.sub(r"\W+", "", record["title"]).casefold()
        groups.setdefault(key, []).append(record)
    events = []
    for key, group in groups.items():
        group.sort(key=lambda item: (_time(item, "updated_at" if item.get("updated_at") else "published_at")[1], item["record_id"]))
        first_record = min(group, key=lambda item: _time(item, "original_published_at" if item.get("original_published_at") else "published_at")[0])
        first_value = first_record.get("original_published_at") or first_record["published_at"]
        first = _time(first_record, "original_published_at" if first_record.get("original_published_at") else "published_at")[0]
        updates = []
        update_records = []
        prior_content = set()
        for item in group:
            if item["substantive_update"] and item["content_hash"] not in prior_content:
                updates.append(_time(item, "updated_at" if item.get("updated_at") else "published_at")[1])
                update_records.append(item)
            prior_content.add(item["content_hash"])
        state_changes = [item for item in group if item["status"] != "active" or item in update_records]
        latest = state_changes[-1] if state_changes else group[-1]
        freshness = max([first, *updates])
        statuses = {item["status"] for item in group}
        # Corrected text is usable only if supplied as a substantive, verifiable revision.
        correction_unresolved = latest["status"] == "corrected" and not latest["substantive_update"]
        invalidated = latest["status"] in {"withdrawn", "denied"} or correction_unresolved
        current_ids = [item["evidence_id"] for item in group if item["source_item_id"] == latest["source_item_id"]
                       and item["record_id"] == latest["record_id"]] if statuses - {"active"} else [item["evidence_id"] for item in group]
        events.append({"event_id": "evt-" + digest({"key": key})[:24], "version": digest([item["record_id"] for item in group]),
                       "title": latest["title"], "category": latest["category"],
                       "entities": sorted({entity for item in group for entity in item["entities"]}),
                       "first_reported_at": first_value,
                       "substantive_updated_at": (update_records[-1].get("updated_at") or update_records[-1]["published_at"]) if update_records else None,
                       "freshness_at": iso(freshness), "occurred_at": latest.get("occurred_at"),
                       "effective_at": latest.get("effective_at"), "scheduled_at": latest.get("scheduled_at"),
                       "future_event": latest["future_event"], "status": latest["status"], "invalidated": invalidated,
                       "record_ids": [item["record_id"] for item in group], "evidence_ids": current_ids,
                       "all_evidence_ids": [item["evidence_id"] for item in group],
                       "source_lineage": sorted({item["origin_id"] or item["url"] or item["source_item_id"] for item in group}),
                       "conflicts": sorted(statuses) if len(statuses) > 1 else [],
                       "background": freshness <= start or (end - freshness).days >= 30,
                       "deduplication_basis": "event_key" if latest["event_key"] else "origin_id" if latest["origin_id"] else "exact_url_or_title"})
    by_evidence = {item["evidence_id"]: item for item in evidence}
    for event in events:
        for identity in event["all_evidence_ids"]:
            if event["invalidated"] or identity not in event["evidence_ids"]:
                by_evidence[identity]["eligible"] = False
                by_evidence[identity]["major_eligible"] = False
                by_evidence[identity]["exclusion_reason"] = "invalidated event or superseded evidence version"
    events.sort(key=lambda event: (event["freshness_at"], event["event_id"]), reverse=True)
    return {"events": events, "evidence": evidence, "excluded_records": exclusions}
