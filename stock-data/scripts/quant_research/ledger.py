"""Append-only forward prediction ledger.

A prediction is written once, when the analysis is frozen. A settlement is a
second event that references the prediction id; neither event is ever edited.
Re-running either command is a no-op, so a retried analysis cannot double-count
the same forecast.

The ledger lives outside the run directory on purpose: ``cleanup-run`` deletes
run JSON, and a forward record must survive that cleanup.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ledger-1.0"
PREDICTION = "prediction"
SETTLEMENT = "settlement"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _horizon_sessions(horizon: Any) -> int:
    digits = "".join(ch for ch in str(horizon) if ch.isdigit())
    return max(1, int(digits)) if digits else 5


def _prediction_id(result: dict[str, Any]) -> str:
    snapshot = result.get("snapshot") or {}
    consensus = result.get("consensus") or {}
    probability = consensus.get("prob_up")
    probability_text = f"{float(probability):.12f}" if isinstance(probability, (int, float)) and not isinstance(probability, bool) else "none"
    identity = "|".join([
        str(snapshot.get("ticker")),
        str(snapshot.get("market")),
        str(snapshot.get("horizon")),
        str(snapshot.get("asof_timestamp")),
        str(snapshot.get("snapshot_content_sha256")),
        str((result.get("engine") or {}).get("version")),
        probability_text,
    ])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger line {number} is not valid JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise TypeError(f"ledger line {number} must be a JSON object")
        if event.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"ledger line {number} has unsupported schema {event.get('schema_version')!r}")
        if event.get("event") not in {PREDICTION, SETTLEMENT}:
            raise ValueError(f"ledger line {number} has unknown event {event.get('event')!r}")
        events.append(event)
    return events


def _append(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def record_prediction(result: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    """Append one frozen prediction; the same identity is recorded only once."""
    snapshot = result.get("snapshot") or {}
    consensus = result.get("consensus") or {}
    if not isinstance(snapshot, dict) or not snapshot.get("ticker") or not snapshot.get("horizon"):
        raise ValueError("result does not contain a frozen snapshot")
    probability = consensus.get("prob_up")
    if probability is not None and (not isinstance(probability, (int, float)) or isinstance(probability, bool)
                                    or not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1):
        raise ValueError("consensus.prob_up must be a finite probability or null")
    prediction_id = _prediction_id(result)
    events = read_events(ledger_path)
    if any(event["event"] == PREDICTION and event.get("prediction_id") == prediction_id for event in events):
        return {"status": "duplicate", "prediction_id": prediction_id, "ledger": str(ledger_path)}
    timing = consensus.get("timing") or result.get("timing") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "event": PREDICTION,
        "prediction_id": prediction_id,
        "recorded_at_utc": _now(),
        "status": "pending" if consensus.get("prob_up") is not None else "abstained",
        "ticker": snapshot.get("ticker"),
        "market": snapshot.get("market"),
        "horizon": snapshot.get("horizon"),
        "horizon_sessions": _horizon_sessions(snapshot.get("horizon")),
        "asof_timestamp": snapshot.get("asof_timestamp"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_content_sha256": snapshot.get("snapshot_content_sha256"),
        "decision_session": timing.get("decision_session"),
        "decision_at": timing.get("decision_at"),
        "label_basis": timing.get("label_basis", "close_to_close"),
        "entry_basis": timing.get("entry_basis", "not_modelled"),
        "prob_up": consensus.get("prob_up"),
        "raw_probability": consensus.get("raw_probability"),
        "calibrated_probability": consensus.get("calibrated_probability"),
        "probability_basis": consensus.get("probability_basis"),
        "direction": consensus.get("direction"),
        "confidence": consensus.get("confidence"),
        "confidence_score": consensus.get("confidence_score"),
        "predictive_evidence_grade": consensus.get("predictive_evidence_grade"),
        "eligibility": consensus.get("eligibility"),
        "engine_version": (result.get("engine") or {}).get("version"),
    }
    _append(ledger_path, record)
    return {"status": "recorded", "prediction_id": prediction_id, "ledger": str(ledger_path)}


def settle_pending(
    events: list[dict[str, Any]], bars: list[dict[str, Any]], *,
    ticker: str | None = None, horizon: str | None = None,
    source_snapshot_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return settlement events for every matured prediction, plus skip reasons."""
    predictions = {event["prediction_id"]: event for event in events if event["event"] == PREDICTION}
    settled = {event["prediction_id"] for event in events if event["event"] == SETTLEMENT}
    sessions = {row["date"]: index for index, row in enumerate(bars)}
    appended: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for prediction_id, prediction in predictions.items():
        if prediction_id in settled:
            continue
        if ticker and prediction.get("ticker") != ticker:
            continue
        if horizon and prediction.get("horizon") != horizon:
            continue
        decision = prediction.get("decision_session")
        index = sessions.get(decision)
        horizon_sessions = prediction.get("horizon_sessions")
        if index is None:
            skipped.append({"prediction_id": prediction_id, "reason": "decision_session_not_in_snapshot"})
            continue
        if not isinstance(horizon_sessions, int) or isinstance(horizon_sessions, bool) or horizon_sessions < 1:
            skipped.append({"prediction_id": prediction_id, "reason": "invalid_horizon_sessions"})
            continue
        label_index = index + horizon_sessions
        if label_index >= len(bars):
            skipped.append({"prediction_id": prediction_id, "reason": "label_not_matured"})
            continue
        start, end = float(bars[index]["close"]), float(bars[label_index]["close"])
        if not all(math.isfinite(value) and value > 0 for value in (start, end)):
            skipped.append({"prediction_id": prediction_id, "reason": "invalid_close"})
            continue
        if not isinstance(prediction.get("prob_up"), (int, float)) or isinstance(prediction.get("prob_up"), bool):
            skipped.append({"prediction_id": prediction_id, "reason": "prediction_abstained"})
            continue
        realized = end / start - 1
        hit = 1 if realized > 0 else 0
        probability = float(prediction["prob_up"])
        appended.append({
            "schema_version": SCHEMA_VERSION,
            "event": SETTLEMENT,
            "prediction_id": prediction_id,
            "settled_at_utc": _now(),
            "label_session": bars[label_index]["date"],
            "realized_return": realized,
            "hit": hit,
            "brier": (probability - hit) ** 2,
            "source_snapshot_id": source_snapshot_id,
        })
    return appended, skipped


