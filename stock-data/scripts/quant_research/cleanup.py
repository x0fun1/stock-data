"""Owner-scoped JSON cleanup after a validated research response."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import RESEARCHERS, utc_now
from .delivery import validate_final_response
from .paths import safe_output_destination, validate_path_component
from .snapshot import DOMAINS

RUN_MANIFEST = "run_manifest.json"
CLEANUP_RECEIPT = "cleanup_receipt.txt"
PIPELINE_LOCK = ".pipeline.lock"
ANALYSIS_JSON = (
    "quant_result.json", "quant_result.freeze.json", "news_result.json",
    "news_result.freeze.json", "report.json", "agent_summary.json",
)
ANALYSIS_MARKDOWN = ("report.md", "final_response.md")


class CleanupError(ValueError):
    """A cleanup request could not be proven to own every deletion target."""


def _is_link(path: Path) -> bool:
    junction_check = getattr(os.path, "isjunction", None)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        attributes = 0
    return (path.is_symlink() or bool(junction_check and junction_check(path))
            or bool(attributes & reparse_flag))


def _real_directory(path: Path, label: str) -> Path:
    if _is_link(path) or not path.is_dir():
        raise CleanupError(f"{label} must be an existing directory, not a link")
    return path.resolve(strict=True)


def _write_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        return
    _atomic_write_text(path, payload)


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_run_workspace(output_root: Path) -> tuple[Path, dict[str, Any]]:
    """Create an isolated directory whose generated artifacts can be registered."""
    runs_root = output_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    if _is_link(runs_root):
        raise CleanupError("runtime/runs must be a regular directory, not a link")
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + secrets.token_hex(4)
    run_dir = safe_output_destination(runs_root, run_id)
    run_dir.mkdir(parents=False, exist_ok=False)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "mode": "owned_pipeline",
        "run_id": run_id,
        "state": "running",
        "created_at_utc": utc_now(),
        "collection_name": None,
        "snapshot_id": None,
        "analysis_id": None,
    }
    _write_json(run_dir / RUN_MANIFEST, manifest, exclusive=True)
    with (run_dir / PIPELINE_LOCK).open("x", encoding="ascii") as handle:
        handle.write(f"pid={os.getpid()}\n")
    return run_dir, manifest


def update_run_workspace(run_dir: Path, **updates: Any) -> dict[str, Any]:
    root = _real_directory(run_dir, "run directory")
    manifest_path = root / RUN_MANIFEST
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise CleanupError("owned run manifest is missing or linked")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("manifest_version") != 1
            or manifest.get("run_id") != root.name or manifest.get("mode") != "owned_pipeline"):
        raise CleanupError("owned run manifest identity is invalid")
    manifest.update(updates)
    _write_json(manifest_path, manifest)
    return manifest


def mark_run_failed(run_dir: Path, error: BaseException) -> None:
    """Keep a failed run's artifacts for debugging and mark explicit ownership."""
    try:
        update_run_workspace(run_dir, state="failed", failure_type=type(error).__name__)
    except (OSError, ValueError, json.JSONDecodeError):
        # Preserve the original pipeline error; a failed manifest update only
        # makes later automatic cleanup unavailable.
        return
    try:
        release_pipeline_lock(run_dir)
    except (OSError, ValueError):
        return


