from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_pinned_file(
    path: str | Path,
    expected_sha256: str,
    expected_bytes: int | None = None,
    *,
    restore_text_newlines: bool = False,
) -> bool:
    """Verify exact pinned bytes, optionally undoing text checkout line endings.

    A repair is permitted only when an LF or CRLF candidate matches the existing
    expected hash and length. Content, final blank lines and binary assets are
    never normalized. Already matching files remain untouched.
    """
    path = Path(path)
    actual = sha256_file(path)
    size = path.stat().st_size
    if actual == expected_sha256:
        return expected_bytes is None or size == expected_bytes

    text_suffixes = {".py", ".json", ".toml", ".yaml", ".yml", ".md",
                     ".txt", ".csv", ".html", ".sh"}
    text_names = {"LICENSE", ".gitignore", ".gitattributes"}
    if (not restore_text_newlines or path.is_symlink()
            or (path.suffix.lower() not in text_suffixes and path.name not in text_names)):
        return False

    original = path.read_bytes()
    if b"\0" in original:
        return False
    lf = original.replace(b"\r\n", b"\n")
    for candidate in (lf, lf.replace(b"\n", b"\r\n")):
        if candidate == original:
            continue
        if expected_bytes is not None and len(candidate) != expected_bytes:
            continue
        if sha256_bytes(candidate) != expected_sha256:
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pinned-text-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(candidate)
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True
    return False


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def short_hash(value: Any, length: int = 8) -> str:
    return canonical_hash(value)[:length]
