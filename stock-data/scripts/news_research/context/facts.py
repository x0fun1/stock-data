"""Citable accounting facts and company-specific macro exposures, not trading signals."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from ..common.jsonio import digest
from ..common.time import known_by, parse_time
from ..contracts import object_keys, string_list, text

METRICS = {"revenue", "net_income", "operating_cash_flow", "capital_expenditure", "free_cash_flow", "cash", "debt",
           "eps", "shares", "market_cap", "price", "pe", "ps", "operating_data"}


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("financial value must be numeric") from exc
    if not result.is_finite():
        raise ValueError("financial value must be finite")
    return result


def _references(item: dict, record_index: dict, evidence: dict) -> list[str]:
    refs = list(item.get("evidence_ids", []))
    string_list(refs, "evidence_ids")
    for ref in item.get("record_refs", []):
        object_keys(ref, {"source_id", "source_item_id"}, {"source_id", "source_item_id"}, "record reference")
        refs.extend(record_index.get((ref["source_id"], str(ref["source_item_id"])), []))
    refs = sorted(set(refs))
    if not refs or any(ref not in evidence or not evidence[ref]["eligible"] for ref in refs):
        raise ValueError("context needs admitted, as-of eligible evidence")
    return refs


def build_context(raw: dict, records: list[dict], evidence_items: list[dict], request: dict) -> dict:
    evidence = {item["evidence_id"]: item for item in evidence_items}
    record_index = {}
    for record in records:
        record_index.setdefault((record["source_id"], record["source_item_id"]), []).append("ev-" + record["record_id"][4:])
    facts = []
    exposures = []
    gaps = []
    for item in raw.get("financial_facts", []):
        try:
            allowed = {"fact_id", "metric", "value", "unit", "currency", "period_start", "period_end", "period_kind",
                       "disclosed_at", "accounting_basis", "accession", "basis", "evidence_ids", "record_refs", "capex_sign", "subject"}
            required = {"metric", "value", "unit", "currency", "period_end", "period_kind", "disclosed_at", "accounting_basis", "basis"}
            object_keys(item, allowed, required, "financial fact")
            for field in ("unit", "currency", "accounting_basis", "basis"):
                text(item[field], f"financial.{field}")
            if item.get("period_start") and date.fromisoformat(item["period_start"]) > date.fromisoformat(item["period_end"]):
                raise ValueError("financial period starts after its end")
            if item["metric"] not in METRICS or item["period_kind"] not in {"quarter", "cumulative", "annual", "instant", "disclosed_ttm"}:
                raise ValueError("unsupported metric or period kind")
            if not known_by(item["disclosed_at"], request["as_of"]):
                raise ValueError("financial disclosure not known by as_of")
            if date.fromisoformat(item["period_end"]) > parse_time(request["as_of"]).date():
                raise ValueError("financial period ends after as_of")
            value = number(item["value"])
            refs = _references(item, record_index, evidence)
            supported = any(str(item["value"]) in evidence[ref]["quote"] and str(item["unit"]) in evidence[ref]["quote"] for ref in refs)
            if not supported:
                raise ValueError("financial value/unit not found together in cited evidence")
            if item.get("accession"):
                accession = item["accession"]
                if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not any(
                        accession in evidence[ref]["quote"] for ref in refs):
                    raise ValueError("financial accession is malformed or absent from cited evidence")
            if item["metric"] in {"pe", "ps"}:
                raise ValueError("import numerator and denominator facts; use explicit ratio validation, not an unverified provider ratio")
            fact = {**item, "value": str(value), "evidence_ids": refs}
            fact.pop("record_refs", None)
            fact["fact_id"] = "fin-" + digest(fact)[:24]
            facts.append(fact)
        except (ValueError, TypeError, KeyError) as exc:  # noqa: PERF203 - isolate invalid context items and preserve usable evidence
            gaps.append({"domain": "financial", "reason": str(exc)})
    for item in raw.get("exposures", []):
        try:
            allowed = {"exposure_id", "target", "kind", "description", "effective_at", "updated_at", "evidence_ids", "record_refs"}
            object_keys(item, allowed, {"target", "kind", "description", "effective_at", "updated_at"}, "exposure")
            if item["target"] != request["symbol"]:
                raise ValueError("exposure belongs to a different target")
            if not known_by(item["effective_at"], request["as_of"]) or not known_by(item["updated_at"], request["as_of"]):
                raise ValueError("exposure not effective/known by as_of")
            if item["kind"] not in {"industry", "region", "raw_material", "interest_rate", "fx", "etf_holdings"}:
                raise ValueError("unsupported exposure kind")
            refs = _references(item, record_index, evidence)
            description = text(item["description"], "exposure.description")
            if not any(description in evidence[ref]["quote"] for ref in refs):
                raise ValueError("exposure description must be an exact passage from its source")
            exposure = {**item, "evidence_ids": refs}
            exposure.pop("record_refs", None)
            exposure["exposure_id"] = "exp-" + digest(exposure)[:24]
            exposures.append(exposure)
        except (ValueError, TypeError, KeyError) as exc:  # noqa: PERF203 - isolate invalid context items and preserve usable evidence
            gaps.append({"domain": "exposure", "reason": str(exc)})
    if not facts:
        gaps.append({"domain": "financial", "reason": "no validated financial facts; US GAAP/IFRS and annual/interim comparability not established"})
    if not exposures:
        gaps.append({"domain": "exposure", "reason": "no dated company/ETF exposure evidence; macro cannot determine target-specific direction"})
    return {"financial_facts": facts, "exposures": exposures, "gaps": gaps}


def ttm(quarters: list[dict]) -> dict:
    if len(quarters) != 4 or any(item["period_kind"] != "quarter" for item in quarters):
        raise ValueError("TTM requires four standalone quarters, never cumulative quarter totals")
    ordered = sorted(quarters, key=lambda item: item["period_start"])
    if any(not 70 <= (date.fromisoformat(item["period_end"]) - date.fromisoformat(item["period_start"])).days + 1 <= 110 for item in ordered):
        raise ValueError("TTM requires genuine standalone fiscal-quarter periods")
    for field in ("metric", "currency", "unit", "accounting_basis", "basis"):
        if len({item[field] for item in ordered}) != 1:
            raise ValueError(f"TTM inconsistent {field}")
    for left, right in pairwise(ordered):
        if date.fromisoformat(left["period_end"]) + timedelta(days=1) != date.fromisoformat(right["period_start"]):
            raise ValueError("TTM overlapping or missing periods")
    total_days = (date.fromisoformat(ordered[-1]["period_end"]) - date.fromisoformat(ordered[0]["period_start"])).days + 1
    if not 350 <= total_days <= 380:
        raise ValueError("TTM periods do not represent one fiscal year")
    return {"value": str(sum((number(item["value"]) for item in ordered), Decimal(0))), "period_kind": "ttm",
            "period_start": ordered[0]["period_start"], "period_end": ordered[-1]["period_end"],
            "currency": ordered[0]["currency"], "unit": ordered[0]["unit"], "formula": "sum(four standalone quarters)",
            "component_ids": [item["fact_id"] for item in ordered]}


def free_cash_flow(cfo: dict, capex: dict) -> dict:
    for field in ("period_start", "period_end", "currency", "unit", "accounting_basis", "period_kind", "basis"):
        if cfo.get(field) != capex.get(field):
            raise ValueError(f"free cash flow inconsistent {field}")
    if cfo["metric"] != "operating_cash_flow" or capex["metric"] != "capital_expenditure":
        raise ValueError("free cash flow requires operating cash flow and capital expenditure")
    sign = capex.get("capex_sign")
    amount = number(capex["value"])
    if sign not in {"positive_outflow", "negative_outflow"} or (sign == "positive_outflow" and amount < 0) or (sign == "negative_outflow" and amount > 0):
        raise ValueError("capital expenditure sign must be explicit and consistent")
    return {"value": str(number(cfo["value"]) - abs(amount)), "formula": "operating_cash_flow - abs(capital_expenditure)",
            "component_ids": [cfo["fact_id"], capex["fact_id"]], "currency": cfo["currency"], "unit": cfo["unit"]}


def ratio(numerator: dict, denominator: dict, *, kind: str) -> dict:
    if kind not in {"pe", "ps"}:
        raise ValueError("unsupported ratio")
    if numerator["currency"] != denominator["currency"] or numerator["unit"] != denominator["unit"]:
        raise ValueError("ratio requires same currency and unit; no implicit FX conversion")
    if numerator["metric"] != "market_cap" or denominator["metric"] != ("net_income" if kind == "pe" else "revenue"):
        raise ValueError("ratio numerator/denominator mismatch")
    if denominator["period_kind"] not in {"annual", "ttm", "disclosed_ttm"}:
        raise ValueError("ratio needs annual or validated TTM denominator")
    if number(denominator["value"]) <= 0:
        return {"value": None, "reason": "nonpositive denominator; ratio not meaningful"}
    if number(numerator["value"]) <= 0:
        raise ValueError("market capitalization must be positive")
    return {"value": str(number(numerator["value"]) / number(denominator["value"])), "basis": denominator["period_kind"],
            "market_cap_at": numerator["disclosed_at"], "report_period": denominator["period_end"],
            "formula": f"market_cap / {denominator['metric']}", "component_ids": [numerator["fact_id"], denominator["fact_id"]]}
