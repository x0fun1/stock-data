"""Collect once, freeze evidence, and keep all synthesis strictly offline."""

from __future__ import annotations

from pathlib import Path

from .assessment.validation import incomplete, prepare, validate_assessment
from .common.jsonio import atomic_text, digest, read_json, write_json
from .common.paths import child
from .common.security import sanitize_data
from .context.facts import build_context
from .contracts import object_keys, validate_request
from .coverage import summarize_coverage
from .events.normalize import build_events, normalize_records
from .reporting.report import render, validate_reply
from .storage.store import Store, load_snapshot, run_lock


def collect(request: dict, output_root: Path, imports: dict | None = None, *, incremental: bool = False) -> dict:
    from .collection.collect import collect_sources

    request = validate_request(request)
    if incremental:
        request["collection_mode"] = "incremental"
    imports = imports or {}
    object_keys(imports, {"records", "coverage", "financial_facts", "exposures"}, set(), "imports")
    if any(not isinstance(value, list) for value in imports.values()):
        raise ValueError("all import sections must be arrays")
    imports = sanitize_data(imports)
    store = Store(output_root)
    try:
        previous = store.values("coverage") if request["collection_mode"] == "incremental" else []
        collected = collect_sources(request, imports, previous_coverage=previous)
        raw = collected["records"]
        if request["collection_mode"] == "incremental":
            sources = {item["source_id"] for item in request["sources"]}
            old = [item for item in store.values("articles") if item["source_id"] in sources
                   and (item["category"] in {"macro", "industry"} or request["symbol"] in item["entities"])]
            raw = old + raw
        records, rejected = normalize_records(raw)
        built = build_events(records, request)
        context_input = {**imports, "financial_facts": [*imports.get("financial_facts", []), *collected.get("financial_facts", [])]}
        context = build_context(context_input, records, built["evidence"], request)
        coverage = collected["coverage"]
        snapshot = {"schema_version": request["schema_version"], "request": request, "records": records,
                    **built, "context": context, "coverage": coverage,
                    "coverage_summary": summarize_coverage(request, coverage), "source_capabilities": collected["source_capabilities"],
                    "limitations": collected["limitations"]}
        snapshot["excluded_records"].extend(rejected)
        if request["collection_mode"] == "incremental" and not context_input["financial_facts"]:
            snapshot["limitations"].append("Incremental coverage reuses stored source receipts; financial/exposure context must be re-imported or collected for this target and cutoff.")
        store.put("articles", records, id_key="record_id")
        store.put("evidence", built["evidence"], id_key="evidence_id")
        # Event versions carry reference metadata only; licensed article text lives in articles/evidence.
        store.put("events", [{key: value for key, value in item.items() if key != "title"} for item in built["events"]])
        store.put("financial_facts", context["financial_facts"], id_key="fact_id")
        store.put("exposures", [{key: value for key, value in item.items() if key != "description"} for item in context["exposures"]], id_key="exposure_id")
        store.put("coverage", coverage)
        store.put("sources", collected["source_capabilities"])
        run_dir = store.freeze(snapshot)
        return {"status": "collected", "run_dir": str(run_dir), "records": len(records), "events": len(built["events"]),
                "coverage": snapshot["coverage_summary"], "limitations": snapshot["limitations"]}
    finally:
        store.close()


def prepare_run(run_dir: Path) -> dict:
    with run_lock(run_dir):
        snapshot, manifest = load_snapshot(run_dir)
        prepared = prepare(snapshot, manifest["input_digest"])
        write_json(child(run_dir, "prepared.json"), prepared)
    return {"prepared_file": str(run_dir / "prepared.json"), "input_digest": manifest["input_digest"],
            "cache_key": prepared["cache_key"], "batches": len(prepared["batches"]), "omitted": len(prepared["analysis_queue"])}


def analyze_run(run_dir: Path, assessment_path: Path) -> dict:
    with run_lock(run_dir):
        snapshot, manifest = load_snapshot(run_dir)
        try:
            assessment = read_json(assessment_path)
            result = validate_assessment(assessment, snapshot, manifest["input_digest"])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # A failed retry cannot leave an older successful conclusion looking current.
            result = incomplete(snapshot, manifest["input_digest"], f"宿主分析未通过校验：{exc}")
            write_json(child(run_dir, "result.json"), {"result": result, "result_digest": digest(result), "validated": False})
            atomic_text(child(run_dir, "report.md"), render(snapshot, result))
            raise ValueError(str(exc)) from exc
        write_json(child(run_dir, "assessment.json"), assessment)
        write_json(child(run_dir, "result.json"), {"result": result, "result_digest": digest(result), "validated": True})
        atomic_text(child(run_dir, "report.md"), render(snapshot, result))
    return {"status": "analyzed", "direction": result["synthesis"]["direction"], "analysis_status": result["synthesis"]["status"],
            "counts": result["analysis_counts"], "selected_event_id": result["selection"]["selected_event_id"]}


def _result(run_dir: Path, snapshot: dict, manifest: dict) -> dict:
    result_path = child(run_dir, "result.json")
    if not result_path.exists():
        return incomplete(snapshot, manifest["input_digest"], "宿主分析尚未完成；已有采集证据保留。")
    envelope = read_json(result_path)
    result = envelope["result"]
    if result.get("input_digest") != manifest["input_digest"] or digest(result) != envelope["result_digest"]:
        raise ValueError("result integrity check failed")
    if envelope["validated"]:
        checked = validate_assessment(read_json(child(run_dir, "assessment.json")), snapshot, manifest["input_digest"])
        if digest(checked) != digest(result):
            raise ValueError("result no longer matches the validated assessment")
    elif result != incomplete(snapshot, manifest["input_digest"], result["synthesis"]["reason"]):
        raise ValueError("unvalidated result may not contain conclusions")
    return result


def report_run(run_dir: Path) -> dict:
    with run_lock(run_dir):
        snapshot, manifest = load_snapshot(run_dir)
        result = _result(run_dir, snapshot, manifest)
        atomic_text(child(run_dir, "report.md"), render(snapshot, result))
    return {"report_file": str(run_dir / "report.md"), "analysis_status": result["synthesis"]["status"],
            "direction": result["synthesis"]["direction"]}


def validate_report(run_dir: Path, response_file: Path) -> dict:
    with run_lock(run_dir):
        snapshot, manifest = load_snapshot(run_dir)
        result = _result(run_dir, snapshot, manifest)
        reply = response_file.read_text(encoding="utf-8-sig")
        receipt = validate_reply(reply, render(snapshot, result))
        receipt.update({"input_digest": manifest["input_digest"], "response_digest": digest(reply), "result_digest": digest(result)})
        write_json(child(run_dir, "delivery.json"), receipt)
    return receipt
