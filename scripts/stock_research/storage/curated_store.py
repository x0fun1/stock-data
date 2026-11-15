"""Compressed JSON record storage with a SQLite version catalog.

The news skill uses the Python standard library for storage; no numerical or
columnar-data framework is needed by the installed package.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..common.errors import DataError
from ..common.hashing import canonical_hash
from ..common.jsonio import to_jsonable
from ..common.timeutil import now_utc
from .catalog import Catalog


def validate_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if records is None or isinstance(records, (str, bytes, Mapping)):
        raise DataError("dataset must be a list of record objects")
    rows = list(records)
    if any(not isinstance(row, Mapping) for row in rows):
        raise DataError("every dataset record must be an object")
    return to_jsonable([dict(row) for row in rows])


def write_record_file(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    rows = validate_records(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".part.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as compressed:
                compressed.write(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"), allow_nan=False).encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise DataError(f"invalid dataset name or version: {value!r}")
    return value


class CuratedStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.curated_root = self.root / "curated"
        self.curated_root.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(self.root)

    def save(
        self,
        name: str,
        records: Iterable[Mapping[str, Any]],
        metadata: dict[str, Any] | None = None,
        source_manifests: Iterable[str] | None = None,
    ) -> str:
        rows = validate_records(records)
        version = f"{now_utc().strftime('%Y%m%dT%H%M%S%fZ')}-{canonical_hash(rows)[:12]}"
        path = self.curated_root / _identifier(name) / f"{version}.json.gz"
        write_record_file(path, rows)
        self.catalog.register_dataset(name, version, path, len(rows), metadata or {}, list(source_manifests or []))
        return version

    def load(self, name: str, version: str | None = None) -> list[dict[str, Any]]:
        _identifier(name)
        if version is not None:
            _identifier(version)
        entry = self.catalog.get_dataset(name, version)
        if entry is None:
            raise DataError(f"dataset {name} version {version or 'latest'} is not in the catalog")
        path = Path(entry["path"])
        if not path.resolve().is_relative_to(self.root):
            raise DataError("dataset file leaves the runtime root")
        if not path.is_file():
            raise DataError(f"dataset file is missing: {path}")
        if path.suffix == ".parquet":
            raise DataError("legacy Parquet dataset: run scripts/migrate_runtime.py using the old environment before removing it")
        if not path.name.endswith(".json.gz"):
            raise DataError(f"unsupported dataset format: {path.name}")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return validate_records(json.load(handle))

    def metadata(self, name: str, version: str | None = None) -> dict[str, Any]:
        entry = self.catalog.get_dataset(name, version)
        if entry is None:
            raise DataError(f"dataset {name} is not in the catalog")
        return entry

    def versions(self, name: str) -> list[dict[str, Any]]:
        return self.catalog.list_datasets(name)

    def latest_version(self, name: str) -> str | None:
        entry = self.catalog.get_dataset(name)
        return None if entry is None else entry["version"]

    def snapshot(self, required: Iterable[str] = ()) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"datasets": {}, "missing": []}
        for name in required:
            entry = self.catalog.get_dataset(name)
            if entry is None:
                snapshot["missing"].append(name)
                continue
            snapshot["datasets"][name] = {
                "version": entry["version"], "path": entry["path"], "rows": entry["rows"],
                "created_at": entry["created_at"], "metadata": entry["metadata"],
            }
        return snapshot