def release_pipeline_lock(run_dir: Path) -> None:
    root = _real_directory(run_dir, "run directory")
    marker = root / PIPELINE_LOCK
    if _is_link(marker):
        raise CleanupError("pipeline lock marker was replaced by a link")
    marker.unlink(missing_ok=True)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        raise CleanupError("pipeline lock marker contains an invalid process id")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        # Windows os.kill(pid, 0) terminates the process instead of probing it.
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            return kernel32.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: exited
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _clear_stale_pipeline_marker(path: Path, pid: int) -> None:
    if (_is_link(path) or not path.is_file()
            or path.read_text(encoding="ascii").strip() != f"pid={pid}"
            or _pid_is_running(pid)):
        raise CleanupError("pipeline lock marker changed before cleanup")
    path.unlink()


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link(path):
        raise CleanupError("cleanup lock is a link; refusing to continue")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        if _is_link(path) or not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CleanupError("cleanup lock is linked or not a regular file")
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CleanupError("another cleanup is active or its lock is unavailable") from exc
        locked = True
        if os.fstat(descriptor).st_size > 64:
            raise CleanupError("cleanup lock file has unexpected contents")
        os.lseek(descriptor, 0, os.SEEK_SET)
        previous = os.read(descriptor, 64)
        if previous:
            try:
                if not previous.startswith(b"pid=") or not previous.endswith(b"\n"):
                    raise ValueError("invalid marker")
                if int(previous[4:-1]) <= 0:
                    raise ValueError("invalid process id")
            except ValueError as exc:
                raise CleanupError("cleanup lock file has unexpected contents") from exc
        # Keep the lock file: unlinking it lets another process lock a new file
        # while a waiter still holds a handle to the original one.
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_entries(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            if _is_link(path) or not path.is_dir():
                raise CleanupError(f"linked or non-directory path in cleanup tree: {path.name}")
            directories.add(path.relative_to(root).as_posix())
        for name in filenames:
            path = current_path / name
            if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
                raise CleanupError(f"linked or non-regular file in cleanup tree: {path.name}")
            files.add(path.relative_to(root).as_posix())
    return files, directories


def _analysis_targets(analysis_id: str) -> set[str]:
    validate_path_component(analysis_id, label="analysis_id")
    return {f"research/{analysis_id}/researchers/{name}.json" for name in RESEARCHERS} | {
        f"research/{analysis_id}/{name}" for name in ANALYSIS_JSON
    }


def _snapshot_targets(snapshot_id: str) -> set[str]:
    validate_path_component(snapshot_id, label="snapshot_id")
    base = f"snapshots/{snapshot_id}"
    names = {f"{base}/manifest.json"}
    for domain in DOMAINS:
        names.add(f"{base}/{domain}.json")
        names.add(f"{base}/raw/{domain}.json")
    return names


def _allowed_directories(analysis_id: str | None, snapshot_id: str | None, *, full_run: bool) -> set[str]:
    names = {"collections", "snapshots", "research"} if full_run else set()
    if snapshot_id:
        names |= {"snapshots/" + snapshot_id, "snapshots/" + snapshot_id + "/raw"}
    if analysis_id:
        names |= {"research/" + analysis_id, "research/" + analysis_id + "/researchers"}
    return names


def _allowed_markdown(analysis_id: str | None, *, full_run: bool) -> set[str]:
    if not analysis_id:
        return set()
    base = f"research/{analysis_id}" if full_run else ""
    return {f"{base}/{name}" if base else name for name in ANALYSIS_MARKDOWN}


def _expected_full_targets(manifest: dict[str, Any]) -> set[str]:
    targets = {RUN_MANIFEST}
    collection_name = manifest.get("collection_name")
    if collection_name is not None:
        validate_path_component(collection_name, label="collection filename")
        if not collection_name.startswith("collection_") or not collection_name.endswith(".json"):
            raise CleanupError("owned collection filename is invalid")
        targets.add(f"collections/{collection_name}")
    snapshot_id = manifest.get("snapshot_id")
    if snapshot_id:
        targets |= _snapshot_targets(snapshot_id)
    analysis_id = manifest.get("analysis_id")
    if analysis_id:
        targets |= _analysis_targets(analysis_id)
    return targets


def _receipt_data(
    *, mode: str, run_id: str, analysis_id: str | None, snapshot_id: str | None,
    collection_name: str | None, state: str, response_hash: str | None,
    report_hash: str | None, final_response_hash: str | None,
    analysis_status: str | None, reporting_status: str | None,
    targets: list[tuple[str, str, int]], removed_file_count: int = 0,
    removed_bytes: int = 0, errors: list[str] | None = None,
) -> str:
    lines = [
        "format=stock-data-cleanup-v1", f"mode={mode}", f"run_id={run_id}",
        f"analysis_id={analysis_id or ''}", f"snapshot_id={snapshot_id or ''}",
        f"collection_name={collection_name or ''}", f"state={state}",
        f"response_sha256={response_hash or ''}", f"report_sha256={report_hash or ''}",
        f"final_response_sha256={final_response_hash or ''}",
        f"analysis_status={analysis_status or ''}", f"reporting_status={reporting_status or ''}",
        f"target_file_count={len(targets)}",
        f"target_json_bytes={sum(size for _, _, size in targets)}",
        f"removed_file_count={removed_file_count}", f"removed_json_bytes={removed_bytes}",
        f"updated_at_utc={utc_now()}",
    ]
    lines.extend(f"target={name}\t{digest}\t{size}" for name, digest, size in targets)
    lines.extend(f"error={item.replace(chr(10), ' ')[:200]}" for item in errors or [])
    return "\n".join(lines) + "\n"


def _read_receipt(path: Path) -> dict[str, Any]:
    if _is_link(path) or not path.is_file():
        raise CleanupError("cleanup receipt is missing or linked")
    values: dict[str, str] = {}
    targets: list[tuple[str, str, int]] = []
    errors: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("target="):
            fields = line[len("target="):].split("\t")
            if len(fields) != 3:
                raise CleanupError("cleanup receipt contains a malformed target")
            try:
                size = int(fields[2])
            except ValueError as exc:
                raise CleanupError("cleanup receipt contains an invalid target size") from exc
            if size < 0:
                raise CleanupError("cleanup receipt contains an invalid target size")
            targets.append((fields[0], fields[1], size))
        elif line.startswith("error="):
            errors.append(line[len("error="):])
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("format") != "stock-data-cleanup-v1":
        raise CleanupError("cleanup receipt format or target list is invalid")
    if values.get("state") not in {"in_progress", "partial", "complete"}:
        raise CleanupError("cleanup receipt state is invalid")
    if len({name for name, _, _ in targets}) != len(targets):
        raise CleanupError("cleanup receipt contains duplicate targets")
    for name, digest, _ in targets:
        _validate_relative_target(name)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise CleanupError("cleanup receipt contains an invalid file digest")
    for key in ("response_sha256", "report_sha256", "final_response_sha256"):
        digest = values.get(key, "")
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise CleanupError(f"cleanup receipt contains an invalid {key}")
    try:
        target_count = int(values.get("target_file_count", "-1"))
        target_bytes = int(values.get("target_json_bytes", "-1"))
        removed_count = int(values.get("removed_file_count", "-1"))
        removed_bytes = int(values.get("removed_json_bytes", "-1"))
    except ValueError as exc:
        raise CleanupError("cleanup receipt contains invalid file/byte totals") from exc
    if (target_count != len(targets) or target_bytes != sum(size for _, _, size in targets)
            or removed_count < 0 or removed_bytes < 0
            or removed_count > target_count or removed_bytes > target_bytes):
        raise CleanupError("cleanup receipt file/byte totals do not match its inventory")
    if values.get("state") == "complete" and (removed_count != target_count or removed_bytes != target_bytes):
        raise CleanupError("complete cleanup receipt does not account for its full inventory")
    return {**values, "targets": targets, "errors": errors}


def _validate_relative_target(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (not value or relative.is_absolute() or "\\" in value or ":" in value
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.suffix.lower() != ".json"):
        raise CleanupError("cleanup target must be a safe relative JSON filename")
    return relative


def _target_path(root: Path, relative: str) -> Path:
    parts = _validate_relative_target(relative).parts
    candidate = root
    for part in parts:
        candidate = candidate / part
        if (candidate.exists() or candidate.is_symlink()) and _is_link(candidate):
            raise CleanupError(f"refusing to follow linked cleanup target: {relative}")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CleanupError(f"cleanup target escapes its owned root: {relative}") from exc
    return candidate


def _validate_tree(
    root: Path, *, expected_files: set[str], expected_dirs: set[str],
    allowed_markdown: set[str], allow_missing: bool,
) -> set[str]:
    files, directories = _tree_entries(root)
    allowed_files = expected_files | allowed_markdown
    unknown_files = files - allowed_files
    unknown_dirs = directories - expected_dirs
    if unknown_files or unknown_dirs:
        names = sorted(unknown_files | unknown_dirs)
        raise CleanupError("unknown files or directories prevent cleanup: " + ", ".join(names[:8]))
    if not allow_missing:
        missing = expected_files - files
        if missing:
            raise CleanupError("registered cleanup files are missing: " + ", ".join(sorted(missing)[:8]))
    return files


def _verify_targets(root: Path, targets: list[tuple[str, str, int]]) -> tuple[int, list[str]]:
    size = 0
    existing: list[str] = []
    for relative, expected_hash, expected_size in targets:
        path = _target_path(root, relative)
        if not path.exists():
            continue
        if not path.is_file():
            raise CleanupError(f"cleanup target is not a regular file: {relative}")
        if _sha256(path) != expected_hash or path.stat().st_size != expected_size:
            raise CleanupError(f"cleanup target changed after inventory: {relative}")
        size += path.stat().st_size
        existing.append(relative)
    return size, existing


def _remove_empty_directories(root: Path, names: list[str]) -> list[str]:
    removed: list[str] = []
    for relative in sorted(names, key=lambda item: (item.count("/"), item), reverse=True):
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.exists():
            continue
        if _is_link(path) or not path.is_dir():
            raise CleanupError(f"refusing to remove changed directory: {relative}")
        path.rmdir()
        removed.append(relative)
    return removed


def _execute_cleanup(
    root: Path, receipt_path: Path, metadata: dict[str, Any],
    targets: list[tuple[str, str, int]], *, dry_run: bool,
) -> dict[str, Any]:
    size, existing = _verify_targets(root, targets)
    target_bytes = sum(file_size for _, _, file_size in targets)
    if dry_run:
        return {"cleanup_status": "dry_run", "mode": metadata["mode"],
                "json_file_count": len(existing), "json_bytes": size,
                "files": existing, "receipt_file": str(receipt_path)}

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    if not receipt_path.exists():
        _atomic_write_text(receipt_path, _format_receipt(metadata, state="in_progress", targets=targets))

    removed: list[str] = []
    removed_bytes = 0
    errors: list[str] = []
    # Keep the run manifest until every data JSON has been removed. It is the
    # last target, so interrupted cleanup can still be resumed safely.
    ordered = sorted(targets, key=lambda item: item[0] == RUN_MANIFEST)
    for relative, expected_hash, expected_size in ordered:
        path = _target_path(root, relative)
        if not path.exists():
            continue
        try:
            if (not path.is_file() or _sha256(path) != expected_hash
                    or path.stat().st_size != expected_size):
                raise CleanupError(f"cleanup target changed after inventory: {relative}")
            file_bytes = path.stat().st_size
            path.unlink()
            removed.append(relative)
            removed_bytes += file_bytes
        except OSError as exc:
            errors.append(f"{relative}: {type(exc).__name__}")
            break
        except CleanupError as exc:
            errors.append(str(exc))
            break

    if not errors:
        dirs = list(metadata.get("empty_directories", []))
        try:
            _remove_empty_directories(root, dirs)
        except (OSError, CleanupError) as exc:
            errors.append(f"directory_cleanup: {type(exc).__name__}: {str(exc)[:160]}")

    state = "complete" if not errors else "partial"
    cleanup_status = "complete" if not errors else "partial_cleanup"
    remaining = [relative for relative, _, _ in targets if _target_path(root, relative).exists()]
    receipt_metadata = dict(metadata)
    receipt_metadata["removed_file_count"] = len(targets) - len(remaining)
    receipt_metadata["removed_bytes"] = target_bytes - sum(
        file_size for relative, _, file_size in targets if relative in remaining
    )
    _atomic_write_text(receipt_path, _format_receipt(receipt_metadata, state=state, targets=targets, errors=errors))
    retained_files = [str(root / relative) for relative in metadata.get("retained_paths", [])
                      if (root / relative).is_file()]
    return {"cleanup_status": cleanup_status, "mode": metadata["mode"],
            "analysis_id": metadata.get("analysis_id"), "run_id": metadata.get("run_id"),
            "analysis_status": metadata.get("analysis_status"),
            "reporting_status": metadata.get("reporting_status"),
            "json_file_count": len(removed), "json_bytes": removed_bytes,
            "planned_json_bytes": target_bytes,
            "remaining_json_files": remaining, "errors": errors,
            "retained_files": retained_files,
            "receipt_file": str(receipt_path)}


def _format_receipt(
    metadata: dict[str, Any], *, state: str,
    targets: list[tuple[str, str, int]], errors: list[str] | None = None,
) -> str:
    return _receipt_data(
        mode=str(metadata.get("mode", "")), run_id=str(metadata.get("run_id", "")),
        analysis_id=metadata.get("analysis_id"), snapshot_id=metadata.get("snapshot_id"),
        collection_name=metadata.get("collection_name"), state=state,
        response_hash=metadata.get("response_hash"), report_hash=metadata.get("report_hash"),
        final_response_hash=metadata.get("final_response_hash"),
        analysis_status=metadata.get("analysis_status"),
        reporting_status=metadata.get("reporting_status"), targets=targets,
        removed_file_count=int(metadata.get("removed_file_count", 0)),
        removed_bytes=int(metadata.get("removed_bytes", 0)), errors=errors,
    )


def _read_analysis_identity(analysis_dir: Path) -> tuple[Path, str]:
    analysis = _real_directory(analysis_dir, "analysis directory")
    validate_path_component(analysis.name, label="analysis directory name")
    return analysis, analysis.name


def _retained_hashes(analysis_dir: Path, *, required: bool) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, key in (("report.md", "report_hash"), ("final_response.md", "final_response_hash")):
        path = analysis_dir / name
        if not path.exists():
            if required:
                raise CleanupError(f"required retained artifact is missing: {name}")
            continue
        if _is_link(path) or not path.is_file():
            raise CleanupError(f"retained artifact is linked or not a file: {name}")
        hashes[key] = _sha256(path)
    return hashes


def _verify_retained_hashes(analysis_dir: Path, receipt: dict[str, Any]) -> None:
    for name, key in (("report.md", "report_sha256"), ("final_response.md", "final_response_sha256")):
        expected_hash = receipt.get(key)
        if not expected_hash:
            continue
        path = analysis_dir / name
        if _is_link(path) or not path.is_file() or _sha256(path) != expected_hash:
            raise CleanupError(f"retained artifact changed after cleanup inventory: {name}")


def cleanup_analysis(
    analysis_dir: Path, *, response_file: Path | None = None,
    dry_run: bool = False, force_incomplete: bool = False,
) -> dict[str, Any]:
    """Clean only explicitly named analysis JSON; never inspect or delete its snapshot."""
    analysis, analysis_id = _read_analysis_identity(analysis_dir)
    if analysis.parent.name == "research":
        run_root = analysis.parent.parent
        run_manifest = run_root / RUN_MANIFEST
        run_receipt = run_root / CLEANUP_RECEIPT
        if _is_link(run_manifest):
            raise CleanupError("owned run manifest is linked; use the run directory after repairing it")
        if run_manifest.exists():
            try:
                run_data = json.loads(run_manifest.read_text(encoding="utf-8"))
                run_mode = run_data.get("mode") if isinstance(run_data, dict) else None
            except (OSError, json.JSONDecodeError) as exc:
                raise CleanupError("owned run manifest is unreadable; analysis-only cleanup is blocked") from exc
            if run_mode == "owned_pipeline":
                raise CleanupError("analysis belongs to an owned pipeline; use --run-dir to preserve run inventory")
        if _is_link(run_receipt):
            raise CleanupError("owned run cleanup receipt is linked; analysis-only cleanup is blocked")
        if run_receipt.exists():
            if not run_receipt.is_file():
                raise CleanupError("owned run cleanup receipt is not a file")
            try:
                run_mode = _read_receipt(run_receipt).get("mode")
            except (OSError, CleanupError) as exc:
                raise CleanupError("owned run cleanup receipt is unreadable; analysis-only cleanup is blocked") from exc
            if run_mode == "run":
                raise CleanupError("analysis belongs to an owned pipeline; use --run-dir to preserve run inventory")
    lock = analysis.parent / f".{analysis_id}.cleanup.lock"
    with _exclusive_lock(lock):
        receipt_path = analysis / CLEANUP_RECEIPT
        existing_receipt = _read_receipt(receipt_path) if receipt_path.exists() else None
        expected = set(ANALYSIS_JSON) | {f"researchers/{name}.json" for name in RESEARCHERS}
        if existing_receipt:
            if existing_receipt.get("mode") != "analysis" or existing_receipt.get("analysis_id") != analysis_id:
                raise CleanupError("existing cleanup receipt belongs to a different analysis")
            if existing_receipt.get("state") == "complete":
                files, directories = _tree_entries(analysis)
                allowed = set(ANALYSIS_MARKDOWN) | {CLEANUP_RECEIPT}
                if files - allowed or directories:
                    raise CleanupError("JSON or unexpected files appeared after cleanup")
                _verify_retained_hashes(analysis, existing_receipt)
                if response_file and existing_receipt.get("response_sha256") and _sha256(response_file) != existing_receipt["response_sha256"]:
                    raise CleanupError("prepared response differs from the response previously validated")
                return {"cleanup_status": "already_cleaned", "mode": "analysis",
                        "analysis_id": analysis_id,
                        "analysis_status": existing_receipt.get("analysis_status") or None,
                        "reporting_status": existing_receipt.get("reporting_status") or None,
                        "json_file_count": int(existing_receipt.get("removed_file_count", "0")),
                        "json_bytes": int(existing_receipt.get("removed_json_bytes", "0")),
                        "retained_files": [str(analysis / name) for name in ANALYSIS_MARKDOWN
                                           if (analysis / name).is_file()],
                        "errors": [],
                        "receipt_file": str(receipt_path)}

            targets = existing_receipt["targets"]
            if not {name for name, _, _ in targets} <= expected:
                raise CleanupError("analysis cleanup receipt contains an unowned target")
            files, directories = _tree_entries(analysis)
            allowed = expected | set(ANALYSIS_MARKDOWN) | {CLEANUP_RECEIPT}
            if files - allowed or directories - {"researchers"}:
                raise CleanupError("unknown files or directories prevent cleanup")
            registered = {name for name, _, _ in targets}
            if (files & expected) - registered:
                raise CleanupError("unregistered analysis JSON appeared after cleanup inventory")
            _verify_retained_hashes(analysis, existing_receipt)
            if response_file and existing_receipt.get("response_sha256") and _sha256(response_file) != existing_receipt["response_sha256"]:
                raise CleanupError("prepared response differs from the response previously validated")
            metadata = {key: existing_receipt.get(key, "") or None
                        for key in ("mode", "run_id", "analysis_id", "snapshot_id", "collection_name")}
            metadata.update({
                "response_hash": existing_receipt.get("response_sha256") or None,
                "report_hash": existing_receipt.get("report_sha256") or None,
                "final_response_hash": existing_receipt.get("final_response_sha256") or None,
                "analysis_status": existing_receipt.get("analysis_status") or None,
                "reporting_status": existing_receipt.get("reporting_status") or None,
                "removed_file_count": int(existing_receipt.get("removed_file_count", "0")),
                "removed_bytes": int(existing_receipt.get("removed_json_bytes", "0")),
                "empty_directories": ["researchers"],
                "retained_paths": list(ANALYSIS_MARKDOWN),
            })
            return _execute_cleanup(analysis, receipt_path, metadata, targets, dry_run=dry_run)

        actual = _validate_tree(analysis, expected_files=expected, expected_dirs={"researchers"},
                                allowed_markdown=set(ANALYSIS_MARKDOWN), allow_missing=force_incomplete)
        validation = None
        response_hash = None
        if not force_incomplete:
            if response_file is None and not dry_run:
                raise CleanupError("response_file is required unless --force-incomplete is explicit")
            if response_file is not None:
                validation = validate_final_response(analysis, response_file)
                if validation.get("analysis_id") != analysis_id:
                    raise CleanupError("prepared response validation returned a different analysis identity")
                response_hash = _sha256(response_file)
        retained = _retained_hashes(analysis, required=not force_incomplete)
        targets = [(name, _sha256(analysis / name), (analysis / name).stat().st_size)
                   for name in sorted(expected & actual)]
        metadata = {"mode": "analysis", "run_id": "", "analysis_id": analysis_id,
                    "snapshot_id": "", "collection_name": "", "response_hash": response_hash,
                    "report_hash": retained.get("report_hash"),
                    "final_response_hash": retained.get("final_response_hash"),
                    "analysis_status": validation.get("analysis_status") if validation else None,
                    "reporting_status": validation.get("reporting_status") if validation else None,
                    "empty_directories": ["researchers"], "retained_paths": list(ANALYSIS_MARKDOWN)}
        result = _execute_cleanup(analysis, receipt_path, metadata, targets, dry_run=dry_run)
        if validation and result.get("cleanup_status") == "complete":
            result.update({key: validation.get(key) for key in
                           ("ticker", "horizon", "asof_timestamp", "prob_up")})
        return result


def cleanup_owned_run(
    run_dir: Path, *, analysis_dir: Path | None = None, response_file: Path | None = None,
    dry_run: bool = False, force_incomplete: bool = False,
) -> dict[str, Any]:
    """Validate and clean only JSON created by an isolated pipeline run."""
    root = _real_directory(run_dir, "run directory")
    lock = root.parent / f".{root.name}.cleanup.lock"
    with _exclusive_lock(lock):
        pipeline_marker = root / PIPELINE_LOCK
        stale_pipeline_marker = False
        pipeline_pid = None
        if _is_link(pipeline_marker):
            raise CleanupError("pipeline lock marker was replaced by a link")
        if pipeline_marker.exists():
            try:
                marker_text = pipeline_marker.read_text(encoding="ascii").strip()
                if not marker_text.startswith("pid="):
                    raise ValueError("missing pid")
                pipeline_pid = int(marker_text[4:])
            except (OSError, UnicodeError, ValueError) as exc:
                raise CleanupError("pipeline lock marker is unreadable; cleanup is blocked") from exc
            if _pid_is_running(pipeline_pid):
                raise CleanupError("pipeline is still active; cleanup is blocked")
            if not force_incomplete:
                raise CleanupError("interrupted pipeline requires --force-incomplete to clear its stale lock")
            stale_pipeline_marker = True
        manifest_path = root / RUN_MANIFEST
        receipt_path = root / CLEANUP_RECEIPT
        receipt: dict[str, Any] | None = None
        if manifest_path.is_file() and not _is_link(manifest_path):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("manifest_version") != 1
                    or manifest.get("mode") != "owned_pipeline" or manifest.get("run_id") != root.name):
                raise CleanupError("owned run manifest identity is invalid")
        else:
            if not receipt_path.is_file():
                raise CleanupError("owned run manifest is missing and no cleanup receipt can resume this run")
            receipt = _read_receipt(receipt_path)
            if receipt.get("mode") != "run" or receipt.get("run_id") != root.name:
                raise CleanupError("cleanup receipt does not identify this run")
            manifest = {"run_id": root.name, "mode": "owned_pipeline", "state": "cleanup_in_progress",
                        "analysis_id": receipt.get("analysis_id") or None,
                        "snapshot_id": receipt.get("snapshot_id") or None,
                        "collection_name": receipt.get("collection_name") or None}
        analysis_id = manifest.get("analysis_id")
        snapshot_id = manifest.get("snapshot_id")
        collection_name = manifest.get("collection_name")
        for value, label in ((analysis_id, "analysis_id"), (snapshot_id, "snapshot_id")):
            if value:
                validate_path_component(value, label=label)
        if analysis_dir is not None:
            if not analysis_id:
                raise CleanupError("run has no registered analysis directory")
            provided_analysis = _real_directory(analysis_dir, "analysis directory")
            research_root = root / "research"
            if _is_link(research_root):
                raise CleanupError("research directory is linked")
            expected_analysis = research_root / analysis_id
            if provided_analysis != expected_analysis.resolve(strict=True):
                raise CleanupError("analysis directory does not match this owned run")
        if receipt is None and receipt_path.exists():
            receipt = _read_receipt(receipt_path)
        if receipt:
            if receipt.get("mode") != "run" or receipt.get("run_id") != root.name:
                raise CleanupError("cleanup receipt belongs to a different run")
            for key, value in (("analysis_id", analysis_id), ("snapshot_id", snapshot_id),
                               ("collection_name", collection_name)):
                if (receipt.get(key) or "") != (value or ""):
                    raise CleanupError("cleanup receipt does not match the registered run ownership")
            if receipt.get("state") == "complete":
                files, directories = _tree_entries(root)
                allowed_files = _allowed_markdown(analysis_id, full_run=True) | {CLEANUP_RECEIPT}
                if stale_pipeline_marker:
                    allowed_files.add(PIPELINE_LOCK)
                if (files - allowed_files
                        or directories - _allowed_directories(analysis_id, snapshot_id, full_run=True)):
                    raise CleanupError("JSON or unexpected files appeared after cleanup")
                if analysis_id:
                    _verify_retained_hashes(root / "research" / analysis_id, receipt)
                if response_file and receipt.get("response_sha256") and _sha256(response_file) != receipt["response_sha256"]:
                    raise CleanupError("prepared response differs from the response previously validated")
                if stale_pipeline_marker and not dry_run:
                    _clear_stale_pipeline_marker(pipeline_marker, pipeline_pid)
                return {"cleanup_status": "already_cleaned", "mode": "run",
                        "run_id": root.name, "analysis_id": analysis_id,
                        "analysis_status": receipt.get("analysis_status") or None,
                        "reporting_status": receipt.get("reporting_status") or None,
                        "json_file_count": int(receipt.get("removed_file_count", "0")),
                        "json_bytes": int(receipt.get("removed_json_bytes", "0")),
                        "retained_files": [str(root / name) for name in _allowed_markdown(analysis_id, full_run=True)
                                           if (root / name).is_file()],
                        "errors": [],
                        "receipt_file": str(receipt_path)}

        state = manifest.get("state")
        resuming = receipt is not None
        validation = None
        force_mode = bool(force_incomplete and state in {"failed", "running"})
        if not resuming and state != "awaiting_cleanup" and not force_mode:
            raise CleanupError("run is not complete; failed/incomplete runs require --force-incomplete")
        if not resuming and not force_mode and response_file is None and not dry_run:
            raise CleanupError("response_file is required before cleanup")
        if not resuming and not force_mode and response_file is not None:
            if not analysis_id or not snapshot_id:
                raise CleanupError("completed run has no registered analysis/snapshot identity")
            analysis_dir = root / "research" / analysis_id
            validation = validate_final_response(analysis_dir, response_file)
            if validation.get("analysis_id") != analysis_id or validation.get("snapshot_id") != snapshot_id:
                raise CleanupError("prepared response identity does not match the owned run")
            response_hash = _sha256(response_file)
        elif resuming:
            response_hash = receipt.get("response_sha256") or None
            if response_file is not None and response_hash and _sha256(response_file) != response_hash:
                raise CleanupError("prepared response differs from the response previously validated")
        else:
            response_hash = None

        expected = _expected_full_targets(manifest)
        allowed_dirs = _allowed_directories(analysis_id, snapshot_id, full_run=True)
        allowed_markdown = _allowed_markdown(analysis_id, full_run=True)
        if receipt:
            targets = receipt["targets"]
            if not {name for name, _, _ in targets} <= expected:
                raise CleanupError("cleanup receipt contains an unowned target")
            files, directories = _tree_entries(root)
            allowed_run_files = allowed_markdown | {CLEANUP_RECEIPT}
            if stale_pipeline_marker:
                allowed_run_files.add(PIPELINE_LOCK)
            if files - expected - allowed_run_files:
                raise CleanupError("unknown files prevent resumed cleanup")
            if directories - allowed_dirs:
                raise CleanupError("unknown directories prevent resumed cleanup")
            registered = {name for name, _, _ in targets}
            if (files & expected) - registered:
                raise CleanupError("unregistered run JSON appeared after cleanup inventory")
            if analysis_id:
                _verify_retained_hashes(root / "research" / analysis_id, receipt)
        else:
            initial_allowed_files = allowed_markdown | ({PIPELINE_LOCK} if stale_pipeline_marker else set())
            actual = _validate_tree(root, expected_files=expected,
                                    expected_dirs=allowed_dirs,
                                    allowed_markdown=initial_allowed_files,
                                    allow_missing=force_mode)
            if not force_mode:
                if not analysis_id:
                    raise CleanupError("completed run has no registered analysis directory")
                _retained_hashes(root / "research" / analysis_id, required=True)
            targets = [(name, _sha256(root / name), (root / name).stat().st_size)
                       for name in sorted(expected & actual)]

        retained = {}
        retained_paths: list[str] = []
        if analysis_id:
            analysis_dir = root / "research" / analysis_id
            retained = _retained_hashes(analysis_dir, required=not force_mode)
            retained_paths = [f"research/{analysis_id}/{name}" for name in ANALYSIS_MARKDOWN
                              if (analysis_dir / name).is_file()]
        empty_directories = list(allowed_dirs)
        if analysis_id:
            empty_directories = [name for name in empty_directories
                                 if name not in {"research", f"research/{analysis_id}"}]
        metadata = {"mode": "run", "run_id": root.name, "analysis_id": analysis_id or "",
                    "snapshot_id": snapshot_id or "", "collection_name": collection_name or "",
                    "response_hash": response_hash or "",
                    "report_hash": retained.get("report_hash"),
                    "final_response_hash": retained.get("final_response_hash"),
                    "analysis_status": validation.get("analysis_status") if validation else (receipt.get("analysis_status") if receipt else None),
                    "reporting_status": validation.get("reporting_status") if validation else (receipt.get("reporting_status") if receipt else None),
                    "empty_directories": empty_directories,
                    "retained_paths": retained_paths}
        if receipt:
            metadata["removed_file_count"] = int(receipt.get("removed_file_count", "0"))
            metadata["removed_bytes"] = int(receipt.get("removed_json_bytes", "0"))
        if stale_pipeline_marker and not dry_run:
            _clear_stale_pipeline_marker(pipeline_marker, pipeline_pid)
        result = _execute_cleanup(root, receipt_path, metadata, targets, dry_run=dry_run)
        if validation and result.get("cleanup_status") == "complete":
            result.update({key: validation.get(key) for key in
                           ("ticker", "horizon", "asof_timestamp", "prob_up")})
        return result
