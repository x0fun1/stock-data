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
from zoneinfo import ZoneInfo

from .contracts import normalize_request, parse_timestamp, utc_now
from .paths import safe_output_destination, validate_path_component
from .data_checks import EXCHANGE_TIMEZONES, calendar_sessions, check_market
from .gate_policy import classify_reasons
from .security import provider_failed, sanitize_data

SCHEMA_VERSION = "1.2"
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
    failed = provider_failed(block)
    return {
        **block,
        "data": None if failed else data,
        "status": "failed" if failed else block.get("status", "complete" if data is not None else "unavailable"),
        "actual_source": block.get("actual_source", block.get("source")),
    }


def validate_collection(collection: dict[str, Any], request: dict[str, Any]) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    warnings: list[str] = []
    market = _unwrap_domain(collection.get("market"))
    data = market.get("data")
    if provider_failed(market) or market.get("status") in {"unavailable", "failed", "invalid", "empty"} or not isinstance(data, dict):
        return ["market OHLCV collection is missing or failed"], warnings, 0
    bars = data.get("bars")
    if not isinstance(bars, list) or not bars:
        return ["market.data.bars must be a non-empty normalized OHLCV array"], warnings, 0

    asof = request.get("asof")
    asof_day = parse_timestamp(asof).astimezone(ZoneInfo(EXCHANGE_TIMEZONES[request["market"]])).date() if asof else None
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
                if isinstance(bar.get(field), bool):
                    raise ValueError("boolean is not a price/volume")
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

    for block in collection.values():
        if isinstance(block, dict) and isinstance(block.get("warnings"), list):
            warnings.extend(str(item) for item in block["warnings"])
    return errors, sorted(set(warnings)), kept


def _derived_metadata(domains: dict[str, Any], request: dict[str, Any], *, captured_at: str | None = None, legacy: bool = False,
                      resolved_calendar_evidence: dict[str, Any] | None = None,
                      allow_library_calendar: bool = False) -> dict[str, Any]:
    errors, warnings, count = validate_collection(domains, request)
    gate = check_market(domains["market"], request, captured_at=captured_at,
                        resolved_calendar_evidence=resolved_calendar_evidence,
                        allow_library_calendar=allow_library_calendar)
    if errors or gate["errors"]:
        raise ValueError("snapshot rejected: " + "; ".join(sorted(set(errors + gate["errors"]))))
    if legacy:
        gate["reason_codes"].append("SNAPSHOT_IDENTITY_INVALID")
        classification = classify_reasons(gate["reason_codes"])
        gate["blockers"] = classification["blockers"]
        gate["prediction_eligibility"] = {"status": "blocked", "blockers": classification["blockers"]}
        gate["reporting_eligibility"] = {"status": "veto", "warnings": classification["warnings"]}
        gate["reporting_status"] = "veto"
        gate["forecast_eligible"] = False
    warnings = sorted(set(warnings + gate["blockers"] + gate["warnings"]))
    statuses = {name: str(domains[name].get("status", "unavailable")) for name in DOMAINS}
    sources = {name: {key: block.get(key) for key in ("actual_source", "source_timestamp", "currency", "unit", "adjustment", "adjustment_evidence", "session_calendar", "latest_price_observations", "fallback_used", "fallback_reason")} | {"fetched_at": block.get("fetched_at_utc", block.get("fetched_at"))} for name, block in domains.items()}
    penalties = {
        "CALENDAR_FALLBACK_USED": 10, "ADJUSTMENT_UNKNOWN": 10,
        "CORPORATE_ACTION_UNKNOWN": 5, "LATEST_CLOSE_SINGLE_PROVIDER": 10,
        "EXTREME_PRICE_MOVE_UNRECONCILED": 10,
    }
    history_penalty = round(min(20.0, max(0.0, (504 - count) / 504 * 20.0)))
    reason_penalties = [{"reason_code": code, "points": penalties[code]} for code in sorted(set(gate["reason_codes"])) if code in penalties]
    confidence_score = 100.0 - history_penalty - sum(item["points"] for item in reason_penalties)
    confidence_penalties = ([{"reason": "limited_history_depth", "points": history_penalty}] if history_penalty else []) + reason_penalties
    if not gate["forecast_eligible"]:
        confidence_score = min(confidence_score, 39.0)
        if not any(item.get("reason") == "fatal_prediction_integrity" for item in confidence_penalties):
            confidence_penalties.append({"reason": "fatal_prediction_integrity", "points": 60})
    confidence_score = max(0.0, min(100.0, confidence_score))
    quality = "high" if confidence_score >= 80 else "medium" if confidence_score >= 60 else "low" if confidence_score >= 40 else "very_low"
    score = round(confidence_score / 100.0, 4)
    return {"market_bar_count": count, "data_domains": statuses, "sources": sources, "data_validation": gate,
            "latest_confirmed_close": gate["latest_confirmed_close"], "warnings": warnings,
            "data_quality": {"overall": quality, "score": score, "confidence_score": round(confidence_score),
                             "confidence_penalties": confidence_penalties,
                             "components": {"history_depth": min(1.0, count / 504), "prediction_eligible": gate["forecast_eligible"], "required_domains": ["market"]},
                             "issues": warnings, "reason_codes": gate["reason_codes"]}}


