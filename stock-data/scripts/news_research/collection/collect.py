"""Coordinate bounded acquisition, authorized host input, and incremental coverage."""

import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..common.security import redact_text, safe_url, sanitize_data
from ..common.time import iso, parse_time
from ..sources import companyfacts, fed, finnhub, fred, sec
from ..sources.base import identifier, in_window, instant, interval, record
from ..sources.registry import source_registry
from ..sources.transport import Client, SourceError
from .coverage import finalize, gaps, pairs, validate_receipt

_ADAPTERS = {"sec_submissions": sec.collect, "sec_companyfacts": companyfacts.collect, "finnhub": finnhub.collect, "fed": fed.collect, "fred": fred.collect}
_OFFICIAL_DOMAINS = {
    "sec_submissions": {"www.sec.gov", "data.sec.gov"},
    "sec_companyfacts": {"www.sec.gov", "data.sec.gov"},
    "fed": {"www.federalreserve.gov"},
    "fred": {"fred.stlouisfed.org", "api.stlouisfed.org", "alfred.stlouisfed.org"},
    "hkex": {"www1.hkexnews.hk", "www.hkexnews.hk", "www.hkex.com.hk"},
    "china_macro": {"www.pbc.gov.cn", "www.stats.gov.cn"},
    "hkma": {"www.hkma.gov.hk"},
    "eia": {"www.eia.gov", "api.eia.gov"},
}
_TIME_FIELDS = ("published_at", "updated_at", "occurred_at", "effective_at", "scheduled_at", "fetched_at", "original_published_at")


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _request(request):
    if not isinstance(request, dict) or not isinstance(request.get("symbol"), str) or not request["symbol"].strip():
        raise ValueError("request_symbol_required")
    if request.get("market") not in {"US", "HK"} or request.get("asset_type", "stock") not in {"stock", "etf"}:
        raise ValueError("request_market_or_asset_type_invalid")
    end = parse_time(request.get("as_of"))
    days = _integer(request.get("lookback_days", 60), 30, 60, "lookback_days")
    recent = _integer(request.get("recent_days", 7), 1, 30, "recent_days")
    budget = request.get("budget", {})
    if not isinstance(budget, dict):
        raise TypeError("request_budget_must_be_object")
    maximum = _integer(budget.get("max_requests", 120), 0, 120, "max_requests")
    _integer(budget.get("batch_size", 100), 1, 1000, "batch_size")
    _integer(budget.get("max_analysis_events", 1000), 0, 100000, "max_analysis_events")
    sources = request.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 100 or any(not isinstance(item, dict) or not isinstance(item.get("source_id"), str) for item in sources):
        raise ValueError("request_sources_invalid")
    if request.get("collection_mode", "backfill") not in {"backfill", "incremental"}:
        raise ValueError("collection_mode_invalid")
    return end - timedelta(days=days), end, recent, maximum


def _scope(item, request, definition):
    source_id = item["source_id"]
    domain = definition["domain"] if definition else item.get("domain", "company")
    if domain not in {"company", "macro", "financial"}:
        raise ValueError("source_domain_invalid")
    symbol = item.get("symbol", request["symbol"])
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("source_symbol_invalid")
    topics = item.get("topics", ["monetary"] if source_id == "fed" else [])
    if not isinstance(topics, (list, str)) or (isinstance(topics, list) and any(not isinstance(topic, str) for topic in topics)):
        raise ValueError("source_topics_invalid")
    entity = (item.get("series_id") or (",".join(sorted(topics)) if isinstance(topics, list) else topics) or "all") if domain == "macro" else symbol
    if not isinstance(entity, str):
        raise TypeError("source_entity_invalid")
    query_key = identifier(source_id, domain, entity, symbol if domain != "macro" else None, item.get("cik"), topics,
                           request["market"], item.get("permissions"))
    return {**item, "symbol": symbol}, domain, entity, query_key


