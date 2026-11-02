"""Confine runtime artifacts; never take filesystem instructions from evidence."""

from __future__ import annotations

import re
from pathlib import Path


def child(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("unsafe artifact component")
    path = root.resolve() / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("artifact escapes runtime root")
    return path
