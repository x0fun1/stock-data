"""Versioned SQLite evidence and immutable, hash-checked research inputs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.jsonio import digest, read_json, write_json
from ..common.paths import child
from ..common.time import now, parse_time

TABLES = ("articles", "evidence", "events", "financial_facts", "exposures", "coverage", "sources")
RUN_FILES = {"snapshot.json", "manifest.json", "prepared.json", "assessment.json", "result.json", "report.md", "delivery.json"}


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = child(self.root, "evidence.sqlite3")
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        for table in TABLES:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TEXT NOT NULL, expires_at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, input_digest TEXT NOT NULL, status TEXT NOT NULL)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def put(self, table: str, values: list[dict], *, id_key: str | None = None) -> None:
        if table not in TABLES:
            raise ValueError("unknown evidence table")
        with self.db:
            for value in values:
                identity = value[id_key] if id_key else digest(value)
                # Immutable version IDs: repeat imports are idempotent; newer versions do not overwrite old ones.
                self.db.execute(f"INSERT OR IGNORE INTO {table} VALUES (?, ?, ?, ?)",  # noqa: S608 - table is checked against the constant TABLES allowlist
                                (identity, json.dumps(value, ensure_ascii=False, allow_nan=False), now(), value.get("expires_at")))

    def values(self, table: str) -> list[dict]:
        if table not in TABLES:
            raise ValueError("unknown evidence table")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {table} ORDER BY id")]  # noqa: S608 - constant table allowlist

    def freeze(self, snapshot: dict) -> Path:
        runs = child(self.root, "runs")
        runs.mkdir(exist_ok=True)
        run_id = f"run-{uuid4().hex}"
        run_dir = child(runs, run_id)
        run_dir.mkdir()
        checksum = digest(snapshot)
        # A manifest appears only after the complete snapshot is durable. An interrupted directory is not a valid run.
        write_json(child(run_dir, "snapshot.json"), snapshot)
        write_json(child(run_dir, "manifest.json"), {"run_id": run_id, "created_at": now(), "input_digest": checksum,
                                                     "snapshot_file": "snapshot.json", "replay_status": "available"})
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, now(), checksum, "collected"))
        return run_dir


def load_snapshot(run_dir: Path) -> tuple[dict, dict]:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("run directory must be a real directory, not a link")
    manifest = read_json(child(run_dir, "manifest.json"))
    if manifest.get("replay_status") != "available":
        raise ValueError("evidence expired: this run can no longer be replayed locally")
    snapshot = read_json(child(run_dir, "snapshot.json"))
    if digest(snapshot) != manifest["input_digest"]:
        raise ValueError("snapshot integrity check failed")
    for item in snapshot.get("records", []):
        if item.get("text") and item.get("expires_at") and parse_time(item["expires_at"]) <= parse_time(now()):
            raise ValueError("evidence retention expired; run gc before further use")
    return snapshot, manifest


@contextmanager
def run_lock(run_dir: Path):
    """A SQLite transaction is released even after process failure, unlike a stale PID file."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("run directory must be a real directory, not a link")
    db_path = child(run_dir.parent.parent, "evidence.sqlite3")
    if not db_path.exists():
        raise ValueError("run is not attached to an evidence store")
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    finally:
        connection.close()


def gc(root: Path, *, retention_days: int = 120, dry_run: bool = True) -> dict:
    if type(retention_days) is not int or retention_days < 120:
        raise ValueError("ordinary retention must be at least 120 days; shorter licenses are enforced separately")
    if not root.exists():
        return {"dry_run": dry_run, "actions": [], "skipped": []}
    store = Store(root)
    actions: list[dict[str, Any]] = []
    skipped = []
    instant = parse_time(now())
    cutoff = instant - timedelta(days=retention_days)
    try:
        store.db.execute("BEGIN IMMEDIATE")
        expired_ids = set()
        for table in ("articles", "evidence"):
            for identity, payload, expiry in store.db.execute(f"SELECT id, payload, expires_at FROM {table} WHERE expires_at IS NOT NULL"):  # noqa: S608 - iterates two literal table names
                if parse_time(expiry) > instant:
                    continue
                data = json.loads(payload)
                if data.get("local_status") == "expired":
                    continue
                expired_ids.add(identity)
                actions.append({"table": table, "id": identity, "action": "expire_content"})
                if not dry_run:
                    for field in ("text", "quote", "title", "confirmed_numbers"):
                        data.pop(field, None)
                    data["local_status"] = "expired"
                    store.db.execute(f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(data, ensure_ascii=False), identity))  # noqa: S608 - literal table names; values are parameterized
        runs = child(root, "runs")
        if runs.exists():
            for run in runs.iterdir():
                if run.is_symlink() or not run.is_dir() or run.resolve().parent != runs.resolve():
                    skipped.append({"run": run.name, "reason": "not a safe run directory"})
                    continue
                try:
                    manifest = read_json(child(run, "manifest.json"))
                    registered = store.db.execute("SELECT input_digest FROM runs WHERE id=?", (run.name,)).fetchone()
                    if not registered or manifest.get("run_id") != run.name or manifest.get("input_digest") != registered[0]:
                        raise ValueError("unregistered run or mismatched manifest; manual inspection required")
                    known = set(RUN_FILES)
                    entries = list(run.iterdir())
                    if any(entry.is_symlink() or not entry.is_file() or entry.name not in known for entry in entries):
                        raise ValueError("unknown files or links; manual inspection required")
                    old = parse_time(manifest["created_at"]) <= cutoff
                    expired = manifest.get("replay_status") == "expired"
                    if not expired and (run / "snapshot.json").exists():
                        data = read_json(run / "snapshot.json")
                        if digest(data) != manifest["input_digest"]:
                            raise ValueError("snapshot integrity mismatch; manual inspection required")
                        expired = any(item.get("expires_at") and parse_time(item["expires_at"]) <= instant
                                      for item in data.get("records", []) if item.get("text"))
                    if not old and not expired:
                        continue
                    action = "remove_run" if old else "expire_run_content"
                    actions.append({"run": run.name, "action": action})
                    if not dry_run:
                        for entry in entries:
                            if entry.name != "manifest.json":
                                entry.unlink()
                        if old:
                            (run / "manifest.json").unlink()
                            run.rmdir()
                        else:
                            manifest["replay_status"] = "expired"
                            manifest["reason"] = "source retention elapsed; referenced content no longer locally replayable"
                            write_json(run / "manifest.json", manifest)
                        store.db.execute("UPDATE runs SET status=? WHERE id=?", (action, run.name))
                except (ValueError, OSError, KeyError) as exc:
                    skipped.append({"run": run.name, "reason": str(exc)})
        if not dry_run:
            store.db.commit()
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            store.db.rollback()
    finally:
        store.close()
    return {"dry_run": dry_run, "actions": actions, "skipped": skipped,
            "preserved": ["evidence.sqlite3", "coverage", "source metadata", "event versions", "run index"]}
