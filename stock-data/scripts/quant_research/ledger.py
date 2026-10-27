"""Append-only forward prediction ledger.

A prediction is written once, when the analysis is frozen. A settlement is a
second event that references the prediction id; neither event is ever edited.
Re-running either command is a no-op, so a retried analysis cannot double-count
the same forecast.

The ledger lives outside the run directory on purpose: ``cleanup-run`` deletes
run JSON, and a forward record must survive that cleanup.

Schema 2.0 separates three ideas that schema 1.0 conflated:

* a prediction is *prospective* only when it was recorded before its label could
  have been observed; historical snapshots imported later are retrospective;
* ``observed_up_rate`` is the base rate of the realised labels, not a model hit
  rate; direction accuracy, balanced accuracy and their baselines are separate;
* the scoring baseline is the causal expanding up-rate that was knowable at the
  prediction date, frozen per prediction; the realised base rate is retained as
  a descriptive ``ex_post_climatology`` only.

Schema 1.0 events remain readable for audit, but they are reported separately and
never counted as forward evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .forecast_policy import (
    FEATURE_VERSIONS,
    MODEL_VERSIONS,
    POLICY_VERSION,
    PROSPECTIVE_RECORDING_GRACE_HOURS,
    PROSPECTIVE_RECORDING_RULE,
    TARGET_VERSION,
    ledger_group_key,
    model_identity,
)

SCHEMA_VERSION = "ledger-3.1"
READABLE_SCHEMAS = frozenset({SCHEMA_VERSION, "ledger-3.0", "ledger-2.0", "ledger-1.0"})
PREDICTION = "prediction"
SETTLEMENT = "settlement"
SETTLEMENT_RULE = "close_to_close_horizon_sessions"
BASELINE_RULE = "causal_expanding_up_rate_matured_labels"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _horizon_sessions(horizon: Any) -> int:
    digits = "".join(ch for ch in str(horizon) if ch.isdigit())
    return max(1, int(digits)) if digits else 5


def _horizon_calendar_days(horizon_sessions: int) -> int:
    # A five-session window spans seven calendar days (Mon close -> Mon close).
    return max(1, math.ceil(horizon_sessions * 7 / 5))


def _prediction_id(result: dict[str, Any]) -> str:
    snapshot = result.get("snapshot") or {}
    consensus = result.get("consensus") or {}
    probability = consensus.get("prob_up")
    probability_text = (
        f"{float(probability):.12f}"
        if isinstance(probability, (int, float)) and not isinstance(probability, bool)
        else "none"
    )
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


def _session_schedule(market: dict[str, Any]) -> list[dict[str, Any]]:
    """Frozen exchange schedule with per-session close instants, when present."""

    data = market.get("data") or {}
    calendar = market.get("session_calendar") or data.get("session_calendar")
    if isinstance(calendar, dict) and isinstance(calendar.get("sessions"), list):
        return [row for row in calendar["sessions"] if isinstance(row, dict)]
    return []


def _canonical_sha256(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger line {number} is not valid JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise TypeError(f"ledger line {number} must be a JSON object")
        if event.get("schema_version") not in READABLE_SCHEMAS:
            raise ValueError(f"ledger line {number} has unsupported schema {event.get('schema_version')!r}")
        if event.get("event") not in {PREDICTION, SETTLEMENT}:
            raise ValueError(f"ledger line {number} has unknown event {event.get('event')!r}")
        recorded_hash = event.get("event_hash")
        if recorded_hash is not None:
            if event.get("prev_hash") != previous_hash:
                raise ValueError(
                    f"ledger line {number} breaks the hash chain: prev_hash does not match the previous event"
                )
            expected = _canonical_sha256({key: value for key, value in event.items() if key != "event_hash"})
            if expected != recorded_hash:
                raise ValueError(f"ledger line {number} failed its own hash check: the event was modified")
            previous_hash = recorded_hash
        else:
            # Legacy rows predate the chain.  The next chained row still anchors on
            # the last chained hash, so an unchained suffix cannot be claimed as
            # part of an intact chain.
            previous_hash = None
        events.append(event)
    return events


def _append(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_hash = _last_chain_hash(path)
    chained = {**event, "prev_hash": previous_hash}
    chained["event_hash"] = _canonical_sha256(chained)
    prefix = ""
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + json.dumps(chained, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _last_chain_hash(path: Path) -> str | None:
    """Read only the tail of the ledger to continue the hash chain.

    Reading and parsing the whole file on every append would make the append cost
    grow quadratically with the ledger, which matters once a daily run has
    accumulated a year of records.
    """

    if not path.is_file():
        return None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return None
        block = min(size, 65536)
        handle.seek(size - block)
        tail = handle.read().decode("utf-8", "ignore")
    lines = tail.splitlines()
    if block < size and lines:
        # The first line of a partial tail is very likely truncated mid-record.
        lines = lines[1:]
    for line in reversed(lines):
        if line.strip():
            try:
                return json.loads(line).get("event_hash")
            except (json.JSONDecodeError, AttributeError):
                return None
    return None


def _researcher_rows(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    researchers = result.get("researchers") or {}
    rows: dict[str, dict[str, Any]] = {}
    if isinstance(researchers, dict):
        for name, row in researchers.items():
            if isinstance(row, dict):
                rows[str(name)] = row
    elif isinstance(researchers, list):
        for index, row in enumerate(researchers):
            if isinstance(row, dict):
                rows[str(row.get("id") or row.get("name") or index)] = row
    return rows


def _model_provenance(result: dict[str, Any]) -> dict[str, Any]:
    rows = _researcher_rows(result)
    probabilities: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    statuses: dict[str, Any] = {}
    weights: dict[str, Any] = {}
    fingerprints: dict[str, str] = {}
    versions: dict[str, Any] = {}
    features: dict[str, Any] = {}
    training_boundaries: dict[str, Any] = {}
    calibration_states: dict[str, Any] = {}
    for name, row in sorted(rows.items()):
        probabilities[name] = row.get("prob_up")
        sources[name] = row.get("probability_source") or row.get("probability_basis")
        statuses[name] = row.get("status")
        if row.get("weight") is not None:
            weights[name] = row.get("weight")
        validation = row.get("validation")
        if isinstance(validation, dict):
            fingerprints[name] = _canonical_sha256(validation)
            hyperparameters = validation.get("model_hyperparameters") or {}
            if hyperparameters.get("features"):
                features[name] = list(hyperparameters["features"])
            boundary = validation.get("current_prediction_model_training_boundary")
            if isinstance(boundary, dict):
                training_boundaries[name] = boundary
            calibration = (
                validation.get("current_prediction_calibration")
                or validation.get("probability_calibration")
            )
            if isinstance(calibration, dict):
                calibration_states[name] = {
                    key: calibration.get(key) for key in (
                        "status", "applied", "calibrated", "observations",
                        "fit_observations", "label_end_session", "reachable",
                    )
                    if key in calibration
                }
        if row.get("model_version") is not None:
            versions[name] = row.get("model_version")
        elif isinstance(validation, dict) and validation.get("model_version") is not None:
            versions[name] = validation.get("model_version")
    config = {
        "engine_version": (result.get("engine") or {}).get("version"),
        "horizon": (result.get("snapshot") or {}).get("horizon"),
        "path_probabilities": probabilities,
        "path_probability_sources": sources,
        "path_statuses": statuses,
        "path_weights": weights,
    }
    return {
        "prediction_config_sha256": _canonical_sha256(config),
        "path_probabilities": probabilities,
        "path_probability_sources": sources,
        "path_statuses": statuses,
        "path_weights": weights,
        "validation_sha256": fingerprints,
        "model_versions": versions,
        "feature_sets": features,
        "training_boundaries": training_boundaries,
        "calibration_states": calibration_states,
    }


def _forward_eligibility(
    *,
    recorded_at: datetime,
    decision_at: datetime | None,
    decision_session: Any,
    horizon_sessions: int,
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify a record as forward evidence, late, or backfilled.

    Two independent conditions are required, and they answer different questions:

    * ``recorded_before_label_maturity`` — the outcome was not yet observable;
    * ``recorded_promptly`` — the record was written before the next session could
      open, so no post-decision price could have informed it.

    The second condition is new in ledger-3.1.  Without it a prediction written
    several sessions later still passed the maturity check and was counted as
    forward evidence even though the market had already moved.
    """

    maturity: datetime | None = None
    maturity_basis = "unavailable"
    ordered = [str(row.get("date"))[:10] for row in (sessions or []) if isinstance(row, dict)]
    if isinstance(decision_session, str) and decision_session in ordered:
        index = ordered.index(decision_session) + horizon_sessions
        if index < len(sessions or []):
            close_at = _parse_time((sessions or [])[index].get("close_at"))
            if close_at is not None:
                maturity = close_at
                maturity_basis = "exchange_calendar_target_session_close"
    if maturity is None and decision_at is not None:
        maturity = decision_at + timedelta(days=_horizon_calendar_days(horizon_sessions))
        maturity_basis = "calendar_day_approximation"
    elif maturity is None and isinstance(decision_session, str):
        session_date = _parse_time(f"{decision_session}T23:59:59Z")
        if session_date is not None:
            maturity = session_date + timedelta(days=_horizon_calendar_days(horizon_sessions) + 2)
            maturity_basis = "calendar_day_approximation"
    if maturity is None:
        return {
            "prospective_eligible": False,
            "reason": "decision_time_unavailable",
            "label_maturity_at_utc": None,
            "maturity_basis": maturity_basis,
            "recorded_at_utc": _iso(recorded_at),
            "recorded_before_label_maturity": False,
            "recorded_promptly": False,
            "prompt_deadline_at_utc": None,
            "rule": PROSPECTIVE_RECORDING_RULE,
        }
    deadline = (
        decision_at + timedelta(hours=PROSPECTIVE_RECORDING_GRACE_HOURS)
        if decision_at is not None else None
    )
    before_maturity = recorded_at < maturity
    prompt = deadline is not None and recorded_at <= deadline
    if not before_maturity:
        reason = "recorded_after_label_maturity"
    elif not prompt:
        reason = "recorded_after_prompt_window"
    else:
        reason = "recorded_before_label_maturity_within_prompt_window"
    return {
        "prospective_eligible": bool(before_maturity and prompt),
        "reason": reason,
        "label_maturity_at_utc": _iso(maturity),
        "maturity_basis": maturity_basis,
        "recorded_at_utc": _iso(recorded_at),
        "recorded_before_label_maturity": before_maturity,
        "recorded_promptly": prompt,
        "prompt_deadline_at_utc": _iso(deadline) if deadline is not None else None,
        "recording_grace_hours": PROSPECTIVE_RECORDING_GRACE_HOURS,
        "recording_lag_hours": round((recorded_at - (decision_at or maturity)).total_seconds() / 3600, 3)
        if decision_at is not None else None,
        "rule": PROSPECTIVE_RECORDING_RULE,
    }


