"""As-filed core SEC XBRL facts, preserving taxonomy, unit, period and accession."""

import json
import math
import re
from datetime import date

from ..common.time import iso, known_by
from .base import identifier, interval, record, result
from .sec import _cik, _identity
from .transport import SourceError

# Each tag retains its own basis. Components/alternative tags are never added or
# substituted for consolidated totals, and no ratios/TTM/FCF are calculated here.
_TAGS = {
    "us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
        "RevenueFromContractWithCustomerIncludingAssessedTax": "revenue",
        "Revenues": "revenue", "SalesRevenueNet": "revenue",
        "NetIncomeLoss": "net_income", "ProfitLoss": "net_income",
        "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
        "PaymentsToAcquirePropertyPlantAndEquipment": "capital_expenditure",
        "CashAndCashEquivalentsAtCarryingValue": "cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "cash",
        "LongTermDebt": "debt", "LongTermDebtCurrent": "debt", "LongTermDebtNoncurrent": "debt",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": "debt",
        "ShortTermBorrowings": "debt", "DebtCurrent": "debt", "CommercialPaper": "debt",
        "EarningsPerShareBasic": "eps", "EarningsPerShareDiluted": "eps",
        "WeightedAverageNumberOfSharesOutstandingBasic": "shares",
        "WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
    },
    "ifrs-full": {
        "Revenue": "revenue", "ProfitLoss": "net_income", "ProfitLossAttributableToOwnersOfParent": "net_income",
        "CashFlowsFromUsedInOperatingActivities": "operating_cash_flow",
        "PurchaseOfPropertyPlantAndEquipment": "capital_expenditure",
        "CashAndCashEquivalents": "cash", "Borrowings": "debt", "CurrentBorrowings": "debt", "NoncurrentBorrowings": "debt",
        "BasicEarningsLossPerShare": "eps", "DilutedEarningsLossPerShare": "eps",
        "WeightedAverageNumberOfOrdinarySharesOutstanding": "shares",
        "AdjustedWeightedAverageShares": "shares",
    },
}
_FORMS = {"10-K", "10-Q", "20-F", "6-K"}


def _period(raw):
    end = date.fromisoformat(raw["end"])
    if not raw.get("start"):
        return "instant"
    start = date.fromisoformat(raw["start"])
    duration = (end - start).days + 1
    if duration < 1:
        raise ValueError("sec_fact_reversed_period")
    frame = raw.get("frame", "")
    if not isinstance(frame, str):
        raise TypeError("sec_fact_frame_invalid")
    if re.fullmatch(r"CY\d{4}Q[1-4]", frame) and 70 <= duration <= 110:
        return "quarter"
    if 350 <= duration <= 380 and (re.fullmatch(r"CY\d{4}", frame) or raw.get("fp") == "FY"):
        return "annual"
    # An fp=Q2/Q3 label is not a standalone-quarter receipt. Without an explicit
    # quarter frame, retain the reported duration as cumulative, not TTM input.
    return "cumulative"


def _currency(metric, unit):
    if metric == "shares" and unit == "shares":
        return "N/A"  # Explicitly currencyless, never the request's quote currency.
    pattern = r"([A-Z]{3})/shares" if metric == "eps" else r"([A-Z]{3})"
    match = re.fullmatch(pattern, unit)
    if not match:
        raise ValueError("sec_fact_unit_not_supported_without_conversion")
    return match.group(1)


def _fact(raw, *, taxonomy, tag, metadata, metric, unit, cik, symbol, as_of):
    if not isinstance(raw, dict) or type(raw.get("val")) not in {int, float}:
        raise ValueError("sec_fact_numeric_value_missing")
    if isinstance(raw["val"], float) and not math.isfinite(raw["val"]):
        raise ValueError("sec_fact_nonfinite_value")
    accession = raw.get("accn")
    if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("sec_fact_accession_invalid")
    filed, period_end = raw.get("filed"), raw.get("end")
    if not isinstance(filed, str) or not isinstance(period_end, str):
        raise TypeError("sec_fact_dates_missing")
    date.fromisoformat(filed)
    period_date = date.fromisoformat(period_end)
    if not known_by(filed, as_of, precision="date", timezone_name="America/New_York") or period_date > date.fromisoformat(filed):
        return None
    period_kind = _period(raw)
    currency = _currency(metric, unit)
    label = metadata.get("label", tag)
    if not isinstance(label, str):
        raise TypeError("sec_fact_label_invalid")
    excerpt = {"taxonomy": taxonomy, "tag": tag, "label": label, "unit": unit, "fact": raw}
    item_id = f"{taxonomy}:{tag}:{accession}:{raw.get('start', 'instant')}:{period_end}:{unit}:{identifier(raw)}"
    row = record(
        "sec_companyfacts", item_id, title=f"{symbol}: {label} ({raw['form']}, {period_end})",
        url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", published_at=filed,
        data_type="financial", source_kind="official", timezone="America/New_York", entities=[symbol],
        text=json.dumps(excerpt, ensure_ascii=False, sort_keys=True, allow_nan=False), text_scope="official_excerpt",
        permissions={"store_text": True, "retention_days": 120}, origin_id=f"sec:{accession}",
        evidence_locator=f"facts/{taxonomy}/{tag}/units/{unit}; accession={accession}; start={raw.get('start', '')}; end={period_end}; filed={filed}",
        confirmed_numbers=[{"value": raw["val"], "unit": unit, "subject": label}],
    )
    fact = {
        "metric": metric, "value": raw["val"], "unit": unit, "currency": currency,
        "period_end": period_end, "period_kind": period_kind, "disclosed_at": filed,
        "accounting_basis": "US GAAP" if taxonomy == "us-gaap" else "IFRS",
        "accession": accession, "basis": f"{taxonomy}:{tag}; reported {raw['form']}; no aggregation or unit conversion",
        "subject": label, "record_refs": [{"source_id": "sec_companyfacts", "source_item_id": item_id}],
    }
    if raw.get("start"):
        fact["period_start"] = raw["start"]
    if metric == "capital_expenditure" and raw["val"] >= 0:
        # These specifically named acquisition-payment tags report positive outflows.
        # A negative/reversal value is not assigned a sign convention automatically.
        fact["capex_sign"] = "positive_outflow"
    return row, fact


