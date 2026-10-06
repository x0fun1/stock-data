"""Normalize one stock-data gateway receipt; this adapter never performs I/O to providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import normalize_request, utc_now
from .snapshot import DOMAINS, freeze_snapshot


class StockDataAdapter:
    """Adapter between the Skill's MCP/Python gateway calls and the Snapshot Builder.

    The host Skill performs source routing. This adapter receives those responses,
    retains the original envelope, and only exposes caller-normalized domain data.
    """

    def make_receipt(self, request: dict[str, Any], responses: dict[str, Any], *, captured_at_utc: str | None = None) -> dict[str, Any]:
        normalized_request = normalize_request(request)
        domains: dict[str, Any] = {}
        for name in DOMAINS:
            response = responses.get(name)
            if not isinstance(response, dict):
                domains[name] = {"status": "unavailable", "actual_source": None, "data": None}
                continue
            envelope = response.get("gateway_envelope", response)
            payload = response.get("data")
            if isinstance(envelope, dict) and "is_success" in envelope:
                if envelope.get("is_success") is False:
                    status = "failed"
                    payload = None
                else:
                    response_status = str(response.get("status", "complete")).lower()
                    status = "complete" if response_status == "success" else response_status
                    payload = envelope.get("data", payload)
            else:
                response_status = str(response.get("status", "")).lower()
                if response_status in {"error", "failed", "failure"}:
                    status = "failed"
                elif response_status in {"success", "complete"}:
                    status = "complete"
                else:
                    status = response_status or ("complete" if payload is not None else "unavailable")
            block = {
                key: response[key]
                for key in ("actual_source", "source", "source_timestamp", "fetched_at_utc", "fetched_at", "currency", "unit", "adjustment", "fallback_used", "fallback_reason", "last_bar_closed", "published_at", "filed_at", "period_start", "period_end", "warnings")
                if key in response
            }
            block.update({"status": status, "data": payload, "gateway_envelope": envelope})
            if "actual_source" not in block and response.get("source") is not None:
                block["actual_source"] = response["source"]
            domains[name] = block
        return {"request": normalized_request, "captured_at_utc": captured_at_utc or utc_now(), "domains": domains}

    def write_receipt(self, request: dict[str, Any], responses: dict[str, Any], output_path: Path, *, captured_at_utc: str | None = None) -> Path:
        receipt = self.make_receipt(request, responses, captured_at_utc=captured_at_utc)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return output_path

    def freeze(self, receipt_path: Path, output_root: Path) -> Path:
        return freeze_snapshot(receipt_path, output_root)