def record_prediction(
    result: dict[str, Any],
    ledger_path: Path,
    *,
    import_reason: str | None = None,
    snapshot_dir: Path | None = None,
    bars: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Append one frozen prediction; the same identity is recorded only once.

    ``clock`` exists so tests can place the recording instant deterministically.
    The CLI never supplies it: a caller must not be able to backdate a record.
    """
    snapshot = result.get("snapshot") or {}
    consensus = result.get("consensus") or {}
    if not isinstance(snapshot, dict) or not snapshot.get("ticker") or not snapshot.get("horizon"):
        raise ValueError("result does not contain a frozen snapshot")
    if snapshot.get("horizon") != "5D":
        raise ValueError("the fixed policy ledger only accepts 5D forecasts")
    probability = consensus.get("prob_up")
    if probability is not None and (
        not isinstance(probability, (int, float))
        or isinstance(probability, bool)
        or not math.isfinite(float(probability))
        or not 0 <= float(probability) <= 1
    ):
        raise ValueError("consensus.prob_up must be a finite probability or null")
    prediction_id = _prediction_id(result)
    events = read_events(ledger_path)
    if any(event["event"] == PREDICTION and event.get("prediction_id") == prediction_id for event in events):
        return {"status": "duplicate", "prediction_id": prediction_id, "ledger": str(ledger_path)}
    group_key = ledger_group_key(ticker=str(snapshot.get("ticker")), market=str(snapshot.get("market")), path="quant")
    timing = consensus.get("timing") or result.get("timing") or {}
    decision_session = timing.get("decision_session")
    if any(
        event.get("event") == PREDICTION
        and event.get("group_key") == group_key
        and event.get("decision_session") == decision_session
        for event in events
    ):
        return {"status": "duplicate", "reason": "same_group_and_decision_session", "ledger": str(ledger_path)}
    recorded_at = _parse_time(_iso(clock())) if callable(clock) else _parse_time(_now())
    if recorded_at is None:
        raise ValueError("system clock could not be parsed")
    decision_at = _parse_time(timing.get("decision_at")) or _parse_time(snapshot.get("asof_timestamp"))
    horizon_sessions = _horizon_sessions(snapshot.get("horizon"))
    if bars is None and snapshot_dir is not None:
        from .market_data import market_bars
        from .snapshot import load_snapshot

        _manifest, market = load_snapshot(snapshot_dir)
        bars = market_bars(market)
        if sessions is None:
            sessions = _session_schedule(market)
    decision_session = timing.get("decision_session")
    baseline_probability = None
    baseline_history_labels = 0
    if bars and isinstance(decision_session, str):
        sessions = {row["date"]: index for index, row in enumerate(bars)}
        decision_index = sessions.get(decision_session)
        if decision_index is not None:
            base_rates = _causal_base_rates(bars, horizon_sessions)
            baseline_probability = base_rates[decision_index]
            if baseline_probability is not None:
                baseline_history_labels = decision_index - horizon_sessions + 1
    forward = _forward_eligibility(
        recorded_at=recorded_at,
        decision_at=decision_at,
        decision_session=timing.get("decision_session"),
        horizon_sessions=horizon_sessions,
        sessions=sessions,
    )
    if not forward["prospective_eligible"] and not import_reason:
        import_reason = {
            "recorded_after_label_maturity": "historical_snapshot_recorded_after_label_maturity",
            "recorded_after_prompt_window": "recorded_after_prompt_window_not_forward_evidence",
            "decision_time_unavailable": "decision_time_unavailable",
        }.get(forward["reason"], "not_prospective")
    sampling = consensus.get("sampling") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "event": PREDICTION,
        "prediction_id": prediction_id,
        "recorded_at_utc": _iso(recorded_at),
        "status": "pending" if consensus.get("prob_up") is not None else "abstained",
        "ticker": snapshot.get("ticker"),
        "market": snapshot.get("market"),
        "horizon": snapshot.get("horizon"),
        "horizon_sessions": horizon_sessions,
        "asof_timestamp": snapshot.get("asof_timestamp"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_content_sha256": snapshot.get("snapshot_content_sha256"),
        "decision_session": decision_session,
        "decision_at": timing.get("decision_at"),
        "label_basis": timing.get("label_basis", "close_to_close"),
        "entry_basis": timing.get("entry_basis", "not_modelled"),
        "prob_up": consensus.get("prob_up"),
        "raw_probability": consensus.get("raw_probability"),
        "calibrated_probability": None,
        "calibration_status": "disabled_by_policy",
        "probability_basis": consensus.get("probability_label") or "uncalibrated_model_estimate",
        "direction": consensus.get("direction"),
        "confidence": None,
        "confidence_score": None,
        "confidence_score_deprecated": True,
        "predictive_evidence_grade": None,
        "eligibility": consensus.get("eligibility"),
        "policy_version": POLICY_VERSION,
        "model_version": model_identity("quant")["model_version"],
        "feature_version": model_identity("quant")["feature_version"],
        "target_version": TARGET_VERSION,
        "group_key": ledger_group_key(ticker=str(snapshot.get("ticker")), market=str(snapshot.get("market")), path="quant"),
        "probability_transform": (consensus.get("production") or {}).get("probability_transform"),
        "causal_base_rate": (consensus.get("production") or {}).get("causal_base_rate"),
        "decision_sampled": consensus.get("decision_sampled"),
        "sampling_status": sampling.get("status") or (
            "anchored" if consensus.get("decision_sampled") is not None else "unanchored_calendar"
        ),
        "production_probability_eligible": consensus.get("production_probability_eligible"),
        "engine_version": (result.get("engine") or {}).get("version"),
        "forward_eligibility": forward,
        "prospective_eligible": bool(forward["prospective_eligible"]),
        "import_reason": None if forward["prospective_eligible"] else import_reason,
        "model_provenance": _model_provenance(result),
        "baseline_probability": baseline_probability,
        "baseline_method": BASELINE_RULE if baseline_probability is not None else None,
        "baseline_history_labels": baseline_history_labels,
        "baseline_frozen_at": "prediction" if baseline_probability is not None else None,
    }
    _append(ledger_path, record)
    return {
        "status": "recorded",
        "prediction_id": prediction_id,
        "prospective_eligible": record["prospective_eligible"],
        "ledger": str(ledger_path),
    }


def _causal_base_rates(bars: list[dict[str, Any]], horizon_sessions: int) -> list[float | None]:
    """Expanding up-rate over labels that had matured by each decision index."""
    closes: list[float] = []
    for row in bars:
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            close = float("nan")
        closes.append(close)
    up_flags: list[int] = [0]
    for index in range(max(0, len(closes) - horizon_sessions)):
        start, end = closes[index], closes[index + horizon_sessions]
        up_flags.append(up_flags[-1] + (1 if math.isfinite(start) and math.isfinite(end) and end > start else 0))
    rates: list[float | None] = []
    for index in range(len(closes)):
        count = index - horizon_sessions + 1
        if count <= 0 or count >= len(up_flags):
            rates.append(None)
            continue
        rates.append(up_flags[count] / count)
    return rates


def settle_pending(
    events: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    ticker: str,
    market: str,
    horizon: str | None = None,
    source_snapshot_id: str | None = None,
    source_snapshot_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return settlement events for matured predictions, plus skip reasons.

    ``ticker`` and ``market`` identify the bars and are mandatory: a settlement
    may only label a prediction that belongs to the same instrument and market.
    The caller normally derives both from the snapshot manifest in ``settle``.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("settle_pending requires the ticker that the bars belong to")
    if not isinstance(market, str) or not market.strip():
        raise ValueError("settle_pending requires the market that the bars belong to")
    predictions = {event["prediction_id"]: event for event in events if event["event"] == PREDICTION}
    settled = {event["prediction_id"] for event in events if event["event"] == SETTLEMENT}
    sessions = {row["date"]: index for index, row in enumerate(bars)}
    appended: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    base_rates: dict[int, list[float | None]] = {}
    for prediction_id, prediction in predictions.items():
        if prediction_id in settled:
            continue
        if prediction.get("ticker") != ticker:
            skipped.append({
                "prediction_id": prediction_id,
                "reason": "ticker_mismatch",
                "prediction_ticker": prediction.get("ticker"),
                "bars_ticker": ticker,
            })
            continue
        if prediction.get("market") != market:
            skipped.append({
                "prediction_id": prediction_id,
                "reason": "market_mismatch" if prediction.get("market") else "prediction_market_missing",
                "prediction_market": prediction.get("market"),
                "bars_market": market,
            })
            continue
        if horizon and prediction.get("horizon") != horizon:
            continue
        if prediction.get("label_basis") not in (None, "close_to_close"):
            skipped.append({"prediction_id": prediction_id, "reason": "unsupported_label_basis"})
            continue
        decision = prediction.get("decision_session")
        index = sessions.get(decision)
        horizon_sessions = prediction.get("horizon_sessions")
        if not isinstance(horizon_sessions, int) or isinstance(horizon_sessions, bool):
            # Ledger-1.0 rows have no explicit session count; derive it from the label.
            horizon_sessions = _horizon_sessions(prediction.get("horizon"))
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
        baseline = prediction.get("baseline_probability")
        baseline_frozen_at = "prediction"
        baseline_history_labels = prediction.get("baseline_history_labels", 0)
        if baseline is None:
            if horizon_sessions not in base_rates:
                base_rates[horizon_sessions] = _causal_base_rates(bars, horizon_sessions)
            baseline = base_rates[horizon_sessions][index]
            baseline_frozen_at = "settlement"
            baseline_history_labels = index - horizon_sessions + 1 if baseline is not None else 0
        appended.append({
            "schema_version": SCHEMA_VERSION,
            "event": SETTLEMENT,
            "prediction_id": prediction_id,
            "settled_at_utc": _now(),
            "settlement_rule": SETTLEMENT_RULE,
            "bars_ticker": ticker,
            "bars_market": market,
            "bars_first_session": bars[0]["date"] if bars else None,
            "bars_last_session": bars[-1]["date"] if bars else None,
            "bars_session_count": len(bars),
            "decision_session": decision,
            "label_session": bars[label_index]["date"],
            "entry_close": start,
            "exit_close": end,
            "realized_return": realized,
            "hit": hit,
            "brier": (probability - hit) ** 2,
            "baseline_probability": baseline,
            "baseline_brier": (baseline - hit) ** 2 if baseline is not None else None,
            "baseline_method": BASELINE_RULE,
            "baseline_history_labels": baseline_history_labels,
            "baseline_frozen_at": baseline_frozen_at,
            "probability_source": "recorded_prob_up",
            "source_snapshot_id": source_snapshot_id,
            "source_snapshot_sha256": source_snapshot_sha256,
        })
    return appended, skipped


def settle(ledger_path: Path, snapshot_dir: Path, *,
           ticker: str | None = None, horizon: str | None = None) -> dict[str, Any]:
    from .market_data import market_bars
    from .snapshot import load_snapshot

    manifest, market = load_snapshot(snapshot_dir)
    bars = market_bars(market)
    snapshot_ticker = manifest.get("ticker")
    snapshot_market = manifest.get("market")
    if not isinstance(snapshot_ticker, str) or not snapshot_ticker.strip():
        raise ValueError("snapshot manifest has no ticker; refusing to settle without an identity")
    if not isinstance(snapshot_market, str) or not snapshot_market.strip():
        raise ValueError("snapshot manifest has no market; refusing to settle without an identity")
    if ticker is not None and ticker != snapshot_ticker:
        raise ValueError(
            f"refusing to settle {ticker} with a snapshot for {snapshot_ticker}; "
            "the snapshot identity is authoritative"
        )
    events = read_events(ledger_path)
    appended, skipped = settle_pending(
        events, bars,
        ticker=snapshot_ticker,
        market=snapshot_market,
        horizon=horizon,
        source_snapshot_id=manifest.get("snapshot_id"),
        source_snapshot_sha256=manifest.get("snapshot_content_sha256"),
    )
    for event in appended:
        _append(ledger_path, event)
    return {
        "status": "settled" if appended else "no_new_settlements",
        "settled": len(appended),
        "skipped": len(skipped),
        "skip_reasons": skipped,
        "source_snapshot_id": manifest.get("snapshot_id"),
        "bars_ticker": snapshot_ticker,
        "bars_market": snapshot_market,
    }


def _metrics(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    if not rows:
        return {
            "samples": 0,
            "brier": None,
            "causal_baseline_brier": None,
            "causal_baseline_samples": 0,
            "brier_skill_vs_causal_baseline": None,
            "observed_up_rate": None,
            "ex_post_climatology": None,
            "ex_post_climatology_brier": None,
            "direction_accuracy": None,
            "direction_samples": 0,
            "direction_ties": 0,
            "always_up_accuracy": None,
            "direction_accuracy_delta_vs_always_up": None,
            "balanced_accuracy": None,
            "mean_probability": None,
            "mean_realized_return": None,
            "calibrated_predictions": 0,
            "uncalibrated_predictions": 0,
        }
    outcomes = [int(settlement["hit"]) for _, settlement in rows]
    probabilities = [float(prediction["prob_up"]) for prediction, _ in rows]
    brier = sum(
        (probability - outcome) ** 2
        for probability, outcome in zip(probabilities, outcomes, strict=True)
    ) / len(rows)
    observed_up = sum(outcomes) / len(rows)
    climatology_brier = sum((observed_up - outcome) ** 2 for outcome in outcomes) / len(rows)
    baseline_pairs = [
        (float(settlement["baseline_probability"]), int(settlement["hit"]))
        for _, settlement in rows
        if isinstance(settlement.get("baseline_probability"), (int, float))
        and not isinstance(settlement.get("baseline_probability"), bool)
    ]
    baseline_brier = (
        sum((baseline - outcome) ** 2 for baseline, outcome in baseline_pairs) / len(baseline_pairs)
        if baseline_pairs else None
    )
    direction_rows = [
        (probability > 0.5, outcome)
        for probability, outcome in zip(probabilities, outcomes, strict=True)
        if probability != 0.5
    ]
    direction_accuracy = (
        sum(1 for predicted_up, outcome in direction_rows if int(predicted_up) == outcome) / len(direction_rows)
        if direction_rows else None
    )
    always_up = (
        sum(outcome for _, outcome in direction_rows) / len(direction_rows)
        if direction_rows else None
    )
    recalls: list[float] = []
    for label in (0, 1):
        subset = [(predicted_up, outcome) for predicted_up, outcome in direction_rows if outcome == label]
        if subset:
            recalls.append(sum(1 for predicted_up, outcome in subset if int(predicted_up) == outcome) / len(subset))
    balanced = sum(recalls) / len(recalls) if len(recalls) == 2 else None
    return {
        "samples": len(rows),
        "brier": round(brier, 6),
        "causal_baseline_brier": round(baseline_brier, 6) if baseline_brier is not None else None,
        "causal_baseline_samples": len(baseline_pairs),
        "brier_skill_vs_causal_baseline": round(1 - brier / baseline_brier, 6) if baseline_brier else None,
        "observed_up_rate": round(observed_up, 6),
        "ex_post_climatology": round(observed_up, 6),
        "ex_post_climatology_brier": round(climatology_brier, 6),
        "direction_accuracy": round(direction_accuracy, 6) if direction_accuracy is not None else None,
        "direction_samples": len(direction_rows),
        "direction_ties": len(rows) - len(direction_rows),
        "always_up_accuracy": round(always_up, 6) if always_up is not None else None,
        "direction_accuracy_delta_vs_always_up": (
            round(direction_accuracy - always_up, 6)
            if direction_accuracy is not None and always_up is not None else None
        ),
        "balanced_accuracy": round(balanced, 6) if balanced is not None else None,
        "mean_probability": round(sum(probabilities) / len(probabilities), 6),
        "mean_realized_return": round(
            sum(float(settlement["realized_return"]) for _, settlement in rows) / len(rows), 6,
        ),
        "calibrated_predictions": sum(
            1 for prediction, _ in rows if prediction.get("calibrated_probability") is not None
        ),
        "uncalibrated_predictions": sum(
            1 for prediction, _ in rows if prediction.get("calibrated_probability") is None
        ),
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
    prospective = [
        row for row in realized
        if row[0].get("schema_version") == SCHEMA_VERSION and row[0].get("prospective_eligible") is True
    ]
    prompt_proven = [
        row for row in prospective
        if (row[0].get("forward_eligibility") or {}).get("recorded_promptly") is True
    ]
    retrospective = [
        row for row in realized
        if row[0].get("schema_version") == SCHEMA_VERSION and row[0].get("prospective_eligible") is not True
    ]
    legacy = [row for row in realized if row[0].get("schema_version") != SCHEMA_VERSION]
    pending = [
        prediction for prediction_id, prediction in predictions.items()
        if prediction_id not in by_prediction and prediction.get("status") != "abstained"
    ]
    pending_prospective = [p for p in pending if p.get("prospective_eligible") is True]
    abstained = [prediction for prediction in predictions.values() if prediction.get("status") == "abstained"]

    def by_key(rows: list[tuple[dict[str, Any], dict[str, Any]]], key: str) -> dict[str, Any]:
        values = sorted({str(prediction.get(key)) for prediction, _ in rows})
        return {
            value: _metrics([row for row in rows if str(row[0].get(key)) == value])
            for value in values
        }

    identity_conflicts = sorted(
        prediction_id
        for prediction_id, rows in by_prediction.items()
        if (prediction := predictions.get(prediction_id)) is not None
        and any(
            settlement.get("bars_ticker") not in (None, prediction.get("ticker"))
            or settlement.get("bars_market") not in (None, prediction.get("market"))
            for settlement in rows
        )
    )
    seen_dates: dict[tuple[Any, Any, Any], int] = {}
    for prediction in predictions.values():
        key = (prediction.get("ticker"), prediction.get("horizon"), prediction.get("asof_timestamp"))
        seen_dates[key] = seen_dates.get(key, 0) + 1
    settled_forward = len(prospective)
    coverage = settled_forward / (settled_forward + len(pending_prospective)) if settled_forward + len(pending_prospective) else None
    cutoff = (date.today() - timedelta(days=364)).isoformat()
    recent = [item for item in prospective if str(item.get("decision_session") or "") >= cutoff]
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger": str(ledger_path),
        "unique_predictions": len(predictions),
        "settled_predictions": len(realized),
        "prospective_settled_predictions": settled_forward,
        "prospective_prompt_recording_verified": len(prompt_proven),
        "retrospective_settled_predictions": len(retrospective),
        "legacy_schema_settled_predictions": len(legacy),
        "pending_predictions": len(pending),
        "pending_prospective_predictions": len(pending_prospective),
        "abstained_predictions": len(abstained),
        "settlement_coverage_prospective": round(coverage, 6) if coverage is not None else None,
        "duplicate_settlements": duplicates,
        "identity_conflicts": identity_conflicts,
        "duplicate_prediction_dates": sorted(
            {"|".join(str(part) for part in key): count for key, count in seen_dates.items() if count > 1}.items()
        ),
        "metrics_basis": "prospective_eligible_predictions_only",
        "evaluation_window_days": 364,
        "evaluation_window_start": cutoff,
        "recent_prospective": _metrics(recent),
        "overall": _metrics(prospective),
        "prospective": _metrics(prospective),
        "retrospective": _metrics(retrospective),
        "legacy_schema": _metrics(legacy),
        "all_settled_descriptive": _metrics(realized),
        "by_horizon": by_key(prospective, "horizon"),
        "by_engine_version": by_key(prospective, "engine_version"),
        "caveats": [
            "observed_up_rate is the realised label base rate, not a model hit rate",
            "ex_post_climatology is descriptive and must not be used as a deployable baseline",
            "only prospective_eligible predictions enter the forward score",
            "prospective also requires a provable prompt recording timestamp (ledger-3.1); "
            "earlier rows are reported separately",
            "legacy ledger-1.0/2.0/3.0 rows cannot prove prompt recording and are reported separately",
        ],
    }


def prospective_records(
    ledger_path: str | Path, *, ticker: str | None = None, market: str | None = None,
) -> list[dict[str, Any]]:
    """Matured, prospective, same-identity records for the directional-edge check.

    The filter is deliberately narrow and identical to the group key:

    * policy, model, feature and target versions must all match the active policy,
      so a model upgrade cannot inherit the previous model's forward evidence;
    * the record must be prospective *and* provably written inside the prompt
      window.  Rows written before ledger-3.1 have no such proof, so they are
      returned with ``evidence_scope="legacy_unverified_recording_time"`` and
      ``sampled=False`` instead of being silently counted;
    * an unanchored sampling phase yields ``sampling_status="unanchored_calendar"``
      with ``sampled=None``: the count of non-overlapping forward outcomes is
      unavailable, not zero and certainly not "every row is independent".
    """

    events = read_events(Path(ledger_path))
    predictions: dict[str, dict[str, Any]] = {}
    settlements: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == PREDICTION:
            predictions[event["prediction_id"]] = event
        elif event.get("event") == SETTLEMENT:
            settlements[event["prediction_id"]] = event
    rows = []
    for prediction_id, settlement in settlements.items():
        prediction = predictions.get(prediction_id)
        if not prediction or prediction.get("prospective_eligible") is not True:
            continue
        if (
            prediction.get("policy_version") != POLICY_VERSION
            or prediction.get("target_version") != TARGET_VERSION
            or prediction.get("model_version") != MODEL_VERSIONS["quant"]
            or prediction.get("feature_version") != FEATURE_VERSIONS["quant"]
        ):
            continue
        if ticker and str(prediction.get("ticker")).upper() != ticker.upper():
            continue
        if market and str(prediction.get("market")).upper() != market.upper():
            continue
        transform = prediction.get("probability_transform") or {}
        forward = prediction.get("forward_eligibility") or {}
        recording_prompt_proven = forward.get("recorded_promptly") is True
        sampling_status = prediction.get("sampling_status")
        if sampling_status is None:
            sampled_value = prediction.get("decision_sampled")
            sampling_status = "anchored" if sampled_value is not None else "unknown_legacy"
        sampled = prediction.get("decision_sampled") is True
        evidence_scope = (
            "prospective_prompt_recording_anchored" if recording_prompt_proven and sampled
            else "prospective_prompt_recording_unanchored" if recording_prompt_proven
            else "legacy_unverified_recording_time"
        )
        rows.append({
            "decision_session": prediction.get("decision_session"),
            "probability": prediction.get("prob_up"),
            "target": settlement.get("hit"),
            "probability_transform": transform,
            "causal_base_rate": transform.get("base_rate_at_origin", prediction.get("causal_base_rate")),
            "sampled": sampled if recording_prompt_proven else False,
            "sampling_status": sampling_status,
            "recording_prompt_proven": recording_prompt_proven,
            "status": "matured",
            "evidence_scope": evidence_scope,
            "settled_at": settlement.get("recorded_at"),
        })
    return rows
