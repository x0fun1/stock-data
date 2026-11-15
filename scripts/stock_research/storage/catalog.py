"""SQLite catalog for dataset versions and frozen message reports."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..common.timeutil import iso, now_utc


class Catalog:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS datasets (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    rows INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    source_manifests TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (name, version)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    path TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '{}'
                );
            """)

    def register_dataset(self, name: str, version: str, path: str | Path, rows: int,
                         metadata: dict[str, Any] | None = None,
                         source_manifests: list[str] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO datasets(name, version, path, rows, created_at, metadata, source_manifests) VALUES (?,?,?,?,?,?,?)",
                (name, version, str(path), int(rows), iso(now_utc()),
                 json.dumps(metadata or {}, ensure_ascii=False, default=str), json.dumps(source_manifests or [])),
            )

    def list_datasets(self, name: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM datasets WHERE name = ? ORDER BY created_at DESC", (name,)).fetchall()
        return [dict(row) | {"metadata": json.loads(row["metadata"]),
                            "source_manifests": json.loads(row["source_manifests"])} for row in rows]

    def get_dataset(self, name: str, version: str | None = None) -> dict[str, Any] | None:
        with self._conn() as conn:
            if version:
                row = conn.execute("SELECT * FROM datasets WHERE name = ? AND version = ?", (name, version)).fetchone()
            else:
                row = conn.execute("SELECT * FROM datasets WHERE name = ? ORDER BY created_at DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            return None
        return dict(row) | {"metadata": json.loads(row["metadata"]),
                            "source_manifests": json.loads(row["source_manifests"])}

    def register_run(self, run_id: str, path: str | Path, summary: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO runs(run_id, created_at, path, summary) VALUES (?,?,?,?)",
                         (run_id, iso(now_utc()), str(path), json.dumps(summary or {}, ensure_ascii=False, default=str)))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else dict(row) | {"summary": json.loads(row["summary"])}

    def replace_dataset_path(self, name: str, version: str, path: Path) -> None:
        """Migrate storage without changing version identity or registration time."""
        with self._conn() as conn:
            changed = conn.execute("UPDATE datasets SET path = ? WHERE name = ? AND version = ?",
                                   (str(path), name, version)).rowcount
            if changed != 1:
                raise ValueError("dataset version was not registered")
