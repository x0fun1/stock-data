"""Freeze a stock-data collection payload into a validated point-in-time snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import normalize_request, parse_timestamp, utc_now

SCHEMA_VERSION = "1.0"
DOMAINS = (
    "market",
    "fundamentals",
    "expectations",
    "news",
    "insider",
    "options",
    "short_volume",
    "sec",
    "macro",
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _date_only(value: Any) -> date:
    text = str(value).strip()
    if "T" in text:
        parsed = parse_timestamp(text)
        return parsed.date()
    return date.fromisoformat(text[:10])


def _unwrap_domain(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"status": "unavailable", "data": None, "actual_source": None}
    # Keep the original gateway response intact. Only unwrap its documented `data`
    # envelope for analysis; do not infer alternate field names or fill omissions.
    data = block.get("data")
    if data is None and "payload" in block:
        data = block.get("payload")
    return {
        **block,
        "data": data,
        "status": block.get("status", "complete" if data is not None else "unavailable"),
        "actual_source": block.get("actual_source", block.get("source")),
    }


def validate_collection(collection: dict[str, Any], request: dict[str, Any]) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    warnings: list[str] = []
    market = _unwrap_domain(collection.get("market"))
    data = market.get("data")
    if market.get("status") in {"unavailable", "failed", "invalid"} or not isinstance(data, dict):
        return ["market OHLCV collection is missing or failed"], warnings, 0
    bars = data.get("bars")
    if not isinstance(bars, list) or not bars:
        return ["market.data.bars must be a non-empty normalized OHLCV array"], warnings, 0

    asof = request.get("asof")
    asof_day = _date_only(asof) if asof else None
    previous_day: date | None = None
    seen: set[date] = set()
    kept = 0
    for index, bar in enumerate(bars):
        if not isinstance(bar, dict):
            errors.append(f"market bar {index} is not an object")
            continue
        try:
            bar_day = _date_only(bar.get("date"))
        except (TypeError, ValueError):
            errors.append(f"market bar {index} has an invalid date")
            continue
        if bar_day in seen:
            errors.append(f"duplicate market session: {bar_day.isoformat()}")
        if previous_day is not None and bar_day <= previous_day:
            errors.append("market bars must be strictly sorted by ascending session date")
        seen.add(bar_day)
        previous_day = bar_day
        if asof_day is not None and bar_day > asof_day:
            errors.append(f"market bar {bar_day.isoformat()} occurs after the requested as-of date")
        values: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "volume"):
            try:
                number = float(bar[field])
            except (KeyError, TypeError, ValueError):
                errors.append(f"market bar {bar_day.isoformat()} is missing numeric {field}")
                continue
            if not math.isfinite(number):
                errors.append(f"market bar {bar_day.isoformat()} has non-finite {field}")
            elif field != "volume" and number <= 0:
                errors.append(f"market bar {bar_day.isoformat()} has non-positive {field}")
            elif field == "volume" and number < 0:
                errors.append(f"market bar {bar_day.isoformat()} has negative volume")
            values[field] = number
        if len(values) == 5:
            if values["high"] < max(values["open"], values["close"], values["low"]):
                errors.append(f"market bar {bar_day.isoformat()} violates high >= open/close/low")
            if values["low"] > min(values["open"], values["close"], values["high"]):
                errors.append(f"market bar {bar_day.isoformat()} violates low <= open/close/high")
        if asof_day is None or bar_day <= asof_day:
            kept += 1

    if market.get("adjustment") not in {"adjusted", "unadjusted", "split_adjusted", "total_return"}:
        warnings.append("OHLCV adjustment convention is missing or unrecognized")
    if seen and asof_day is not None:
        market_age_days = (asof_day - max(seen)).days
        if market_age_days > 7:
            warnings.append(f"latest market session is {market_age_days} calendar days before as-of; market history may be stale")
    if kept < 120:
        warnings.append(f"only {kept} daily bars are available; statistical research may be insufficient")
    for name in DOMAINS[1:]:
        block = _unwrap_domain(collection.get(name))
        if block.get("status") in {"unavailable", "failed", "partial"}:
            warnings.append(f"{name} domain status is {block.get('status')}")
    return errors, warnings, kept


def freeze_snapshot(input_path: Path, output_root: Path) -> Path:
    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        raise ValueError("input must be an object containing a request object")
    request = normalize_request(payload["request"])
    collection = payload.get("domains")
    if not isinstance(collection, dict):
        raise ValueError("input.domains must contain normalized stock-data domain responses")
    asof_timestamp = request["asof"] or payload.get("captured_at_utc") or payload.get("asof_timestamp")
    if asof_timestamp is None:
        raise ValueError("provide request.asof or captured_at_utc with an explicit timezone")
    asof_timestamp = parse_timestamp(asof_timestamp).isoformat().replace("+00:00", "Z")
    request["asof"] = asof_timestamp
    errors, warnings, bar_count = validate_collection(collection, request)
    if errors:
        raise ValueError("snapshot rejected: " + "; ".join(errors))
    market_metadata = _unwrap_domain(collection.get("market"))
    valid_adjustment = market_metadata.get("adjustment") in {"adjusted", "unadjusted", "split_adjusted", "total_return"}
    if request["mode"] == "strict" and not valid_adjustment:
        raise ValueError("strict snapshot rejected: OHLCV adjustment convention is missing or unrecognized")

    # A daily bar dated on the as-of day may still be forming. Unless the gateway
    # confirms the session close, keep its original payload in raw/ but exclude it
    # from normalized data used by every research path.
    market = _unwrap_domain(collection.get("market"))
    market_data = market.get("data")
    if isinstance(market_data, dict) and isinstance(market_data.get("bars"), list):
        asof_day = _date_only(asof_timestamp)
        bars = market_data["bars"]
        if bars and _date_only(bars[-1].get("date")) == asof_day and market.get("last_bar_closed") is not True:
            market_data = dict(market_data)
            market_data["bars"] = bars[:-1]
            market["data"] = market_data
            market["warnings"] = list(market.get("warnings", [])) + ["excluded same-as-of-date daily bar because the source did not confirm that the session was closed"]
            warnings.append("latest daily bar was excluded because the gateway did not confirm that its as-of-date session had closed")
            bar_count -= 1
            if bar_count <= 0:
                raise ValueError("snapshot rejected: no closed market bars remain after excluding an unverified same-day bar")
    normalized_market_data = market

    created_at = utc_now()
    source_digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    stamp = created_at.replace("-", "").replace(":", "").replace(".", "")
    slug = re.sub(r"[^A-Z0-9._-]+", "_", request["ticker"])

    data_domains: dict[str, str] = {}
    sources: dict[str, Any] = {}
    normalized: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        block = normalized_market_data if domain == "market" else _unwrap_domain(collection.get(domain))
        normalized[domain] = block
        status = str(block.get("status", "unavailable")).lower()
        data_domains[domain] = status if status in {"complete", "partial", "failed", "invalid", "unavailable", "available"} else "available"
        sources[domain] = {
            "actual_source": block.get("actual_source"),
            "source_timestamp": block.get("source_timestamp"),
            "fetched_at": block.get("fetched_at_utc", block.get("fetched_at")),
            "currency": block.get("currency"),
            "unit": block.get("unit"),
            "adjustment": block.get("adjustment"),
            "fallback_used": bool(block.get("fallback_used", False)),
            "fallback_reason": block.get("fallback_reason"),
        }
    normalized_hashes = {name: hashlib.sha256(_canonical_bytes(block)).hexdigest() for name, block in normalized.items()}
    raw_blocks = {name: collection.get(name) for name in DOMAINS}
    raw_hashes = {name: hashlib.sha256(_canonical_bytes(block)).hexdigest() for name, block in raw_blocks.items()}
    content_payload = {"request": request, "asof_timestamp": asof_timestamp, "domains": normalized}
    content_digest = hashlib.sha256(_canonical_bytes(content_payload)).hexdigest()
    snapshot_id = f"{slug}_{stamp}_{request['horizon']}_{content_digest[:10]}"
    destination = output_root / snapshot_id
    output_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite frozen snapshot: {destination}")
    available_domains = sum(status in {"complete", "available", "partial"} for status in data_domains.values())
    history_component = min(1.0, bar_count / 504)
    domain_component = available_domains / len(DOMAINS)
    fallback_count = sum(bool(block.get("fallback_used", False)) for block in normalized.values())
    stale_market_history = any("market history may be stale" in warning for warning in warnings)
    quality_score = 0.55 * history_component + 0.45 * domain_component
    quality_score -= min(0.15, 0.03 * fallback_count)
    if stale_market_history:
        quality_score -= 0.15
    if not valid_adjustment:
        quality_score *= 0.8
    quality_score = round(max(0.0, min(1.0, quality_score)), 4)
    quality = "high" if quality_score >= 0.8 else "medium" if quality_score >= 0.55 else "low"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "ticker": request["ticker"],
        "market": request["market"],
        "horizon": request["horizon"],
        "request": request,
        "asof_timestamp": asof_timestamp,
        "snapshot_created_at": created_at,
        "source_payload_sha256": source_digest,
        "snapshot_content_sha256": content_digest,
        "domain_sha256s": normalized_hashes,
        "raw_domain_sha256s": raw_hashes,
        "market_bar_count": bar_count,
        "data_domains": data_domains,
        "sources": sources,
        "data_quality": {"overall": quality, "score": quality_score, "components": {"history_depth": round(history_component, 4), "available_domain_fraction": round(domain_component, 4), "fallback_count": fallback_count, "stale_market_history": stale_market_history, "adjustment_known": valid_adjustment}, "issues": warnings},
        "warnings": warnings,
    }

    temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=output_root))
    try:
        _write_json(temporary / "manifest.json", manifest)
        raw_dir = temporary / "raw"
        raw_dir.mkdir()
        for domain, block in normalized.items():
            _write_json(temporary / f"{domain}.json", block)
            _write_json(raw_dir / f"{domain}.json", raw_blocks[domain])
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def load_snapshot(snapshot_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = snapshot_dir / "manifest.json"
    market_path = snapshot_dir / "market.json"
    if not manifest_path.is_file() or not market_path.is_file():
        raise ValueError("snapshot must include manifest.json and market.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported snapshot schema: {manifest.get('schema_version')!r}")
    domains: dict[str, Any] = {}
    for name in DOMAINS:
        path = snapshot_dir / f"{name}.json"
        raw_path = snapshot_dir / "raw" / f"{name}.json"
        if not path.is_file() or not raw_path.is_file():
            raise ValueError(f"snapshot is missing {name}.json or raw/{name}.json")
        block = json.loads(path.read_text(encoding="utf-8"))
        raw_block = json.loads(raw_path.read_text(encoding="utf-8"))
        expected = manifest.get("domain_sha256s", {}).get(name)
        raw_expected = manifest.get("raw_domain_sha256s", {}).get(name)
        if expected != hashlib.sha256(_canonical_bytes(block)).hexdigest():
            raise ValueError(f"snapshot domain digest mismatch: {name}")
        if raw_expected != hashlib.sha256(_canonical_bytes(raw_block)).hexdigest():
            raise ValueError(f"snapshot raw domain digest mismatch: {name}")
        domains[name] = block
    content_payload = {"request": manifest.get("request"), "asof_timestamp": manifest.get("asof_timestamp"), "domains": domains}
    content_digest = hashlib.sha256(_canonical_bytes(content_payload)).hexdigest()
    if content_digest != manifest.get("snapshot_content_sha256") or not str(manifest.get("snapshot_id", "")).endswith(content_digest[:10]):
        raise ValueError("snapshot content identity mismatch")
    if manifest.get("ticker") != manifest.get("request", {}).get("ticker") or manifest.get("horizon") != manifest.get("request", {}).get("horizon"):
        raise ValueError("snapshot request identity mismatch")
    market = domains["market"]
    if not market.get("data", {}).get("bars"):
        raise ValueError("snapshot has no market OHLCV bars")
    if len(market["data"]["bars"]) != manifest.get("market_bar_count"):
        raise ValueError("snapshot market bar count mismatch")
    return manifest, market
