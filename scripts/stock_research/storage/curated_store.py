"""Parquet curated store with a SQLite version catalog."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ..common.errors import DataError
from ..common.hashing import sha256_file
from ..common.timeutil import now_utc
from .catalog import Catalog


class CuratedStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.curated_root = self.root / "curated"
        self.curated_root.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(self.root)

    def save(
        self,
        name: str,
        frame: pd.DataFrame,
        metadata: dict[str, Any] | None = None,
        source_manifests: Iterable[str] | None = None,
    ) -> str:
        if frame is None:
            raise DataError(f"dataset {name} is None")
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame(frame)
        if frame.columns.duplicated().any():
            raise DataError(f"dataset {name} has duplicate columns")
        target_dir = self.curated_root / name
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".part.", suffix=".parquet", dir=str(target_dir))
        os.close(fd)
        try:
            frame.to_parquet(tmp, index=False)
            digest = sha256_file(tmp)[:12]
            version = f"{now_utc().strftime('%Y%m%dT%H%M%S%fZ')}-{digest}"
            final = target_dir / f"{version}.parquet"
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.catalog.register_dataset(
            name,
            version,
            final,
            len(frame),
            metadata or {},
            list(source_manifests or []),
        )
        return version

    def load(self, name: str, version: str | None = None) -> pd.DataFrame:
        entry = self.catalog.get_dataset(name, version)
        if entry is None:
            raise DataError(f"dataset {name} version {version or 'latest'} is not in the catalog")
        path = Path(entry["path"])
        if not path.exists():
            raise DataError(f"dataset file is missing: {path}")
        return pd.read_parquet(path)

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
                "version": entry["version"],
                "path": entry["path"],
                "rows": entry["rows"],
                "created_at": entry["created_at"],
                "metadata": entry["metadata"],
            }
        return snapshot
