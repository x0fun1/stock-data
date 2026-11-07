"""SQLite catalog for dataset versions, runs, models and experiments."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
            conn.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS models (
                    model_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    path TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def register_dataset(
        self,
        name: str,
        version: str,
        path: str | Path,
        rows: int,
        metadata: dict[str, Any] | None = None,
        source_manifests: list[str] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO datasets(name, version, path, rows, created_at, metadata, source_manifests) VALUES (?,?,?,?,?,?,?)",
                (
                    name,
                    version,
                    str(path),
                    int(rows),
                    iso(now_utc()),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    json.dumps(source_manifests or []),
                ),
            )

    def list_datasets(self, name: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM datasets WHERE name = ? ORDER BY created_at DESC", (name,)
            ).fetchall()
        return [dict(r) | {"metadata": json.loads(r["metadata"]), "source_manifests": json.loads(r["source_manifests"])} for r in rows]

    def get_dataset(self, name: str, version: str | None = None) -> dict[str, Any] | None:
        with self._conn() as conn:
            if version:
                row = conn.execute("SELECT * FROM datasets WHERE name = ? AND version = ?", (name, version)).fetchone()
            else:
                row = conn.execute("SELECT * FROM datasets WHERE name = ? ORDER BY created_at DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["source_manifests"] = json.loads(result["source_manifests"])
        return result

    def register_run(self, run_id: str, path: str | Path, summary: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, created_at, path, summary) VALUES (?,?,?,?)",
                (run_id, iso(now_utc()), str(path), json.dumps(summary or {}, ensure_ascii=False, default=str)),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = json.loads(result["summary"])
        return result

    def register_model(self, model_id: str, path: str | Path, status: str, metadata: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO models(model_id, created_at, path, status, metadata) VALUES (?,?,?,?,?)",
                (model_id, iso(now_utc()), str(path), status, json.dumps(metadata or {}, ensure_ascii=False, default=str)),
            )

    def update_model_status(self, model_id: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE models SET status = ?, metadata = ? WHERE model_id = ?",
                (status, json.dumps(metadata or {}, ensure_ascii=False, default=str), model_id),
            )

    def list_models(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM models WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM models ORDER BY created_at DESC").fetchall()
        return [dict(r) | {"metadata": json.loads(r["metadata"])} for r in rows]

    def register_experiment(self, experiment_id: str, path: str | Path, metadata: dict[str, Any] | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiments(experiment_id, created_at, path, metadata) VALUES (?,?,?,?)",
                (experiment_id, iso(now_utc()), str(path), json.dumps(metadata or {}, ensure_ascii=False, default=str)),
            )
