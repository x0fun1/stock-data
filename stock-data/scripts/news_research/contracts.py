"""Versioned, deliberately narrow contracts for evidence and host assessments."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from . import POLICY_VERSION, PROMPT_VERSION, SCHEMA_VERSION
from .common.security import sanitize_data
from .common.time import iso, parse_time

DIRECTIONS = ["bullish", "bearish", "neutral", None]
EVIDENCE_STATES = ["sufficient", "limited", "insufficient", "not_assessed"]


def object_keys(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    if set(value) - allowed:
        raise ValueError(f"{label}: unknown fields {sorted(set(value) - allowed)}")
    if required - set(value):
        raise ValueError(f"{label}: missing fields {sorted(required - set(value))}")
    return value


def text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 20000 or (not empty and not value.strip()):
        raise ValueError(f"{label} must be nonempty text of at most 20000 characters")
    return value


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a list of strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate IDs")
    return value


def validate_request(raw: Any) -> dict:
    allowed = {"schema_version", "symbol", "market", "currency", "asset_type", "as_of", "lookback_days", "recent_days",
               "sources", "budget", "policy_version", "prompt_version", "model_id", "collection_mode"}
    value = dict(object_keys(raw, allowed, {"symbol", "market", "as_of", "sources"}, "request"))
    if not re.fullmatch(r"[A-Za-z0-9.^=-]{1,32}", text(value["symbol"], "symbol")):
        raise ValueError("unsupported symbol")
    value["symbol"] = value["symbol"].upper()
    if value["market"] not in {"US", "HK"}:
        raise ValueError("market must be US or HK")
    value.setdefault("schema_version", SCHEMA_VERSION)
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported request schema")
    value.setdefault("currency", "USD" if value["market"] == "US" else "HKD")
    if not isinstance(value["currency"], str) or not re.fullmatch(r"[A-Z]{3}", value["currency"]):
        raise ValueError("currency must be an ISO currency code")
    value.setdefault("asset_type", "stock")
    if value["asset_type"] not in {"stock", "etf"}:
        raise ValueError("asset_type must be stock or etf")
    value["as_of"] = iso(parse_time(value["as_of"]))
    value.setdefault("lookback_days", 60)
    if type(value["lookback_days"]) is not int or not 30 <= value["lookback_days"] <= 60:
        raise ValueError("lookback_days must be an integer from 30 to 60")
    value.setdefault("recent_days", 7)
    if type(value["recent_days"]) is not int or value["recent_days"] != 7:
        raise ValueError("recent_days must be 7; selection expands to 30 days only when necessary")
    if not isinstance(value["sources"], list) or not value["sources"]:
        raise ValueError("sources must explicitly name at least one configured source")
    source_keys = {"source_id", "symbol", "cik", "topics", "series_id", "domain", "entity", "language", "permissions"}
    for source in value["sources"]:
        object_keys(source, source_keys, {"source_id"}, "source")
        text(source["source_id"], "source_id")
    budget = dict(value.get("budget", {}))
    object_keys(budget, {"max_requests", "batch_size", "max_analysis_events"}, set(), "budget")
    for key, default, minimum, ceiling in (("max_requests", 120, 0, 120), ("batch_size", 100, 1, 1000), ("max_analysis_events", 1000, 0, 100000)):
        budget.setdefault(key, default)
        if type(budget[key]) is not int or not minimum <= budget[key] <= ceiling:
            raise ValueError(f"{key} must be an integer in {minimum}..{ceiling}")
    value["budget"] = budget
    for key, default in (("policy_version", POLICY_VERSION), ("prompt_version", PROMPT_VERSION), ("model_id", "host")):
        value.setdefault(key, default)
        text(value[key], key)
    value.setdefault("collection_mode", "backfill")
    if value["collection_mode"] not in {"backfill", "incremental"}:
        raise ValueError("collection_mode must be backfill or incremental")
    if sanitize_data(value) != value:
        raise ValueError("request must not contain credentials or tool instructions")
    return value


def window(request: dict) -> tuple[str, str]:
    return iso(parse_time(request["as_of"]) - timedelta(days=request["lookback_days"])), request["as_of"]


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def schema(kind: str) -> dict:
    string = {"type": "string", "minLength": 1}
    strings = {"type": "array", "items": string, "uniqueItems": True}
    nullable_direction = {"enum": DIRECTIONS}
    nullable_time = {"type": ["string", "null"], "description": "ISO timestamp with zone, or a source date preserved at date precision"}
    permissions = _obj({"store_text": {"type": "boolean"}, "retention_days": {"type": "integer", "minimum": 0, "maximum": 3650}})
    source_config = _obj({"source_id": string, "symbol": string, "cik": {"type": ["string", "integer"]},
                          "topics": {"oneOf": [string, strings]}, "series_id": string, "domain": {"enum": ["company", "macro", "financial"]},
                          "entity": string, "language": string, "permissions": permissions}, ["source_id"])
    interval_spec = _obj({"start": string, "end": string, "reason": string}, ["start", "end"])
    coverage_spec = _obj({"receipt_id": string, "source_id": string, "entity": string, "domain": {"enum": ["company", "macro", "financial"]},
                         "requested_start": string, "requested_end": string,
                         "successful_intervals": {"type": "array", "items": interval_spec},
                         "failed_intervals": {"type": "array", "items": interval_spec},
                         "status": {"enum": ["success", "partial", "failed", "unavailable"]}, "cursor": {"type": ["string", "null"]},
                         "exhausted": {"type": "boolean"}, "cap_reached": {"type": "boolean"},
                         "fetched_count": {"type": "integer", "minimum": 0}, "deduplicated_count": {"type": "integer", "minimum": 0},
                         "last_success_at": nullable_time, "reason": {"type": ["string", "null"]}, "query_key": string},
                        ["receipt_id", "source_id", "entity", "domain", "requested_start", "requested_end", "successful_intervals", "failed_intervals",
                         "status", "cursor", "exhausted", "cap_reached", "fetched_count", "deduplicated_count", "last_success_at"])
    record_properties = {"source_id": string, "source_item_id": {"type": ["string", "integer"]}, "url": string,
                         "title": {"type": "string"}, "text": {"type": "string"}, "text_scope": {"enum": ["full_text", "official_excerpt", "abstract", "headline_only"]},
                         "source_kind": {"enum": ["official", "company", "independent_media", "opinion", "rumor", "unknown"]},
                         "permissions": permissions, "receipt_id": string, "time_precision": {"enum": ["timestamp", "date"]}, "timezone": string,
                         "language": string, "origin_id": string, "event_key": string, "entities": strings,
                         "category": {"enum": ["company", "industry", "macro"]}, "data_type": {"enum": ["news", "financial", "exposure"]},
                         "status": {"enum": ["active", "corrected", "withdrawn", "denied"]}, "substantive_update": {"type": "boolean"},
                         "ongoing": {"type": "boolean"}, "evidence_locator": string,
                         "confirmed_numbers": {"type": "array", "items": _obj({"value": {"type": ["string", "number"]}, "unit": string, "subject": string})}}
    record_properties.update(dict.fromkeys(("published_at", "updated_at", "occurred_at", "effective_at", "scheduled_at", "fetched_at", "original_published_at"), nullable_time))
    record_spec = _obj(record_properties, ["source_id", "source_item_id", "url", "permissions"])
    record_refs = {"type": "array", "items": _obj({"source_id": string, "source_item_id": {"type": ["string", "integer"]}})}
    fact_spec = _obj({"fact_id": string, "metric": {"enum": ["revenue", "net_income", "operating_cash_flow", "capital_expenditure", "free_cash_flow", "cash", "debt", "eps", "shares", "market_cap", "price", "operating_data"]},
                     "value": {"type": ["string", "number"]}, "unit": string, "currency": string,
                     "period_start": string, "period_end": string, "period_kind": {"enum": ["quarter", "cumulative", "annual", "instant", "disclosed_ttm"]},
                     "disclosed_at": string, "accounting_basis": string, "accession": string, "basis": string,
                     "evidence_ids": strings, "record_refs": record_refs, "capex_sign": {"enum": ["positive_outflow", "negative_outflow"]}, "subject": string},
                    ["metric", "value", "unit", "currency", "period_end", "period_kind", "disclosed_at", "accounting_basis", "basis"])
    exposure_spec = _obj({"exposure_id": string, "target": string, "kind": {"enum": ["industry", "region", "raw_material", "interest_rate", "fx", "etf_holdings"]},
                         "description": string, "effective_at": string, "updated_at": string, "evidence_ids": strings, "record_refs": record_refs},
                        ["target", "kind", "description", "effective_at", "updated_at"])
    assessment = _obj({
        "event_id": string, "target": string, "evidence_ids": strings, "fact_summary": string, "mechanism": string,
        "direction": nullable_direction, "importance": {"enum": ["major", "routine"]}, "importance_reason": string,
        "time_horizon": string, "evidence_status": {"enum": EVIDENCE_STATES}, "counter_evidence_ids": strings,
        "counterargument": string, "exposure_ids": strings, "impact_scale": string, "persistence": string,
        "new_information": string, "uncertainty": string, "conditions": strings,
    })
    schemas = {
        "request": _obj({
            "schema_version": {"const": SCHEMA_VERSION}, "symbol": string, "market": {"enum": ["US", "HK"]},
            "currency": string, "asset_type": {"enum": ["stock", "etf"]}, "as_of": {"type": "string", "format": "date-time"},
            "lookback_days": {"type": "integer", "minimum": 30, "maximum": 60, "default": 60}, "recent_days": {"const": 7},
            "sources": {"type": "array", "minItems": 1, "items": source_config},
            "budget": _obj({"max_requests": {"type": "integer", "minimum": 0, "maximum": 120, "default": 120},
                            "batch_size": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                            "max_analysis_events": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 1000}}, []),
            "model_id": string, "policy_version": string, "prompt_version": string,
            "collection_mode": {"enum": ["backfill", "incremental"]},
        }, ["symbol", "market", "as_of", "sources"]),
        "assessment": _obj({
            "schema_version": {"const": SCHEMA_VERSION}, "input_digest": string, "model_id": string,
            "assessments": {"type": "array", "items": assessment},
            "selection": _obj({"selected_event_id": {"type": ["string", "null"]}, "reason": string,
                               "comparison": string, "uncertain": {"type": "boolean"}}),
            "synthesis": _obj({"direction": nullable_direction, "status": {"enum": EVIDENCE_STATES},
                               "primary_event_ids": strings, "background_event_ids": strings, "reason": string,
                               "counter_evidence_ids": strings, "counterargument": string, "why_not_counter": string,
                               "change_conditions": strings}),
        }),
        "import": _obj({"records": {"type": "array", "items": record_spec},
                        "coverage": {"type": "array", "items": coverage_spec},
                        "financial_facts": {"type": "array", "items": fact_spec},
                        "exposures": {"type": "array", "items": exposure_spec}}, []),
    }
    if kind not in schemas:
        raise ValueError("unknown schema kind")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:stock-data:{SCHEMA_VERSION}:{kind}", **schemas[kind]}


def validate_shape(value: Any, spec: dict, label: str = "assessment") -> None:
    """Validate the small JSON Schema subset used by the host contract."""
    if "const" in spec and value != spec["const"]:
        raise ValueError(f"{label}: incorrect constant")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{label}: invalid enum")
    kind = spec.get("type")
    if isinstance(kind, list):
        if value is None and "null" in kind:
            return
        kind = next(item for item in kind if item != "null")
    if kind == "object":
        object_keys(value, set(spec["properties"]), set(spec["required"]), label)
        for key, child in value.items():
            validate_shape(child, spec["properties"][key], f"{label}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{label}: expected array")
        if spec.get("uniqueItems"):
            string_list(value, label)
        for index, item in enumerate(value):
            validate_shape(item, spec["items"], f"{label}[{index}]")
    elif kind == "string":
        text(value, label)
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError(f"{label}: expected boolean")