def _normalize(raw, source_id, start, end):
    if not isinstance(raw, dict) or raw.get("source_id") != source_id:
        raise ValueError("record_source_mismatch")
    item_id = raw.get("source_item_id")
    if not isinstance(item_id, (str, int)) or isinstance(item_id, bool) or not str(item_id):
        raise ValueError("record_source_item_id_required")
    permission = raw.get("permissions")
    if not isinstance(permission, dict) or type(permission.get("store_text")) is not bool:
        raise ValueError("record_explicit_permissions_required")
    retention = _integer(permission.get("retention_days"), 0, 3650, "record_retention_days")
    if permission["store_text"] and not retention:
        raise ValueError("record_stored_text_requires_retention")
    title, text = raw.get("title", ""), raw.get("text", "")
    if not isinstance(title, str) or not isinstance(text, str) or len(title) > 4096 or len(text) > 4 * 1024 * 1024:
        raise ValueError("record_text_invalid_or_over_limit")
    url = safe_url(raw.get("url"))
    if not url:
        raise ValueError("record_citation_url_required")
    data_type = raw.get("data_type", "news")
    if data_type not in {"news", "financial", "exposure"}:
        raise ValueError("record_data_type_invalid")
    precision = raw.get("time_precision", "date" if len(str(raw.get("published_at", ""))) == 10 else "timestamp")
    if precision not in {"date", "timestamp"}:
        raise ValueError("record_time_precision_invalid")
    for key in _TIME_FIELDS:
        value = raw.get(key)
        if value is not None:
            instant(value)
            if key == "fetched_at":
                parse_time(value)
    publication = raw.get("published_at")
    if publication and instant(publication).date() > end.date():
        return None
    if publication and len(publication) != 10 and instant(publication) > end:
        return None
    if publication and data_type == "news" and raw.get("ongoing") is not True and not in_window(publication, start, end):
        return None
    entities = raw.get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(entity, str) or not entity for entity in entities):
        raise ValueError("record_entities_invalid")
    category, status = raw.get("category", "company"), raw.get("status", "active")
    if category not in {"company", "industry", "macro"} or status not in {"active", "corrected", "withdrawn", "denied"}:
        raise ValueError("record_category_or_status_invalid")
    if type(raw.get("substantive_update", False)) is not bool or type(raw.get("ongoing", False)) is not bool:
        raise ValueError("record_update_flags_invalid")
    kind = raw.get("source_kind", "unknown")
    if source_id in _OFFICIAL_DOMAINS:
        if urlsplit(url).hostname not in _OFFICIAL_DOMAINS[source_id]:
            raise ValueError("record_official_source_domain_mismatch")
        kind = "official"
    if kind not in {"official", "company", "independent_media", "opinion", "rumor", "unknown"}:
        raise ValueError("record_source_kind_invalid")
    if source_id == "host_search":
        # A search hit is discovery, not the original publisher's confirmation.
        kind = "unknown"
    scope = raw.get("text_scope", "headline_only")
    if scope not in {"full_text", "official_excerpt", "abstract", "headline_only"}:
        raise ValueError("record_text_scope_invalid")
    row = record(source_id, str(item_id), title=title, url=url, published_at=publication)
    for key in (*_TIME_FIELDS, "language", "origin_id", "event_key", "timezone", "evidence_locator"):
        if key in raw:
            if raw[key] is not None and not isinstance(raw[key], str):
                raise ValueError("record_metadata_must_be_text")
            row[key] = raw[key]
    if precision == "date" and "timezone" not in raw:
        row["timezone"] = None
    row.update(text=text if permission["store_text"] else "", text_scope=scope if permission["store_text"] and text else "headline_only",
               permissions={"store_text": permission["store_text"], "retention_days": retention}, time_precision=precision,
               entities=entities, category=category, status=status, substantive_update=raw.get("substantive_update", False),
               data_type=data_type, ongoing=raw.get("ongoing", False), source_kind=kind)
    numbers = raw.get("confirmed_numbers", [])
    if not isinstance(numbers, list):
        raise TypeError("record_confirmed_numbers_invalid")
    cleaned_numbers = []
    for number in numbers:
        if not isinstance(number, dict) or not isinstance(number.get("unit"), str) or not isinstance(number.get("subject"), str):
            raise TypeError("record_confirmed_number_fields_invalid")
        value = number.get("value")
        if type(value) not in {int, float, str} or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("record_confirmed_number_value_invalid")
        cleaned_numbers.append({"value": value, "unit": number["unit"], "subject": number["subject"]})
    if permission["store_text"]:
        row["confirmed_numbers"] = cleaned_numbers
    updated = row["updated_at"]
    if updated and (instant(updated).date() > end.date() or (len(updated) != 10 and instant(updated) > end)):
        row.update(text="", text_scope="headline_only", confirmed_numbers=[])
    return row


