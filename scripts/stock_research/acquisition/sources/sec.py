"""SEC companyfacts/submissions adapter.

The SEC filing-date fields do not provide acceptance times in companyfacts.
Facts therefore carry ``available_at`` at the end of the filing date with an
explicit ``date_only_estimated`` quality flag; they remain candidates until a
submission-level acceptance time is matched.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from ...common.errors import DataError
from ...common.hashing import sha256_bytes
from ...common.timeutil import now_utc, parse_date
from ...contracts import AcquisitionReceipt, FinancialFact

UTC = timezone.utc
BASE = "https://data.sec.gov"


def _cik(cik: int | str) -> str:
    text = str(cik).strip().lstrip("0")
    return text.zfill(10) if text else ""


def _filed_datetime(filed: str | None) -> datetime | None:
    if not filed:
        return None
    return datetime.combine(parse_date(filed), time(23, 59), tzinfo=UTC)


def parse_companyfacts(
    data: dict,
    security_id: str,
    cik: str,
    source_id: str = "sec",
    received_at: datetime | None = None,
) -> list[FinancialFact]:
    if "facts" not in data or not isinstance(data.get("facts"), dict):
        raise DataError("SEC companyfacts payload has no facts object")
    facts = data["facts"]
    out: list[FinancialFact] = []
    for taxonomy, tags in facts.items():
        if not isinstance(tags, dict):
            continue
        for tag, body in tags.items():
            if not isinstance(body, dict):
                continue
            units = body.get("units") or {}
            if not isinstance(units, dict):
                continue
            for unit, records in units.items():
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict) or record.get("val") is None:
                        continue
                    try:
                        value = float(record["val"])
                    except (TypeError, ValueError):
                        continue
                    if record.get("end") is None:
                        continue
                    filed = _filed_datetime(record.get("filed"))
                    out.append(
                        FinancialFact(
                            security_id=security_id,
                            concept=str(tag),
                            value=value,
                            unit=str(unit),
                            period_end=parse_date(record["end"]),
                            period_start=parse_date(record["start"]) if record.get("start") else None,
                            fiscal_year=int(record["fy"]) if record.get("fy") is not None else None,
                            fiscal_period=str(record.get("fp", "")),
                            form=str(record.get("form", "")),
                            accession=str(record.get("accn", "")),
                            filed_at=filed,
                            available_at=filed,
                            source_id=source_id,
                            quality="date_only_estimated" if filed else "availability_unknown",
                        )
                    )
    return out


class SecClient:
    def __init__(self, transport, contact: str) -> None:
        contact = (contact or "").strip()
        if not contact:
            raise ValueError("SEC requests require a contact string in User-Agent")
        self.transport = transport
        self.headers = {"User-Agent": f"stock-research-skill {contact}", "Accept-Encoding": "gzip, deflate"}

    def _get(self, path: str, dataset: str) -> tuple[dict, AcquisitionReceipt]:
        result = self.transport.get(BASE + path, headers=self.headers, expect="json")
        receipt = AcquisitionReceipt(
            source_id="sec",
            dataset=dataset,
            subrequest=path,
            requested_at=result.fetched_at,
            received_at=result.fetched_at,
            status=result.status,
            http_status=result.http_status,
            content_type=result.content_type,
            byte_count=len(result.content),
            sha256=sha256_bytes(result.content),
            error=result.error,
            exhausted=result.ok,
            params_redacted={},
            record_count=0,
        )
        if not result.ok:
            raise DataError(f"SEC {dataset} request failed: {result.error}")
        return result.data, receipt

    def companyfacts(self, cik: int | str, security_id: str) -> tuple[list[FinancialFact], AcquisitionReceipt]:
        packed, receipt = self._get(f"/api/xbrl/companyfacts/CIK{_cik(cik)}.json", "financial_facts")
        facts = parse_companyfacts(packed, security_id, _cik(cik), received_at=receipt.received_at)
        receipt.record_count = len(facts)
        if facts:
            receipt.coverage_start = min(f.period_end for f in facts)
            receipt.coverage_end = max(f.period_end for f in facts)
        return facts, receipt

    def submissions(self, cik: int | str) -> tuple[list[dict], AcquisitionReceipt]:
        data, receipt = self._get(f"/submissions/CIK{_cik(cik)}.json", "filings")
        recent = (data.get("filings") or {}).get("recent") or {}
        columns = recent.keys()
        rows = []
        if columns:
            length = len(recent.get("accessionNumber", []))
            for i in range(length):
                row = {key: (recent[key][i] if i < len(recent[key]) else None) for key in columns}
                rows.append(row)
        receipt.record_count = len(rows)
        dates = [r.get("filingDate") for r in rows if r.get("filingDate")]
        if dates:
            receipt.coverage_start = min(parse_date(d) for d in dates)
            receipt.coverage_end = max(parse_date(d) for d in dates)
        return rows, receipt
