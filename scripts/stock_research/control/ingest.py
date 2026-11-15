"""Convert stored JSON records into typed message contracts."""
from __future__ import annotations

import ast
import json
import math

from ..common.timeutil import parse_date, parse_dt
from ..contracts import ContextEvent, Grade, MacroObservation
from ..identity_calendar.identity import SecurityMaster


def _value(row, name, default=None):
    value = row.get(name)
    if value is None or value == "" or (isinstance(value, float) and math.isnan(value)):
        return default
    return value


def _jsonish(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            # Accept legacy Parquet fields containing literal list/dict strings.
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return default
    return default


def macro_from_frame(records: list[dict]) -> list[MacroObservation]:
    out = []
    for data in records:
        try:
            out.append(MacroObservation(
                series_id=str(data["series_id"]), observation_date=parse_date(data["observation_date"]),
                value=float(data["value"]), unit=str(_value(data, "unit", "")), market=_value(data, "market"),
                available_at=parse_dt(data["available_at"]) if _value(data, "available_at") else None,
                vintage_id=str(_value(data, "vintage_id", "")), source_id=str(_value(data, "source_id", "")),
                quality=str(_value(data, "quality", "ok")),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def news_from_frame(records: list[dict]) -> list[ContextEvent]:
    out = []
    for data in records:
        try:
            security_ids = _jsonish(_value(data, "security_ids"), [])
            if isinstance(security_ids, str):
                security_ids = [security_ids]
            out.append(ContextEvent(
                event_id=str(_value(data, "event_id", "")), market=str(_value(data, "market", "US")),
                security_ids=[str(x) for x in security_ids], title=str(_value(data, "title", "")),
                published_at=parse_dt(data["published_at"]) if _value(data, "published_at") else None,
                available_at=parse_dt(data["available_at"]) if _value(data, "available_at") else None,
                received_at=parse_dt(data["received_at"]) if _value(data, "received_at") else None,
                source_id=str(_value(data, "source_id", "")), source_kind=str(_value(data, "source_kind", "unknown")),
                evidence_scope=str(_value(data, "evidence_scope", "none")), url=str(_value(data, "url", "")),
                body=str(_value(data, "body", "")), evidence_locator=str(_value(data, "evidence_locator", "")),
                event_cluster=str(_value(data, "event_cluster", "")), grade=str(_value(data, "grade", Grade.INSUFFICIENT.value)),
                grade_method=str(_value(data, "grade_method", "insufficient_evidence")),
                grade_reason=str(_value(data, "grade_reason", "")), grade_version=str(_value(data, "grade_version", "")),
                fact_summary=str(_value(data, "fact_summary", "")), permissions=_jsonish(_value(data, "permissions"), {}),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def securities_from_frame(records: list[dict]) -> SecurityMaster:
    rows = []
    for data in records:
        if not _value(data, "ticker"):
            continue
        rows.append({
            "security_id": _value(data, "security_id", ""), "ticker": data["ticker"],
            "market": _value(data, "market", "US"), "exchange": _value(data, "exchange", ""),
            "security_type": _value(data, "security_type", "common_stock"), "currency": _value(data, "currency", ""),
            "valid_from": _value(data, "valid_from", "1900-01-01"), "valid_to": _value(data, "valid_to"),
            "name": _value(data, "name", ""), "industry": _value(data, "industry", ""),
            "source_id": _value(data, "source_id", ""),
        })
    return SecurityMaster.from_rows(rows)
