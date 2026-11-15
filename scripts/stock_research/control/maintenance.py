"""Authorized local CSV/JSON imports, with no network calls or numerical dependencies."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from ..common.errors import DataError, SourceNotAllowed
from ..common.timeutil import parse_date, parse_dt
from ..config import load_policies
from ..contracts import Market
from ..storage.curated_store import CuratedStore, validate_records
from ..storage.retention import retained_runtime

IMPORT_KINDS = ("news", "macro", "securities")


def _read(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise DataError(f"import file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".jsonl":
            data = [json.loads(line) for line in handle if line.strip()]
        elif path.suffix.lower() == ".json":
            data = json.load(handle)
            if isinstance(data, dict):
                data = data["records"] if "records" in data else [data]
        else:
            data = list(csv.DictReader(handle))
    return validate_records(data)


def _require(records: list[dict], columns: list[str], kind: str) -> None:
    missing = sorted({key for row in records for key in columns if key not in row})
    if missing:
        raise DataError(f"{kind} import missing columns: {missing}")


@retained_runtime()
def import_file(
    kind: str,
    path: str | Path,
    market: Market | str,
    runtime_root: str | Path,
    source_id: str = "user_import",
    skill_root: str | Path | None = None,
) -> dict[str, Any]:
    market = Market(market)
    policies = load_policies(skill_root)
    if kind not in IMPORT_KINDS:
        raise DataError(f"unknown import kind: {kind}")
    if not policies.source_allowed(source_id, market.value, kind):
        raise SourceNotAllowed(f"source {source_id} is not allowed for {market.value}/{kind}")
    records = _read(path)
    if not records:
        raise DataError("import file is empty")
    required = {"news": ["title", "published_at", "source_id"],
                "macro": ["series_id", "observation_date", "value"],
                "securities": ["ticker", "market", "valid_from"]}
    _require(records, required[kind], kind)
    for row in records:
        row.setdefault("market", market.value)
        row.setdefault("source_id", source_id)
        if kind == "macro":
            row["observation_date"] = parse_date(str(row["observation_date"]).replace("/", "-")).isoformat()
            row["value"] = float(row["value"])
            if not math.isfinite(row["value"]):
                raise DataError("macro values must be finite")
        elif kind == "news":
            if row["published_at"]:
                row["published_at"] = parse_dt(row["published_at"]).isoformat()
            # Native JSON retains nested objects. CSV/legacy JSON strings are
            # decoded by ingest without forcing large columnar libraries.
    store = CuratedStore(runtime_root)
    version = store.save(kind, records, {"kind": kind, "market": market.value, "source_id": source_id})
    return {"dataset": kind, "version": version, "rows": len(records),
            "columns": sorted({column for row in records for column in row})}
