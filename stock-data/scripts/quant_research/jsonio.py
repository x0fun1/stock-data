"""Single definition of how this runtime serializes JSON.

Snapshot manifests, frozen stage results and cleanup receipts all hash JSON
bytes, so the exact serialization (key order, separators, non-ASCII handling,
NaN policy and the trailing newline) is part of the on-disk contract. Keeping it
in one place means a change here is a visible contract change instead of three
quietly drifting copies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Stable, hashable bytes: sorted keys, no whitespace, UTF-8, NaN rejected."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def json_payload(value: Any) -> str:
    """Readable on-disk form: two-space indent, UTF-8, NaN rejected, trailing newline."""
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    """Write a JSON artifact, optionally refusing to replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json_payload(value))
        return
    path.write_text(json_payload(value), encoding="utf-8")