def freeze_snapshot(input_path: Path, output_root: Path) -> Path:
    payload = sanitize_data(json.loads(input_path.read_text(encoding="utf-8-sig")))
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


    # A daily bar dated on the as-of day may still be forming. Unless the gateway
    # confirms the session close, keep its original payload in raw/ but exclude it
    # from normalized data used by every research path.
    market = _unwrap_domain(collection.get("market"))
    market_data = market.get("data")
    if isinstance(market_data, dict) and isinstance(market_data.get("bars"), list):
        asof_day = parse_timestamp(asof_timestamp).astimezone(ZoneInfo(EXCHANGE_TIMEZONES[request["market"]])).date()
        bars = market_data["bars"]
        try:
            sessions = calendar_sessions(market, request["market"]) if market.get("session_calendar") is not None else []
        except (KeyError, TypeError, ValueError):
            sessions = []  # Invalid/missing calendar evidence degrades to source close status.
        forming = {row["date"] for row in sessions if parse_timestamp(row["close_at"]) > parse_timestamp(asof_timestamp)}
        exclude = bars and (str(bars[-1]["date"])[:10] in forming if sessions else _date_only(bars[-1].get("date")) == asof_day and market.get("last_bar_closed") is not True)
        if exclude:
            market_data = dict(market_data)
            market_data["bars"] = bars[:-1]
            if isinstance(market_data.get("history_window"), dict) and bars[:-1]:
                market_data["history_window"] = {**market_data["history_window"], "end": bars[-2]["date"]}
            market["data"] = market_data
            market["warnings"] = list(market.get("warnings", [])) + ["excluded same-as-of-date daily bar because the source did not confirm that the session was closed"]
            warnings.append("latest daily bar was excluded because the gateway did not confirm that its as-of-date session had closed")
            bar_count -= 1
            if bar_count <= 0:
                raise ValueError("snapshot rejected: no closed market bars remain after excluding an unverified same-day bar")
    normalized_market_data = market

    created_at = utc_now()
    if payload.get("captured_at_utc") and parse_timestamp(payload["captured_at_utc"]) > parse_timestamp(created_at):
        raise ValueError("collection capture occurs after snapshot creation")
    source_digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    stamp = created_at.replace("-", "").replace(":", "").replace(".", "")
    slug = re.sub(r"[^A-Z0-9._-]+", "_", request["ticker"])

    normalized: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        block = normalized_market_data if domain == "market" else _unwrap_domain(collection.get(domain))
        normalized[domain] = block
    captured_at = payload.get("captured_at_utc")
    derived = _derived_metadata(normalized, request, captured_at=captured_at, allow_library_calendar=True)
    resolved_calendar = derived["data_validation"].get("resolved_calendar_evidence")
    if resolved_calendar is not None:
        # Persist the optional-library schedule in the signed manifest so snapshot reloads are deterministic.
        derived = _derived_metadata(normalized, request, captured_at=captured_at,
                                    resolved_calendar_evidence=resolved_calendar)
    normalized_hashes = {name: hashlib.sha256(_canonical_bytes(block)).hexdigest() for name, block in normalized.items()}
    raw_blocks = {name: collection.get(name) for name in DOMAINS}
    raw_hashes = {name: hashlib.sha256(_canonical_bytes(block)).hexdigest() for name, block in raw_blocks.items()}
    content_payload = {"request": request, "asof_timestamp": asof_timestamp, "domains": normalized}
    content_digest = hashlib.sha256(_canonical_bytes(content_payload)).hexdigest()
    snapshot_id = f"{slug}_{stamp}_{request['horizon']}_{content_digest[:10]}"
    destination = safe_output_destination(output_root, snapshot_id)
    output_root = destination.parent
    output_root.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite frozen snapshot: {destination}")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "ticker": request["ticker"],
        "market": request["market"],
        "horizon": request["horizon"],
        "request": request,
        "asof_timestamp": asof_timestamp,
        "snapshot_created_at": created_at,
        "collection_captured_at": captured_at,
        "source_payload_sha256": source_digest,
        "snapshot_content_sha256": content_digest,
        "domain_sha256s": normalized_hashes,
        "raw_domain_sha256s": raw_hashes,
    }
    manifest.update(derived)
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()

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
    if manifest.get("schema_version") not in {"1.0", "1.1", SCHEMA_VERSION}:
        raise ValueError(f"unsupported snapshot schema: {manifest.get('schema_version')!r}")
    snapshot_id = validate_path_component(manifest.get("snapshot_id"), label="snapshot_id")
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
    if content_digest != manifest.get("snapshot_content_sha256") or not snapshot_id.endswith(content_digest[:10]):
        raise ValueError("snapshot content identity mismatch")
    if any(manifest.get(key) != manifest.get("request", {}).get(key) for key in ("ticker", "horizon", "market")) or manifest.get("asof_timestamp") != manifest.get("request", {}).get("asof"):
        raise ValueError("snapshot request identity mismatch")
    if manifest.get("schema_version") == SCHEMA_VERSION:
        digest = hashlib.sha256(_canonical_bytes({key: value for key, value in manifest.items() if key != "manifest_sha256"})).hexdigest()
        if digest != manifest.get("manifest_sha256"):
            raise ValueError("snapshot manifest digest mismatch")
        if manifest.get("collection_captured_at") and parse_timestamp(manifest["collection_captured_at"]) > parse_timestamp(manifest["snapshot_created_at"]):
            raise ValueError("collection capture occurs after snapshot creation")
    request = normalize_request(manifest["request"])
    schema_version = manifest.get("schema_version")
    resolved_calendar = (manifest.get("data_validation") or {}).get("resolved_calendar_evidence")
    derived = _derived_metadata(domains, request, captured_at=manifest.get("collection_captured_at"),
                                legacy=schema_version in {"1.0", "1.1"},
                                resolved_calendar_evidence=resolved_calendar)
    manifest.update(derived)
    market = domains["market"]
    if not market.get("data", {}).get("bars"):
        raise ValueError("snapshot has no market OHLCV bars")
    if len(market["data"]["bars"]) != manifest.get("market_bar_count"):
        raise ValueError("snapshot market bar count mismatch")
    return manifest, {**market, "_data_validation": derived["data_validation"]}
