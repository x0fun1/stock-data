"""Bounded retention for runtime artifacts that outlive a finished research run.

`cleanup.py` removes the JSON evidence of one run after delivery, but it leaves
the report, the response and the receipt behind forever, and it refuses a tree
that a hard-killed writer left scratch paths in. This module closes both gaps
without weakening the owner-scoped deletion rules:

* orphan scratch paths are swept inside an owned run directory (the same
  patterns `sweep_orphans` in cleanup.py recognises);
* a run that was never cleaned because the host could not report delivery is
  finished after a grace period, with the retained response validated in place
  instead of the exact outgoing text;
* a completed run directory is deleted once it is older than the retention
  window, and only when it no longer holds any JSON;
* a stale provider cache is pruned by age.

Every decision is fail-closed: a linked path, an unknown file, a missing
manifest or an unfinished receipt leaves the directory untouched and reports why.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .cleanup import (
    CLEANUP_RECEIPT,
    ORPHAN_TEMP_DIR,
    PIPELINE_LOCK,
    RUN_MANIFEST,
    CleanupError,
    _exclusive_lock,
    _is_link,
    _member_path,
    _pid_is_running,
    _read_receipt,
    _tree_entries,
    _write_json,
    cleanup_owned_run,
    sweep_orphans,
)
from .contracts import utc_now
from .paths import validate_path_component

RUN_ID_PATTERN = re.compile(r"run_[0-9]{8}T[0-9]{6}[0-9]{6}Z_[0-9a-f]{8}")
GC_STATE = ".gc_state.json"
DEFAULT_RETAIN_DAYS = 30
DEFAULT_CACHE_DAYS = 7
DEFAULT_PENDING_DAYS = 1
GC_THROTTLE_HOURS = 24.0
# Files that survive a completed JSON cleanup. A run directory holding anything
# else still has evidence in it and is never expired.
RETAINED_FILENAMES = frozenset({RUN_MANIFEST, CLEANUP_RECEIPT, "report.md", "final_response.md", PIPELINE_LOCK})
RETAINED_ROOT_DIRS = frozenset({"collections", "snapshots", "research"})


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_started_at(root: Path, manifest: dict[str, Any] | None, receipt: dict[str, Any] | None) -> datetime | None:
    for value in ((manifest or {}).get("created_at_utc"), (receipt or {}).get("updated_at_utc")):
        parsed = _parse_utc(value)
        if parsed is not None:
            return parsed
    try:
        return datetime.fromtimestamp(root.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _read_manifest(root: Path) -> dict[str, Any] | None:
    path = root / RUN_MANIFEST
    if not path.exists():
        return None
    if _is_link(path) or not path.is_file():
        raise CleanupError("owned run manifest is linked or not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError("owned run manifest is unreadable") from exc
    if (not isinstance(value, dict) or value.get("manifest_version") != 1
            or value.get("mode") != "owned_pipeline" or value.get("run_id") != root.name):
        raise CleanupError("owned run manifest identity is invalid")
    return value


def _prunable_tree(root: Path) -> tuple[list[str], list[str]]:
    """Return the retained-only inventory of a run directory, or raise."""
    files, directories = _tree_entries(root)
    for name in sorted(files):
        if PurePosixPath(name).name not in RETAINED_FILENAMES:
            raise CleanupError(f"run directory still holds a non-retained file: {name}")
    for name in sorted(directories):
        parts = PurePosixPath(name).parts
        if len(parts) == 1 and parts[0] in RETAINED_ROOT_DIRS:
            continue
        if len(parts) == 2 and parts[0] == "research":
            validate_path_component(parts[1], label="analysis_id")
            continue
        raise CleanupError(f"run directory holds an unexpected directory: {name}")
    return sorted(files), sorted(directories)


def _delete_run_tree(root: Path, files: list[str], directories: list[str]) -> int:
    removed_bytes = 0
    for name in sorted(files, key=lambda item: (item.count("/"), item), reverse=True):
        path = _member_path(root, name)
        if not path.is_file() or _is_link(path):
            raise CleanupError(f"retained file changed before expiry: {name}")
        removed_bytes += path.stat().st_size
        path.unlink()
    for name in sorted(directories, key=lambda item: (item.count("/"), item), reverse=True):
        path = _member_path(root, name)
        if not path.is_dir() or _is_link(path):
            raise CleanupError(f"retained directory changed before expiry: {name}")
        path.rmdir()
    root.rmdir()
    return removed_bytes


def _drop_lock_file(lock: Path) -> None:
    """Remove a spent cleanup lock after the run directory is gone.

    The lock must stay in place while it is held, so this runs after release and
    only ever touches a regular file that is not a link; a concurrent waiter may
    recreate it, which is harmless because the directory it guards is gone.
    """
    try:
        if lock.exists() and not _is_link(lock) and stat.S_ISREG(lock.lstat().st_mode):
            lock.unlink()
    except OSError:
        return


def _expire_run(root: Path, *, dry_run: bool) -> str:
    lock = root.parent / f".{root.name}.cleanup.lock"
    with _exclusive_lock(lock):
        files, directories = _prunable_tree(root)
        if dry_run:
            return "would_delete"
        _delete_run_tree(root, files, directories)
    _drop_lock_file(lock)
    return "deleted"


def _finish_pending(root: Path, *, dry_run: bool) -> str:
    """Clean the JSON of a run the host never reported as delivered."""
    result = cleanup_owned_run(root, dry_run=dry_run, delivery_unverified=True)
    return str(result.get("cleanup_status", "unknown"))


def sweep_legacy_snapshots(output_root: Path, *, min_age_hours: float = 1.0,
                           dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Sweep snapshot build directories abandoned in the shared layout.

    The shared layout has no cleanup contract, so only the documented scratch
    pattern is matched, links abort the sweep, and an entry is left alone until
    it is older than ``min_age_hours`` in case a live freeze is still writing.
    """
    moment = now or datetime.now(timezone.utc)
    base = output_root / "snapshots"
    report: dict[str, Any] = {"status": "success", "removed": [], "orphan_bytes": 0}
    if not base.exists():
        report["status"] = "absent"
        return report
    if _is_link(base) or not base.is_dir():
        report["status"] = "skipped"
        report["reason"] = "snapshots is not a regular directory"
        return report
    for entry in sorted(base.iterdir()):
        if not ORPHAN_TEMP_DIR.fullmatch(entry.name) or _is_link(entry) or not entry.is_dir():
            continue
        try:
            age_hours = (moment - datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        except OSError:
            continue
        if age_hours < min_age_hours:
            continue
        files, directories = _tree_entries(entry)
        if dry_run:
            report["removed"].append(entry.name)
            continue
        size = 0
        for name in sorted(files, key=lambda item: (item.count("/"), item), reverse=True):
            path = _member_path(entry, name)
            if not path.is_file() or _is_link(path):
                raise CleanupError(f"snapshot scratch file changed before removal: {name}")
            size += path.stat().st_size
            path.unlink()
        for name in sorted(directories, key=lambda item: (item.count("/"), item), reverse=True):
            _member_path(entry, name).rmdir()
        entry.rmdir()
        report["removed"].append(entry.name)
        report["orphan_bytes"] += size
    return report


def prune_cache(cache_dir: Path, *, days: int, dry_run: bool = False,
                now: datetime | None = None) -> dict[str, Any]:
    """Delete provider cache files older than ``days``; links and odd entries are skipped."""
    report: dict[str, Any] = {"status": "success", "files_removed": 0, "bytes_removed": 0, "errors": []}
    if days <= 0:
        report["status"] = "disabled"
        return report
    if not cache_dir.exists():
        report["status"] = "absent"
        return report
    if _is_link(cache_dir) or not cache_dir.is_dir():
        report["status"] = "skipped"
        report["reason"] = "cache directory is not a regular directory"
        return report
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=days)
    stale: list[Path] = []
    directories: list[Path] = []
    for current, dirnames, filenames in os.walk(cache_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            if _is_link(path):
                report["errors"].append(f"linked cache directory skipped: {name}")
                dirnames.remove(name)
                continue
            directories.append(path)
        for name in filenames:
            path = current_path / name
            if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
                report["errors"].append(f"non-regular cache entry skipped: {name}")
                continue
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                    stale.append(path)
            except OSError:
                continue
    for path in stale:
        try:
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            report["files_removed"] += 1
            report["bytes_removed"] += size
        except OSError as exc:
            report["errors"].append(f"{path.name}: {type(exc).__name__}")
    if not dry_run:
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
    return report


def _read_gc_state(output_root: Path) -> dict[str, Any]:
    path = output_root / GC_STATE
    if not path.is_file() or _is_link(path):
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_gc_state(output_root: Path, moment: datetime, report: dict[str, Any]) -> None:
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        _write_json(output_root / GC_STATE, {
            "format": "stock-data-gc-v1",
            "last_run_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "runs_scanned": report.get("runs_scanned", 0),
            "runs_deleted": len(report.get("runs_deleted") or []),
            "orphans_removed": report.get("orphans_removed", 0),
        })
    except (OSError, ValueError):
        return


def gc_runtime(
    output_root: Path, *, retain_days: int = DEFAULT_RETAIN_DAYS,
    cache_days: int = DEFAULT_CACHE_DAYS, pending_days: int = DEFAULT_PENDING_DAYS,
    cache_dir: Path | None = None, dry_run: bool = False,
    throttle_hours: float | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Sweep orphans, finish abandoned runs and expire run directories past retention.

    Called with ``throttle_hours`` from the pipeline so a host without a
    scheduler still garbage-collects, at most once per window.
    """
    moment = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "status": "success", "dry_run": dry_run, "runs_scanned": 0,
        "orphans_removed": 0, "orphans_detected": 0, "orphan_bytes": 0, "pending_cleaned": [],
        "runs_deleted": [], "runs_would_delete": [], "runs_skipped": [], "errors": [],
        "legacy_snapshots": {"status": "absent"}, "cache": {"status": "absent"},
    }
    if throttle_hours:
        state = _read_gc_state(output_root)
        last = _parse_utc(state.get("last_run_utc"))
        # A future timestamp means the clock moved backwards; treat it as stale
        # instead of suppressing collection until real time catches up.
        if last is not None and last <= moment and (moment - last) < timedelta(hours=throttle_hours):
            return {"status": "throttled", "dry_run": dry_run,
                    "last_run_utc": state.get("last_run_utc"),
                    "throttle_hours": throttle_hours}
    runs_root = output_root / "runs"
    if runs_root.exists() and not _is_link(runs_root) and runs_root.is_dir():
        for entry in sorted(runs_root.iterdir()):
            if not RUN_ID_PATTERN.fullmatch(entry.name) or _is_link(entry) or not entry.is_dir():
                continue
            report["runs_scanned"] += 1
            try:
                marker = entry / PIPELINE_LOCK
                if marker.exists():
                    try:
                        pid = int(marker.read_text(encoding="ascii").strip()[4:])
                    except (OSError, UnicodeError, ValueError):
                        report["runs_skipped"].append({"run_id": entry.name, "reason": "unreadable_pipeline_lock"})
                        continue
                    if _pid_is_running(pid):
                        # Never touch a run whose writer is still alive: its scratch
                        # files are in flight and the cleanup lock is not held yet.
                        report["runs_skipped"].append({"run_id": entry.name, "reason": "pipeline_active"})
                        continue
                manifest = _read_manifest(entry)
                receipt_path = entry / CLEANUP_RECEIPT
                receipt = _read_receipt(receipt_path) if receipt_path.is_file() else None
                started = _run_started_at(entry, manifest, receipt)
                age_days = (moment - started).total_seconds() / 86400 if started else 0.0
                orphans = sweep_orphans(entry, dry_run=dry_run)
                orphan_total = len(orphans["orphan_files"]) + len(orphans["orphan_directories"])
                report["orphans_detected"] += orphan_total
                if not dry_run:
                    report["orphans_removed"] += orphan_total
                report["orphan_bytes"] += int(orphans["orphan_bytes"])
                if (manifest and manifest.get("state") == "awaiting_cleanup"
                        and pending_days > 0 and age_days >= pending_days):
                    status = _finish_pending(entry, dry_run=dry_run)
                    report["pending_cleaned"].append({"run_id": entry.name, "cleanup_status": status})
                    receipt_path = entry / CLEANUP_RECEIPT
                    receipt = _read_receipt(receipt_path) if receipt_path.is_file() else receipt
                    if receipt is None or receipt.get("state") != "complete":
                        report["runs_skipped"].append({"run_id": entry.name, "reason": "cleanup_incomplete"})
                        continue
                if retain_days <= 0 or age_days < retain_days:
                    continue
                if receipt is None:
                    report["runs_skipped"].append({"run_id": entry.name, "reason": "no_cleanup_receipt"})
                    continue
                if receipt.get("state") != "complete":
                    report["runs_skipped"].append({"run_id": entry.name, "reason": "cleanup_incomplete"})
                    continue
                outcome = _expire_run(entry, dry_run=dry_run)
                if outcome == "deleted":
                    report["runs_deleted"].append(entry.name)
                else:
                    report["runs_would_delete"].append(entry.name)
            except (CleanupError, OSError, ValueError) as exc:
                report["runs_skipped"].append({"run_id": entry.name, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
    try:
        report["legacy_snapshots"] = sweep_legacy_snapshots(output_root, dry_run=dry_run, now=moment)
    except (CleanupError, OSError) as exc:
        report["legacy_snapshots"] = {"status": "error", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    target_cache = cache_dir if cache_dir is not None else output_root / "yfinance-cache"
    report["cache"] = prune_cache(target_cache, days=cache_days, dry_run=dry_run, now=moment)
    if not dry_run:
        _write_gc_state(output_root, moment, report)
    report["generated_at_utc"] = utc_now()
    return report