def _clean(value, secrets):
    if isinstance(value, dict):
        return {key: _clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    return redact_text(value, secrets=secrets) if isinstance(value, str) else value


def _deduplicate(rows):
    output, seen = [], set()
    for row in rows:
        # Same-source exact versions only. Corrections and substantive updates survive.
        key = identifier({key: value for key, value in row.items() if key not in {"fetched_at", "receipt_id", "record_id"}})
        if key not in seen:
            output.append(row)
            seen.add(key)
    return output


def collect_sources(request: dict, imports: dict | None = None, *, previous_coverage: list | None = None) -> dict:
    """Collect configured sources; analysis batch/event budgets never truncate acquisition."""
    start, end, recent, maximum = _request(request)
    imports = {} if imports is None else imports
    if not isinstance(imports, dict) or any(not isinstance(imports.get(key, []), list) for key in ("records", "coverage")):
        raise ValueError("imports_records_and_coverage_must_be_lists")
    if previous_coverage is not None and not isinstance(previous_coverage, list):
        raise ValueError("previous_coverage_must_be_list")
    client = Client(maximum)
    registry = {entry["source_id"]: entry for entry in source_registry()}
    configured_ids = {item["source_id"] for item in request.get("sources", [])}
    unknown = sorted(configured_ids - set(registry))
    if unknown:
        raise ValueError(f"unregistered source_id(s): {', '.join(unknown)}")
    output = {"records": [], "coverage": [], "source_capabilities": [], "limitations": [], "financial_facts": []}
    limitations = output["limitations"]
    for raw in imports.get("records", []) + imports.get("coverage", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("source_id"), str) or raw.get("source_id") not in configured_ids:
            limitations.append("Unconfigured or malformed host input was ignored; host records do not silently add sources.")
    seen_scopes = set()
    for config in request.get("sources", []):
        source_id = config["source_id"]
        definition = registry.get(source_id)
        item, domain, entity, query_key = _scope(config, request, definition)
        if query_key in seen_scopes:
            continue
        seen_scopes.add(query_key)
        receipt_id = "receipt-" + identifier(source_id, entity, domain, query_key, iso(start), iso(end))
        receipt = {"receipt_id": receipt_id, "source_id": source_id, "entity": entity, "domain": domain,
                   "requested_start": iso(start), "requested_end": iso(end), "query_key": query_key,
                   "last_success_at": None, "collected_at": iso(datetime.now(timezone.utc)), "collection_mode": request.get("collection_mode", "backfill")}
        successful, failed, rows, queried = [], [], [], []
        fetched_count, cap_reached, unavailable, cursor = 0, False, False, None
        exhausted = True
        before = client.requests
        imported_receipts = [raw for raw in imports.get("coverage", []) if isinstance(raw, dict) and raw.get("source_id") == source_id
                             and raw.get("entity") == entity and raw.get("query_key") in (None, query_key)]
        imported_ids = {raw.get("receipt_id") for raw in imported_receipts if isinstance(raw.get("receipt_id"), str)}
        unique_source_scope = sum(value["source_id"] == source_id for value in request.get("sources", [])) == 1
        unattributed = [raw for raw in imports.get("records", [])
                        if isinstance(raw, dict) and raw.get("source_id") == source_id
                        and not (isinstance(raw.get("receipt_id"), str) and raw["receipt_id"] in imported_ids)]
        if unattributed and not unique_source_scope:
            limitations.append(f"{source_id}: {len(unattributed)} host record(s) lacked a scope-matching receipt and were withheld.")
        host_rows = [raw for raw in imports.get("records", []) if isinstance(raw, dict) and raw.get("source_id") == source_id
                     and ((isinstance(raw.get("receipt_id"), str) and raw["receipt_id"] in imported_ids) or unique_source_scope)
                     and (domain == "macro" or not isinstance(raw.get("entities"), list)
                          or item["symbol"] in raw["entities"] or not raw["entities"])]
        valid_receipts, conflicted_ids = {}, set()
        for raw in imported_receipts:
            try:
                valid = validate_receipt(raw, source_id=source_id, entity=entity, domain=domain, start=start, end=end)
                if raw.get("query_key") not in (None, query_key) or (not unique_source_scope and raw.get("query_key") != query_key):
                    raise ValueError("coverage_query_key_mismatch_or_ambiguous_scope")
                if valid["receipt_id"] in conflicted_ids:
                    raise ValueError("coverage_duplicate_receipt_conflict")
                if valid["receipt_id"] in valid_receipts:
                    if valid != valid_receipts[valid["receipt_id"]]:
                        valid_receipts.pop(valid["receipt_id"])
                        conflicted_ids.add(valid["receipt_id"])
                        failed.append(interval(start, end, "host_conflicting_receipt_id"))
                        raise ValueError("coverage_duplicate_receipt_conflict")
                    continue
                valid_receipts[valid["receipt_id"]] = valid
            except (ValueError, TypeError) as exc:
                limitations.append(f"{source_id}: host receipt rejected ({redact_text(str(exc))}).")
        for raw in host_rows:
            try:
                row = _normalize(raw, source_id, start, end)
                if row:
                    row["receipt_id"] = receipt_id
                    if not isinstance(raw.get("receipt_id"), str) or raw["receipt_id"] not in valid_receipts:
                        limitations.append(f"{source_id}: host record lacks a matching valid query receipt; its article dates do not establish coverage.")
                        failed.append(interval(start, end, "host_record_query_receipt_missing"))
                    rows.append(row)
            except (ValueError, TypeError) as exc:  # noqa: PERF203 -- isolate each invalid host record
                limitations.append(f"{source_id}: host record rejected ({redact_text(str(exc))}).")
                failed.append(interval(start, end, "host_record_validation_failed"))
        for host_id, valid in valid_receipts.items():
            matched = sum(raw.get("receipt_id") == host_id for raw in host_rows)
            if matched > valid["fetched_count"]:
                limitations.append(f"{source_id}: host receipt count smaller than linked records; coverage claim rejected.")
                failed.append(interval(start, end, "host_receipt_count_mismatch"))
                continue
            successful.extend(pairs(valid["successful_intervals"]))
            if valid["successful_intervals"]:
                previous_success = receipt["last_success_at"]
                receipt["last_success_at"] = max(previous_success or valid["last_success_at"], valid["last_success_at"], key=parse_time)
            failed.extend(valid["failed_intervals"])
            fetched_count += valid["fetched_count"]
            cap_reached |= valid["cap_reached"]
            exhausted &= valid["exhausted"]
            cursor = valid["cursor"] or cursor
            unavailable |= valid["status"] == "unavailable"
        if host_rows or imported_receipts:
            fetched_count = max(fetched_count, len(host_rows))
            # Invalid host evidence cannot leave an otherwise successful ingestion receipt.
            if any(value["reason"].startswith("host_") for value in failed):
                successful = []
                exhausted = False
            if not valid_receipts:
                exhausted = False
                failed.append(interval(start, end, "host_query_receipt_missing_or_invalid"))
        else:
            if request.get("collection_mode") == "incremental":
                for previous in previous_coverage or []:
                    if not isinstance(previous, dict) or previous.get("query_key") != query_key:
                        continue
                    try:
                        valid = validate_receipt(previous, source_id=source_id, entity=entity, domain=domain, start=start, end=end, previous=True)
                        refresh_start = end - timedelta(days=recent)
                        successful.extend((max(a, start), min(b, refresh_start)) for a, b in pairs(valid["successful_intervals"]) if max(a, start) < min(b, refresh_start))
                    except (ValueError, TypeError):
                        limitations.append(f"{source_id}: invalid previous coverage ignored; interval will be recollected.")
                if successful:
                    limitations.append(f"{source_id}: incremental mode reuses verified historical queries and refreshes the latest {recent} days; older in-place revisions may require a backfill.")
            remaining = gaps(start, end, successful)
            for left, right in remaining:
                try:
                    if source_id not in _ADAPTERS:
                        raise SourceError("authorized_host_import_required", unavailable=True)
                    acquired = _ADAPTERS[source_id](client, item, left, right)
                    for raw in acquired["records"]:
                        normalized = _normalize(raw, source_id, start, end)
                        if normalized:
                            normalized["receipt_id"] = receipt_id
                            rows.append(normalized)
                    output["financial_facts"].extend(acquired.get("financial_facts", []))
                    successful.extend(pairs(acquired["successful_intervals"]))
                    failed.extend(acquired["failed_intervals"])
                    queried.extend(acquired.get("queried_intervals", []))
                    fetched_count += acquired["fetched_count"]
                    cap_reached |= acquired["cap_reached"]
                    exhausted &= acquired["exhausted"]
                    cursor = acquired["cursor"] or cursor
                    limitations.extend(f"{source_id}: {note}" for note in acquired["limitations"])
                except (SourceError, ValueError, TypeError, KeyError) as exc:  # noqa: PERF203 -- later source failures must preserve earlier results
                    reason = exc.reason if isinstance(exc, SourceError) else "invalid_source_response_or_configuration"
                    failed.append(interval(left, right, reason))
                    cap_reached |= getattr(exc, "cap_reached", False)
                    unavailable |= getattr(exc, "unavailable", False)
                    exhausted = False
        if source_id == "host_search" and successful:
            successful = []
            exhausted = False
            failed.append(interval(start, end, "discovery_search_is_not_exhaustive_news_coverage"))
        rows = _deduplicate(rows)
        receipt = finalize(receipt, successful, failed, fetched_count=max(fetched_count, len(rows)), deduplicated_count=len(rows),
                           exhausted=exhausted, cap_reached=cap_reached, cursor=cursor, queried=queried, unavailable=unavailable)
        receipt["requests_used"] = client.requests - before
        receipt["host_receipt_ids"] = sorted(valid_receipts)
        output["records"].extend(rows)
        output["coverage"].append(receipt)
        if receipt["status"] != "success":
            limitations.append(f"{source_id}/{entity}: {receipt['status']} — {receipt['reason']}; requested window is unchanged.")
        if definition is None:
            definition = {"source_id": source_id, "domain": domain, "documented": {"adapter_status": "unregistered_host_input_only"},
                          "account_test": {"status": "not_tested", "tested_at": None}, "current_request": {"status": "not_requested", "receipt_ids": []}}
        current = definition["current_request"]
        current.setdefault("receipts", []).append({"receipt_id": receipt_id, "status": receipt["status"], "requests_used": receipt["requests_used"], "entity": entity})
        current["receipt_ids"].append(receipt_id)
        current["status"] = receipt["status"] if len(current["receipt_ids"]) == 1 else "multiple_scopes"
        if source_id not in {entry["source_id"] for entry in output["source_capabilities"]}:
            output["source_capabilities"].append(definition)
    if not configured_ids:
        limitations.append("No sources configured; no window coverage can be established.")
    output["financial_facts"] = list({identifier(fact): fact for fact in output["financial_facts"]}.values())
    output["limitations"] = list(dict.fromkeys(limitations))
    secrets = tuple(os.environ.get(name, "") for name in ("SEC_USER_AGENT", "SEC_CONTACT", "FINNHUB_API_KEY", "FRED_API_KEY"))
    return _clean(sanitize_data(output), secrets)