def collect(client, item, start, end):
    headers = _identity()
    cik = _cik(item.get("cik"))
    payload = client.json("data.sec.gov", f"/api/xbrl/companyfacts/CIK{cik}.json", headers=headers)
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict) or _cik(payload.get("cik")) != cik:
        raise SourceError("sec_companyfacts_invalid_issuer_or_payload")
    taxonomies = payload["facts"]
    available = [name for name in _TAGS if isinstance(taxonomies.get(name), dict)]
    if not available:
        raise SourceError("sec_companyfacts_supported_taxonomies_unavailable", unavailable=True)
    rows, financial_facts, skipped = [], [], set()
    seen, observed_forms, observed_metrics = set(), set(), set()
    as_of = iso(end)
    for taxonomy in available:
        for tag, metric in _TAGS[taxonomy].items():
            metadata = taxonomies[taxonomy].get(tag)
            if metadata is None:
                continue
            if not isinstance(metadata, dict) or not isinstance(metadata.get("units"), dict):
                skipped.add("sec_fact_units_missing")
                continue
            for unit, raw_values in metadata["units"].items():
                if not isinstance(unit, str) or not isinstance(raw_values, list):
                    skipped.add("sec_fact_units_invalid")
                    continue
                for raw in raw_values:
                    try:
                        if not isinstance(raw, dict) or not isinstance(raw.get("form"), str):
                            raise TypeError("sec_fact_form_missing")
                        if raw["form"].removesuffix("/A") not in _FORMS:
                            continue
                        built = _fact(raw, taxonomy=taxonomy, tag=tag, metadata=metadata, metric=metric, unit=unit,
                                      cik=cik, symbol=item["symbol"], as_of=as_of)
                        if built is None:
                            continue
                        row, fact = built
                        if row["source_item_id"] not in seen:
                            rows.append(row)
                            financial_facts.append(fact)
                            seen.add(row["source_item_id"])
                            observed_forms.add(raw["form"])
                            observed_metrics.add(metric)
                    except (ValueError, TypeError, KeyError) as exc:
                        skipped.add(str(exc) if str(exc).startswith("sec_fact_") else "sec_fact_invalid_value")
    limitations = [
        "SEC companyfacts contains only tagged core facts actually supplied in us-gaap/ifrs-full and 10-K/10-Q/20-F/6-K (including amendments); no issuer-extension or untagged 6-K completeness claim.",
        "Financial history is independent of the news window. Each accession/period/tag/unit remains separate; debt components, restricted cash and alternative revenue tags are not aggregated.",
        "Filed dates have no exact release time; a date must be fully past in America/New_York to be admitted. Unframed durations remain cumulative, never assumed standalone quarters.",
        "SEC structured numeric excerpts are retained, not issuer report prose. Response size and shared request budget still apply.",
        "Observed supported taxonomies: " + ", ".join(available) + "; admitted forms: " + (", ".join(sorted(observed_forms)) or "none") + ".",
    ]
    missing = {"revenue", "net_income", "operating_cash_flow", "capital_expenditure", "cash", "debt", "eps"} - observed_metrics
    if missing:
        limitations.append("Core facts not supplied/admitted for: " + ", ".join(sorted(missing)) + ".")
    if skipped:
        limitations.append("Malformed or unsupported fact rows were not replaced: " + ", ".join(sorted(skipped)) + ".")
    output = result(rows, successful=[] if skipped else [interval(start, end)],
                    failed=[interval(start, end, "; ".join(sorted(skipped)))] if skipped else [],
                    exhausted=not skipped, limitations=limitations)
    output["financial_facts"] = financial_facts
    return output
