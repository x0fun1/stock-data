"""Convert stored tables into typed contracts without inventing fields."""

from __future__ import annotations

import ast
import json
from datetime import date
from typing import Any

import pandas as pd

from ..common.errors import DataError
from ..common.timeutil import parse_date, parse_dt
from ..contracts import (
    ActionCoverage,
    ActionType,
    ContextEvent,
    CorporateAction,
    EvidenceScope,
    FinancialFact,
    Grade,
    MacroObservation,
    Market,
    PriceBar,
    SecurityIdentity,
    SourceKind,
)
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries
from ..identity_calendar.identity import SecurityMaster


def _value(row, name, default=None):
    if name not in row:
        return default
    value = row[name]
    if pd.isna(value):
        return default
    return value


def _jsonish(value, default):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            try:
                return ast.literal_eval(value)
            except Exception:
                return default
    return default


def _bool(value, default=True) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def prices_from_frame(frame: pd.DataFrame, market: Market | str) -> dict[str, PriceSeries]:
    market = Market(market)
    if frame.empty:
        return {}
    frame = frame[frame["market"].astype(str).str.upper() == market.value] if "market" in frame else frame
    grouped: dict[str, list[PriceBar]] = {}
    for row in frame.itertuples(index=False):
        data = row._asdict()
        if not _value(data, "date") or _value(data, "close") is None:
            continue
        try:
            bar = PriceBar(
                security_id=str(_value(data, "security_id")),
                date=parse_date(_value(data, "date")),
                open=None if _value(data, "open") is None else float(_value(data, "open")),
                high=None if _value(data, "high") is None else float(_value(data, "high")),
                low=None if _value(data, "low") is None else float(_value(data, "low")),
                close=None if _value(data, "close") is None else float(_value(data, "close")),
                volume=None if _value(data, "volume") is None else float(_value(data, "volume")),
                amount=None if _value(data, "amount") is None else float(_value(data, "amount")),
                currency=str(_value(data, "currency", "")),
                source_id=str(_value(data, "source_id", "")),
                available_at=parse_dt(_value(data, "available_at")) if _value(data, "available_at") else None,
                quality=str(_value(data, "quality", "ok")),
            )
        except (ValueError, TypeError, DataError):
            continue
        grouped.setdefault(bar.security_id, []).append(bar)
    return {sid: PriceSeries(bars, sid) for sid, bars in grouped.items()}


