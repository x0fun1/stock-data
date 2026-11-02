"""EDGAR submissions archive traversal; filing metadata, never invented filing text."""

import os
import re
from datetime import datetime

from ..common.time import iso, parse_time
from .base import in_window, interval, record, result
from .transport import SourceError


def _identity() -> dict:
    value = os.environ.get("SEC_USER_AGENT", "").strip()
    if not value:
        contact = os.environ.get("SEC_CONTACT", "").strip()
        value = "stock-data-news " + contact if contact else ""
    if not value or "@" not in value or any(ord(char) < 32 for char in value) or len(value) > 256:
        raise SourceError("sec_user_identification_not_configured", unavailable=True)
    return {"User-Agent": value}


def _cik(value):
    value = str(value or "")
    if not re.fullmatch(r"\d{1,10}", value) or int(value) <= 0:
        raise SourceError("sec_cik_required", unavailable=True)
    return value.zfill(10)


def _rows(data: dict, cik: str, symbol: str, start, end):
    fields = ("accessionNumber", "filingDate", "form", "primaryDocument")
    if not isinstance(data, dict) or any(not isinstance(data.get(field), list) for field in fields):
        raise SourceError("sec_invalid_submissions_arrays")
    size = len(data["accessionNumber"])
    if any(len(data[field]) != size for field in fields):
        raise SourceError("sec_inconsistent_submissions_arrays")
    rows = []
    accepted = data.get("acceptanceDateTime", [])
    if not isinstance(accepted, list):
        raise SourceError("sec_invalid_acceptance_times")
    for index in range(size):
        accession = data["accessionNumber"][index]
        date = data["filingDate"][index]
        if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise SourceError("sec_invalid_accession")
        publication = date
        if index < len(accepted) and accepted[index]:
            try:
                publication = iso(parse_time(accepted[index]))
            except (ValueError, TypeError):
                # Older files can omit timezone. Do not pretend that value is UTC.
                publication = date
        if not in_window(publication, start, end):
            continue
        document = data["primaryDocument"][index]
        if (not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", document)
                or any(segment in {".", ".."} for segment in document.split("/"))):
            raise SourceError("sec_invalid_primary_document")
        form = str(data["form"][index])
        descriptions = data.get("primaryDocDescription", [])
        description = str(descriptions[index]) if isinstance(descriptions, list) and index < len(descriptions) else ""
        rows.append(record(
            "sec_submissions", accession,
            title=f"{symbol}: {form}" + (f" — {description}" if description else ""),
            url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}",
            published_at=publication, entities=[symbol], origin_id=f"sec:{accession}",
            event_key=f"sec:{accession}", permissions={"store_text": False, "retention_days": 120},
        ))
    return rows


def collect(client, item, start, end):
    headers = _identity()
    cik = _cik(item.get("cik"))
    symbol = item["symbol"]
    payload = client.json("data.sec.gov", f"/submissions/CIK{cik}.json", headers=headers)
    if not isinstance(payload, dict) or not isinstance(payload.get("filings"), dict):
        raise SourceError("sec_invalid_submissions_response")
    filings = payload["filings"]
    rows = _rows(filings.get("recent"), cik, symbol, start, end)
    archives = filings.get("files")
    if not isinstance(archives, list):
        raise SourceError("sec_missing_historical_file_index")
    for index, archive in enumerate(archives):
        if not isinstance(archive, dict):
            raise SourceError("sec_invalid_historical_file_index")
        name = archive.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", name):
            raise SourceError("sec_invalid_historical_filename")
        try:
            # Date boundaries are intentionally inclusive to cover timestamp/date ambiguity.
            file_start = datetime.strptime(archive["filingFrom"], "%Y-%m-%d").date()
            file_end = datetime.strptime(archive["filingTo"], "%Y-%m-%d").date()
            if file_start > file_end:
                raise SourceError("sec_reversed_historical_file_dates")
            if file_end < start.date() or file_start > end.date():
                continue
            historical = client.json("data.sec.gov", f"/submissions/{name}", headers=headers)
            rows.extend(_rows(historical, cik, symbol, start, end))
        except (SourceError, KeyError, TypeError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, SourceError) else "sec_invalid_historical_file"
            return result(rows, failed=[interval(start, end, reason)], cap_reached=getattr(exc, "cap_reached", False),
                          cursor={"historical_file_index": index, "name": name})
    return result(rows, successful=[interval(start, end)], exhausted=True,
                  limitations=["SEC filing metadata does not contain filing-body evidence; companyfacts is a separate financial input."])
