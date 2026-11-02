"""Coverage is interval arithmetic on validated query receipts, never article span."""

from ..common.time import iso, parse_time
from ..sources.base import interval


def merge(ranges):
    output = []
    for left, right in sorted(ranges):
        if left >= right:
            continue
        if output and left <= output[-1][1]:
            output[-1] = (output[-1][0], max(output[-1][1], right))
        else:
            output.append((left, right))
    return output


def gaps(start, end, successful):
    cursor = start
    output = []
    for segment_start, segment_end in merge(successful):
        left, right = max(segment_start, start), min(segment_end, end)
        if right <= start or left >= end:
            continue
        if left > cursor:
            output.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        output.append((cursor, end))
    return output


def pairs(values):
    return [(parse_time(value["start"]), parse_time(value["end"])) for value in values]


def validate_receipt(value: dict, *, source_id: str, entity: str, domain: str, start, end, previous=False) -> dict:
    if not isinstance(value, dict):
        raise TypeError("coverage_must_be_object")
    required = {
        "receipt_id", "source_id", "entity", "domain", "requested_start", "requested_end",
        "successful_intervals", "failed_intervals", "status", "cursor", "exhausted", "cap_reached",
        "fetched_count", "deduplicated_count", "last_success_at", "reason",
    }
    if not required.issubset(value):
        raise ValueError("coverage_required_fields_missing")
    if not isinstance(value["receipt_id"], str) or not value["receipt_id"] or len(value["receipt_id"]) > 256:
        raise ValueError("coverage_invalid_receipt_id")
    if (value["source_id"], value["entity"], value["domain"]) != (source_id, entity, domain):
        raise ValueError("coverage_source_entity_domain_mismatch")
    left, right = parse_time(value["requested_start"]), parse_time(value["requested_end"])
    if left >= right or right > end or (not previous and left < start):
        raise ValueError("coverage_request_outside_window")
    if type(value["exhausted"]) is not bool or type(value["cap_reached"]) is not bool:
        raise ValueError("coverage_pagination_flags_must_be_boolean")
    for key in ("fetched_count", "deduplicated_count"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("coverage_invalid_counts")
    if value["deduplicated_count"] > value["fetched_count"]:
        raise ValueError("coverage_deduplicated_exceeds_fetched")
    if value["exhausted"] and value["cursor"] not in (None, ""):
        raise ValueError("coverage_exhausted_with_cursor")
    if value["status"] not in {"success", "partial", "failed", "unavailable"}:
        raise ValueError("coverage_invalid_status")
    valid = {**value, "successful_intervals": [], "failed_intervals": []}
    for key in ("successful_intervals", "failed_intervals"):
        if not isinstance(value[key], list):
            raise TypeError("coverage_intervals_must_be_list")
        for segment in value[key]:
            if not isinstance(segment, dict):
                raise TypeError("coverage_invalid_interval")
            segment_start, segment_end = parse_time(segment.get("start")), parse_time(segment.get("end"))
            if not left <= segment_start < segment_end <= right:
                raise ValueError("coverage_interval_outside_request")
            if key == "failed_intervals" and (not isinstance(segment.get("reason"), str) or not segment["reason"]):
                raise ValueError("coverage_failed_interval_reason_missing")
            valid[key].append(interval(segment_start, segment_end, segment.get("reason") if key == "failed_intervals" else None))
    successes, failures = pairs(valid["successful_intervals"]), pairs(valid["failed_intervals"])
    if any(max(a, c) < min(b, d) for a, b in successes for c, d in failures):
        raise ValueError("coverage_success_failure_overlap")
    complete = not gaps(left, right, successes)
    if value["status"] == "success" and (not complete or failures or not value["exhausted"] or value["cap_reached"]):
        raise ValueError("coverage_false_success")
    if value["status"] != "success" and complete:
        raise ValueError("coverage_inconsistent_complete_status")
    if value["status"] in {"failed", "unavailable"} and successes:
        raise ValueError("coverage_failed_with_successful_intervals")
    if value["status"] != "success" and (not isinstance(value["reason"], str) or not value["reason"]):
        raise ValueError("coverage_failure_reason_missing")
    if successes or value["last_success_at"] is not None:
        parse_time(value["last_success_at"])
    return valid


def finalize(receipt, successful, failed, *, fetched_count, deduplicated_count, exhausted, cap_reached, cursor=None,
             queried=None, unavailable=False):
    left, right = parse_time(receipt["requested_start"]), parse_time(receipt["requested_end"])
    successful = merge([(max(a, left), min(b, right)) for a, b in successful])
    missing = gaps(left, right, successful)
    failures = []
    for start, end in missing:
        reasons = sorted({value["reason"] for value in failed
                          if max(start, parse_time(value["start"])) < min(end, parse_time(value["end"]))})
        failures.append(interval(start, end, "; ".join(reasons) or "range_not_verified"))
    complete = not missing and exhausted and not cap_reached and cursor in (None, "")
    if not missing and not complete:
        # A cap/cursor contradicts an unqualified completeness claim.
        successful = []
        failures = [interval(left, right, "pagination_not_exhausted")]
    status = "success" if complete else ("partial" if successful or fetched_count or queried else ("unavailable" if unavailable else "failed"))
    receipt.update(
        successful_intervals=[interval(a, b) for a, b in successful], failed_intervals=failures,
        status=status, exhausted=bool(complete), cap_reached=bool(cap_reached), cursor=cursor,
        fetched_count=fetched_count, deduplicated_count=deduplicated_count,
        reason=None if complete else "; ".join(sorted({value["reason"] for value in failures})),
    )
    if successful and receipt["last_success_at"] is None:
        receipt["last_success_at"] = receipt["collected_at"]
    if queried:
        receipt["queried_intervals"] = [interval(a, b) for a, b in merge(pairs(queried))]
    # Normalized explicit UTC strings are also useful to storage query keys.
    receipt["requested_start"], receipt["requested_end"] = iso(left), iso(right)
    return receipt