def actions_from_frame(frame: pd.DataFrame, coverage_frame: pd.DataFrame | None = None) -> ActionLedger:
    if frame.empty:
        actions: list[CorporateAction] = []
    else:
        rows = []
        for data in frame.to_dict("records"):
            if not _value(data, "security_id") or not _value(data, "ex_date"):
                continue
            rows.append(
                {
                    "security_id": str(data["security_id"]),
                    "ex_date": data["ex_date"],
                    "action_type": str(_value(data, "action_type", "other")),
                    "q": _value(data, "q", 1.0),
                    "cash_old": _value(data, "cash_old", 0.0),
                    "currency": _value(data, "currency", ""),
                    "announced_at": _value(data, "announced_at"),
                    "available_at": _value(data, "available_at"),
                    "accession": _value(data, "accession", ""),
                    "source_id": _value(data, "source_id", ""),
                    "confirmed": _value(data, "confirmed", True),
                    "supported": _value(data, "supported", True),
                    "note": _value(data, "note", ""),
                }
            )
        actions = []
        for row in rows:
            try:
                actions.append(
                    CorporateAction(
                        security_id=row["security_id"],
                        ex_date=parse_date(row["ex_date"]),
                        action_type=ActionType(str(row["action_type"])),
                        q=float(row["q"] or 1.0),
                        cash_old=float(row["cash_old"] or 0.0),
                        currency=str(row["currency"]),
                        announced_at=parse_dt(row["announced_at"]) if row["announced_at"] else None,
                        available_at=parse_dt(row["available_at"]) if row["available_at"] else None,
                        accession=str(row["accession"]),
                        source_id=str(row["source_id"]),
                        confirmed=_bool(row["confirmed"]),
                        supported=_bool(row["supported"]),
                        note=str(row["note"]),
                    )
                )
            except (ValueError, TypeError, DataError):
                continue
    coverages: list[ActionCoverage] = []
    if coverage_frame is not None and not coverage_frame.empty:
        for data in coverage_frame.to_dict("records"):
            try:
                coverages.append(
                    ActionCoverage(
                        security_id=str(data["security_id"]),
                        start=parse_date(data["start"]),
                        end=parse_date(data["end"]),
                        complete=_bool(_value(data, "complete", False), False),
                        evidence=str(_value(data, "evidence", "")),
                        unresolved=[str(x) for x in _jsonish(_value(data, "unresolved"), [])],
                        source_id=str(_value(data, "source_id", "")),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
    return ActionLedger(actions, coverages)


def financials_from_frame(frame: pd.DataFrame) -> list[FinancialFact]:
    out = []
    if frame.empty:
        return out
    for data in frame.to_dict("records"):
        try:
            out.append(
                FinancialFact(
                    security_id=str(data["security_id"]),
                    concept=str(data["concept"]),
                    value=float(data["value"]),
                    unit=str(_value(data, "unit", "")),
                    period_end=parse_date(data["period_end"]),
                    period_start=parse_date(data["period_start"]) if _value(data, "period_start") else None,
                    fiscal_year=int(_value(data, "fiscal_year")) if _value(data, "fiscal_year") is not None else None,
                    fiscal_period=str(_value(data, "fiscal_period", "")),
                    form=str(_value(data, "form", "")),
                    accession=str(_value(data, "accession", "")),
                    filed_at=parse_dt(_value(data, "filed_at")) if _value(data, "filed_at") else None,
                    available_at=parse_dt(_value(data, "available_at")) if _value(data, "available_at") else None,
                    source_id=str(_value(data, "source_id", "")),
                    quality=str(_value(data, "quality", "ok")),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


def macro_from_frame(frame: pd.DataFrame) -> list[MacroObservation]:
    out = []
    if frame.empty:
        return out
    for data in frame.to_dict("records"):
        try:
            out.append(
                MacroObservation(
                    series_id=str(data["series_id"]),
                    observation_date=parse_date(data["observation_date"]),
                    value=float(data["value"]),
                    unit=str(_value(data, "unit", "")),
                    market=_value(data, "market"),
                    available_at=parse_dt(_value(data, "available_at")) if _value(data, "available_at") else None,
                    vintage_id=str(_value(data, "vintage_id", "")),
                    source_id=str(_value(data, "source_id", "")),
                    quality=str(_value(data, "quality", "ok")),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


def news_from_frame(frame: pd.DataFrame) -> list[ContextEvent]:
    out = []
    if frame.empty:
        return out
    for data in frame.to_dict("records"):
        try:
            security_ids = _jsonish(_value(data, "security_ids"), [])
            if isinstance(security_ids, str):
                security_ids = [security_ids]
            out.append(
                ContextEvent(
                    event_id=str(_value(data, "event_id", "")),
                    market=str(_value(data, "market", "US")),
                    security_ids=[str(x) for x in security_ids],
                    title=str(_value(data, "title", "")),
                    published_at=parse_dt(_value(data, "published_at")) if _value(data, "published_at") else None,
                    available_at=parse_dt(_value(data, "available_at")) if _value(data, "available_at") else None,
                    received_at=parse_dt(_value(data, "received_at")) if _value(data, "received_at") else None,
                    source_id=str(_value(data, "source_id", "")),
                    source_kind=str(_value(data, "source_kind", "unknown")),
                    evidence_scope=str(_value(data, "evidence_scope", "none")),
                    url=str(_value(data, "url", "")),
                    body=str(_value(data, "body", "")),
                    evidence_locator=str(_value(data, "evidence_locator", "")),
                    event_cluster=str(_value(data, "event_cluster", "")),
                    grade=str(_value(data, "grade", Grade.INSUFFICIENT.value)),
                    grade_method=str(_value(data, "grade_method", "insufficient_evidence")),
                    grade_version=str(_value(data, "grade_version", "")),
                    fact_summary=str(_value(data, "fact_summary", "")),
                    permissions=_jsonish(_value(data, "permissions"), {}),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


def securities_from_frame(frame: pd.DataFrame) -> SecurityMaster:
    rows = []
    if not frame.empty:
        for data in frame.to_dict("records"):
            if not _value(data, "ticker"):
                continue
            rows.append(
                {
                    "security_id": _value(data, "security_id", ""),
                    "ticker": data["ticker"],
                    "market": _value(data, "market", "US"),
                    "exchange": _value(data, "exchange", ""),
                    "security_type": _value(data, "security_type", "common_stock"),
                    "currency": _value(data, "currency", ""),
                    "valid_from": _value(data, "valid_from", "1900-01-01"),
                    "valid_to": _value(data, "valid_to"),
                    "name": _value(data, "name", ""),
                    "industry": _value(data, "industry", ""),
                    "source_id": _value(data, "source_id", ""),
                }
            )
    return SecurityMaster.from_rows(rows)
