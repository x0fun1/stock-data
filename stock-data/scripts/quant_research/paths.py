"""Validate artifact names and confine output destinations before writing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def validate_path_component(value: Any, *, label: str) -> str:
    """Accept one portable filename component, never an absolute or relative path."""
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or re.fullmatch(r"[A-Za-z0-9._-]+", value, flags=re.ASCII) is None
    ):
        raise ValueError(f"{label} must be a non-empty safe filename component (letters, digits, '.', '_', '-')")
    return value


def safe_output_destination(output_root: Path, name: str) -> Path:
    """Reject unsafe names and pre-existing links that resolve outside output_root.

    Return the absolute lexical child after checking its resolved destination.
    Existing in-root entries remain subject to the caller's no-overwrite check.
    The output root may itself be a user-selected symlink; its resolved directory
    is the boundary. This check assumes it is not concurrently replaced.
    """
    validate_path_component(name, label="output name")
    root = output_root.resolve()
    destination = root / name
    if destination.resolve().parent != root:
        raise ValueError("output destination resolves outside output_root")
    return destination
