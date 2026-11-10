"""Immutable raw-payload store.

Every payload is saved under its SHA-256 digest and accompanied by a receipt
manifest.  Credentials are never written to payloads, paths, or manifests.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..common.hashing import sha256_bytes
from ..common.jsonio import read_json, write_json
from ..contracts import AcquisitionReceipt, to_dict

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str) -> str:
    cleaned = _SAFE.sub("_", value.strip())[:80]
    return cleaned or "unknown"


class RawStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.raw_root = self.root / "raw"
        self.raw_root.mkdir(parents=True, exist_ok=True)

    def save_payload(
        self,
        source_id: str,
        dataset: str,
        payload: bytes,
        receipt: AcquisitionReceipt,
        extension: str = ".json",
    ) -> Path:
        digest = sha256_bytes(payload)
        day = receipt.received_at or receipt.requested_at
        if day is None:
            from ..common.timeutil import now_utc

            day = now_utc()
        target_dir = self.raw_root / _safe(source_id) / _safe(dataset) / f"{day.year:04d}" / f"{day.month:02d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        if not extension.startswith("."):
            extension = "." + extension
        payload_path = target_dir / f"{digest}{extension}"
        if payload_path.exists():
            if sha256_bytes(payload_path.read_bytes()) != digest:
                raise IOError(f"hash collision or corrupted raw payload at {payload_path}")
        else:
            fd, tmp = tempfile.mkstemp(prefix=".payload.", dir=str(target_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, payload_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        receipt.sha256 = digest
        receipt.byte_count = len(payload)
        receipt.raw_path = str(payload_path)
        manifest_path = payload_path.with_suffix(payload_path.suffix + ".manifest.json")
        previous = read_json(manifest_path) if manifest_path.is_file() else {}
        from ..common.timeutil import parse_dt
        first_value = previous.get('first_observed_at') or previous.get('received_at')
        last_value = previous.get('last_observed_at') or previous.get('received_at')
        retained_first = parse_dt(first_value) if first_value else None
        retained_last = parse_dt(last_value) if last_value else None
        first = min(retained_first, day) if retained_first else day
        last = max(retained_last, day) if retained_last else day
        manifest = {**to_dict(receipt), 'first_observed_at': first.isoformat(),
            'last_observed_at': last.isoformat(),
            'observation_time_basis': 'earliest_retained_local_receipt_for_this_payload; not_provider_first_publication'}
        write_json(manifest_path, manifest)
        return manifest_path

    def read_payload(self, manifest_or_payload: str | Path) -> bytes:
        path = Path(manifest_or_payload)
        if path.name.endswith(".manifest.json"):
            manifest = read_json(path)
            path = Path(manifest.get("raw_path", ""))
        return path.read_bytes()

    def read_manifest(self, manifest_path: str | Path) -> dict[str, Any]:
        return read_json(manifest_path)

    def verify(self, manifest_path: str | Path) -> bool:
        manifest = self.read_manifest(manifest_path)
        payload = Path(manifest["raw_path"]).read_bytes()
        return sha256_bytes(payload) == manifest.get("sha256")