def settle(ledger_path: Path, snapshot_dir: Path, *,
           ticker: str | None = None, horizon: str | None = None) -> dict[str, Any]:
    from .market_data import market_bars
    from .snapshot import load_snapshot

    manifest, market = load_snapshot(snapshot_dir)
    bars = market_bars(market)
    events = read_events(ledger_path)
    appended, skipped = settle_pending(
        events, bars, ticker=ticker, horizon=horizon,
        source_snapshot_id=manifest.get("snapshot_id"),
    )
    for event in appended:
        _append(ledger_path, event)
    return {
        "status": "settled" if appended else "no_new_settlements",
        "settled": len(appended),
        "skipped": len(skipped),
        "skip_reasons": skipped,
        "source_snapshot_id": manifest.get("snapshot_id"),
    }


def audit(ledger_path: Path) -> dict[str, Any]:
    events = read_events(ledger_path)
    predictions = {event["prediction_id"]: event for event in events if event["event"] == PREDICTION}
    settlements = [event for event in events if event["event"] == SETTLEMENT]
    by_prediction: dict[str, list[dict[str, Any]]] = {}
    for event in settlements:
        by_prediction.setdefault(event["prediction_id"], []).append(event)
    duplicates = sorted(key for key, rows in by_prediction.items() if len(rows) > 1)
    realized: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for prediction_id, rows in by_prediction.items():
        prediction = predictions.get(prediction_id)
        if prediction is None:
            continue
        realized.append((prediction, rows[0]))
    pending = [prediction for prediction_id, prediction in predictions.items()
               if prediction_id not in by_prediction and prediction.get("status") != "abstained"]
    abstained = [prediction for prediction in predictions.values() if prediction.get("status") == "abstained"]

    def metrics(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        if not rows:
            return {"samples": 0, "brier": None, "base_rate_brier": None, "skill": None,
                    "hit_rate": None, "mean_realized_return": None}
        brier = sum((float(prediction["prob_up"]) - int(settlement["hit"])) ** 2 for prediction, settlement in rows) / len(rows)
        base_rate = sum(int(settlement["hit"]) for _, settlement in rows) / len(rows)
        base_brier = sum((base_rate - int(settlement["hit"])) ** 2 for _, settlement in rows) / len(rows)
        return {
            "samples": len(rows),
            "brier": round(brier, 6),
            "base_rate_brier": round(base_brier, 6),
            "skill": round(1 - brier / base_brier, 6) if base_brier > 0 else None,
            "hit_rate": round(base_rate, 6),
            "mean_realized_return": round(sum(float(settlement["realized_return"]) for _, settlement in rows) / len(rows), 6),
        }

    by_horizon = {
        horizon: metrics([row for row in realized if row[0].get("horizon") == horizon])
        for horizon in sorted({prediction.get("horizon") for prediction, _ in realized})
    }
    by_engine = {
        version: metrics([row for row in realized if row[0].get("engine_version") == version])
        for version in sorted({str(prediction.get("engine_version")) for prediction, _ in realized})
    }
    seen_dates: dict[tuple[Any, Any, Any], int] = {}
    for prediction in predictions.values():
        key = (prediction.get("ticker"), prediction.get("horizon"), prediction.get("asof_timestamp"))
        seen_dates[key] = seen_dates.get(key, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger": str(ledger_path),
        "unique_predictions": len(predictions),
        "settled_predictions": len(realized),
        "pending_predictions": len(pending),
        "abstained_predictions": len(abstained),
        "duplicate_settlements": duplicates,
        "duplicate_prediction_dates": sorted(
            {"|".join(str(part) for part in key): count for key, count in seen_dates.items() if count > 1}.items()
        ),
        "overall": metrics(realized),
        "by_horizon": by_horizon,
        "by_engine_version": by_engine,
    }
