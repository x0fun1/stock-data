"""Authorized local imports.

No source is fetched here.  The caller supplies a CSV/JSON file, and the import
is checked against the source policy before a new curated version is written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..common.errors import DataError, SourceNotAllowed
from ..config import load_policies
from ..contracts import Market, make_security_id
from ..storage.curated_store import CuratedStore


def _read(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise DataError(f"import file not found: {path}")
    if path.suffix.lower() == ".jsonl":
        return pd.read_json(path, lines=True)
    if path.suffix.lower() == ".json":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return pd.DataFrame(data)
        if isinstance(data, dict) and "records" in data:
            return pd.DataFrame(data["records"])
        if isinstance(data, dict):
            return pd.DataFrame([data])
        raise DataError("JSON import must be an object, record list, or {records: [...]}")
    return pd.read_csv(path, encoding="utf-8-sig")


def _require(frame: pd.DataFrame, columns: list[str], kind: str) -> None:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise DataError(f"{kind} import missing columns: {missing}")


def import_file(
    kind: str,
    path: str | Path,
    market: Market | str,
    runtime_root: str | Path,
    ticker: str | None = None,
    security_id: str | None = None,
    source_id: str = "user_import",
    skill_root: str | Path | None = None,
) -> dict[str, Any]:
    market = Market(market)
    policies = load_policies(skill_root)
    dataset = {
        "prices": "prices",
        "actions": "actions",
        "coverage": "action_coverage",
        "financial_facts": "financial_facts",
        "macro": "macro",
        "news": "news",
        "securities": "securities",
        "security_metadata": "security_metadata",
    }.get(kind)
    if dataset is None:
        raise DataError(f"unknown import kind: {kind}")
    if not policies.source_allowed(source_id, market.value, dataset):
        raise SourceNotAllowed(f"source {source_id} is not allowed for {market.value}/{dataset}")
    store = CuratedStore(Path(runtime_root))
    frame = _read(path)
    if frame.empty:
        raise DataError("import file is empty")
    if kind == "prices":
        _require(frame, ["date", "close"], kind)
        if "security_id" not in frame.columns:
            if not ticker:
                raise DataError("prices import needs security_id or ticker")
            frame["security_id"] = make_security_id(market, "XNAS" if market is Market.US else "XHKG", ticker)
        frame["market"] = market.value
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        dup = frame.duplicated(subset=["security_id", "date"]).sum()
        if dup:
            raise DataError(f"prices import has {dup} duplicate security/date rows")
        defaults = {"open": None, "high": None, "low": None, "volume": None, "amount": None, "currency": "USD" if market is Market.US else "HKD", "source_id": source_id, "quality": "ok"}
        for column, default in defaults.items():
            if column not in frame.columns:
                frame[column] = default
        frame["available_at"] = frame["available_at"] if "available_at" in frame.columns else None
    elif kind == "actions":
        _require(frame, ["security_id", "ex_date", "action_type"], kind)
        frame["ex_date"] = pd.to_datetime(frame["ex_date"]).dt.strftime("%Y-%m-%d")
        for column, default in {"q": 1.0, "cash_old": 0.0, "currency": "", "source_id": source_id, "confirmed": True, "supported": True, "note": ""}.items():
            if column not in frame.columns:
                frame[column] = default
    elif kind == "coverage":
        _require(frame, ["security_id", "start", "end", "complete"], kind)
        frame["start"] = pd.to_datetime(frame["start"]).dt.strftime("%Y-%m-%d")
        frame["end"] = pd.to_datetime(frame["end"]).dt.strftime("%Y-%m-%d")
    elif kind == "financial_facts":
        _require(frame, ["security_id", "concept", "value", "period_end"], kind)
        frame["period_end"] = pd.to_datetime(frame["period_end"]).dt.strftime("%Y-%m-%d")
        if "period_start" in frame.columns:
            frame["period_start"] = pd.to_datetime(frame["period_start"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "available_at" not in frame.columns:
            raise DataError("financial facts need available_at to support point-in-time use")
    elif kind == "macro":
        _require(frame, ["series_id", "observation_date", "value"], kind)
        frame["observation_date"] = pd.to_datetime(frame["observation_date"]).dt.strftime("%Y-%m-%d")
    elif kind == "news":
        _require(frame, ["title", "published_at", "source_id"], kind)
        frame["published_at"] = pd.to_datetime(frame["published_at"], errors="coerce", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    elif kind == "securities":
        _require(frame, ["ticker", "market", "valid_from"], kind)
    elif kind == "security_metadata":
        _require(frame, ["security_id", "effective_from", "available_at", "industry"], kind)
        dates = pd.to_datetime(frame["available_at"], errors="coerce", utc=True)
        if dates.isna().any():
            raise DataError("security_metadata requires a valid historical available_at for every record")
        effective = pd.to_datetime(frame["effective_from"], errors="coerce", utc=True)
        if effective.isna().any():
            raise DataError("security_metadata requires valid effective_from dates")
        frame["available_at"] = dates.dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        frame["effective_from"] = effective.dt.strftime("%Y-%m-%d")
        if "effective_to" in frame:
            frame["effective_to"] = pd.to_datetime(frame["effective_to"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
        if "pit_verified" not in frame:
            frame["pit_verified"] = False
    version = store.save(dataset, frame, {"kind": kind, "market": market.value, "source_id": source_id}, source_manifests=[])
    return {"dataset": dataset, "version": version, "rows": int(len(frame)), "columns": list(frame.columns)}
