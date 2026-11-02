"""Source receipt coverage, independent of article density."""

from __future__ import annotations

from .common.time import iso, parse_time
from .contracts import window


def merge_intervals(intervals: list[dict], start: str, end: str) -> list[dict]:
    left, right = parse_time(start), parse_time(end)
    ordered = sorted((max(left, parse_time(item["start"])), min(right, parse_time(item["end"]))) for item in intervals)
    merged = []
    for begin, finish in ordered:
        if begin >= finish:
            continue
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return [{"start": iso(begin), "end": iso(finish)} for begin, finish in merged]


def summarize_coverage(request: dict, receipts: list[dict]) -> dict:
    start, end = window(request)
    sources = []
    configured_ids = list(dict.fromkeys(item["source_id"] for item in request["sources"]))
    scoped = []
    for source_id in configured_ids:
        groups = {}
        for item in receipts:
            if item["source_id"] == source_id:
                key = (item.get("entity"), item.get("domain"), item.get("query_key"))
                groups.setdefault(key, []).append(item)
        scoped.extend((source_id, key, values) for key, values in groups.items())
        expected = len([item for item in request["sources"] if item["source_id"] == source_id])
        scoped.extend((source_id, (None, None, None), []) for _ in range(max(0, expected - len(groups))))
    for source_id, (entity, domain, query_key), matching in scoped:
        intervals = [interval for item in matching for interval in item.get("successful_intervals", [])]
        merged = merge_intervals(intervals, start, end)
        queried = merge_intervals([interval for item in matching for interval in item.get("queried_intervals", [])], start, end)
        complete_range = merged == [{"start": start, "end": end}]
        terminal = bool(matching) and all(item.get("status") == "success" and item.get("exhausted") is True
                                          and not item.get("cap_reached") and not item.get("failed_intervals") for item in matching)
        state = "complete_for_configured_sources" if complete_range and terminal else "partial" if merged else "unknown"
        if not matching or all(item.get("status") == "unavailable" for item in matching):
            collection = "unavailable"
        elif all(item.get("status") == "failed" for item in matching):
            collection = "failed"
        elif terminal:
            collection = "success"
        else:
            collection = "partial"
        covered_days = sum((parse_time(item["end"]) - parse_time(item["start"])).total_seconds() / 86400 for item in merged)
        queried_days = sum((parse_time(item["end"]) - parse_time(item["start"])).total_seconds() / 86400 for item in queried)
        sources.append({"source_id": source_id, "entity": entity, "domain": domain, "query_key": query_key,
                        "domains": sorted({item.get("domain", "company") for item in matching}),
                        "collection_status": collection, "window_status": state, "successful_intervals": merged,
                        "queried_intervals": queried,
                        "covered_days": round(covered_days, 4), "queried_days": round(queried_days, 4),
                        "below_minimum_target": covered_days < 30,
                        "fetched_count": sum(item.get("fetched_count", 0) for item in matching),
                        "reasons": [item.get("reason") for item in matching if item.get("reason")]})
    states = {item["window_status"] for item in sources}
    status = "complete_for_configured_sources" if states == {"complete_for_configured_sources"} else "unknown" if states == {"unknown"} else "partial"
    return {"requested_start": start, "requested_end": end, "lookback_days": request["lookback_days"],
            "window_status": status, "sources": sources,
            "scope_note": "仅声明配置来源和请求范围，不代表全网完整覆盖；文章首末日期不证明检索完整。",
            "missing_domains": sorted({"company", "macro", "financial"} - {domain for item in sources for domain in item["domains"]
                                                                                  if item["collection_status"] == "success"}),
            "below_minimum_target": any(item["below_minimum_target"] for item in sources)}
