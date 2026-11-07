#!/usr/bin/env python
"""Check that the Git index contains only the published stock-research source."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from stock_research.common.package_files import ASSET_MANIFEST, repository_files


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=input,
        check=True, capture_output=True,
    ).stdout


def check_repository(root: Path) -> dict:
    """Inspect indexed paths and sizes, including files added with ``git add -f``."""
    root = root.resolve()
    manifest = json.loads(git(root, 'show', ':' + ASSET_MANIFEST))
    allowed = repository_files(root, manifest=manifest)
    indexed = {
        name.decode("utf-8", errors="surrogateescape")
        for name in git(root, "ls-files", "--cached", "-z").split(b"\0") if name
    }
    entries = []
    unsupported = set()
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        if stage != b"0":
            raise ValueError("Git index has unresolved merge entries")
        if mode not in {b'100644', b'100755'}:
            unsupported.add(name.decode('utf-8', errors='surrogateescape'))
        entries.append(oid)

    sizes = {}
    if entries:
        response = git(
            root, "cat-file", "--batch-check=%(objectname) %(objectsize)",
            input=b"\n".join(sorted(set(entries))) + b"\n",
        )
        for line in response.splitlines():
            oid, size = line.split()
            sizes[oid] = int(size)

    forbidden = sorted((indexed - allowed) | unsupported)
    return {
        "status": "failed" if forbidden else "passed",
        "files": len(indexed),
        "bytes": sum(sizes[oid] for oid in entries),
        "forbidden": forbidden,
    }


def main() -> int:
    try:
        receipt = check_repository(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 1 if receipt["forbidden"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
