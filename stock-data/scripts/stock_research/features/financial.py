"""Point-in-time financial facts, quarterly derivation and fundamental factors.

Cumulative fiscal periods are decomposed into single quarters using only facts
that were available at the requested cutoff.  A later amendment never changes an
older as-of view.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from math import isfinite, log
from typing import Iterable, Mapping, Sequence

from ..common.timeutil import parse_date
from ..contracts import FeatureObservation, FinancialFact, MissingReason, SecurityIdentity
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries

CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
    "gross_profit": ("GrossProfit",),
    "assets": ("Assets",),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    # Totals and noncurrent balances are different scopes, not interchangeable
    # aliases.  debt_balance() handles a separately disclosed current portion.
    "debt": ("DebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebt"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "operating_income": ("OperatingIncomeLoss",),
    "interest_expense": ("InterestExpenseNonoperating", "InterestExpense", "InterestExpenseDebt"),
    "shares": ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
}

BANK_INDUSTRIES = {"bank", "banks", "insurance", "financial", "financial services"}
PRIOR_PERIOD_MATCH_DAYS = 14


def _normalise_concept(concept: str) -> str:
    return concept.split(":")[-1].split("_")[0] if ":" in concept else concept


def _knowledge_time(fact: FinancialFact) -> datetime | None:
    return fact.available_at or fact.filed_at


def _duration_days(fact: FinancialFact) -> int | None:
    if fact.period_start is None:
        return None
    return (fact.period_end - fact.period_start).days


def _fact_detail(fact: FinancialFact) -> dict:
    return {
        "concept": fact.concept,
        "value": fact.value,
        "unit": fact.unit,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat(),
        "accession": fact.accession,
        "available_at": _knowledge_time(fact).isoformat() if _knowledge_time(fact) else None,
        "filed_at": fact.filed_at.isoformat() if fact.filed_at else None,
        "source_id": fact.source_id,
        "quality": fact.quality,
    }


def _latest_component_time(facts: Sequence[FinancialFact]) -> datetime | None:
    times = [_knowledge_time(fact) for fact in facts]
    # A derived fact is never earlier than either operand.  Unknown availability
    # remains unknown instead of being converted to a fictitious early timestamp.
    return max(times) if times and all(t is not None for t in times) else None


@dataclass(slots=True)
class QuarterValue:
    period_start: date
    period_end: date
    value: float
    unit: str
    accession: str = ""
    available_at: datetime | None = None
    derived: str = "cumulative_difference"
    components: list[str] = field(default_factory=list)
    concept: str = ""
    component_facts: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class TTMValue:
    status: str
    value: float | None = None
    reason: str = ""
    quarters: list[QuarterValue] = field(default_factory=list)
    unit: str = ""
    period_start: date | None = None
    period_end: date | None = None
    method: str = ""
    components: list[dict] = field(default_factory=list)
    available_at: datetime | None = None
    quarter_sum: float | None = None
    reconciliation_difference: float | None = None
    expected_period_end: date | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "value": self.value,
            "reason": self.reason,
            "unit": self.unit,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "method": self.method,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "components": self.components,
            "quarter_sum": self.quarter_sum,
            "reconciliation_difference": self.reconciliation_difference,
            "expected_period_end": self.expected_period_end.isoformat() if self.expected_period_end else None,
            "quarters": [
                {
                    "period_start": q.period_start.isoformat(),
                    "period_end": q.period_end.isoformat(),
                    "value": q.value,
                    "accession": q.accession,
                    "derived": q.derived,
                    "unit": q.unit,
                    "concept": q.concept,
                    "available_at": q.available_at.isoformat() if q.available_at else None,
                    "components": q.components,
                    "component_facts": q.component_facts,
                }
                for q in self.quarters
            ],
        }


@dataclass(slots=True)
class DebtValue:
    status: str
    value: float | None = None
    unit: str = ""
    period_end: date | None = None
    debt_scope: str = ""
    method: str = ""
    reason: str = ""
    components: list[dict] = field(default_factory=list)
    available_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status, "debt": self.value, "unit": self.unit,
            "debt_period_end": self.period_end.isoformat() if self.period_end else None,
            "debt_scope": self.debt_scope, "method": self.method, "reason": self.reason,
            "components": self.components,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "short_term_borrowings_coverage": "not_inferred_from_long_term_debt",
        }


class FinancialStore:
    def __init__(self, facts: Iterable[FinancialFact]) -> None:
        self.facts = list(facts)

    def _eligible(self, fact: FinancialFact, known_by: datetime | None) -> bool:
        if known_by is None:
            return True
        if fact.period_end > known_by.date():
            return False
        available = _knowledge_time(fact)
        return available is not None and available <= known_by

    def for_concepts(
        self,
        security_id: str,
        concepts: Sequence[str],
        known_by: datetime | None = None,
        instant: bool | None = None,
    ) -> list[FinancialFact]:
        wanted = {c.split(":")[-1] for c in concepts}
        out = []
        for fact in self.facts:
            if fact.security_id != security_id:
                continue
            if fact.concept.split(":")[-1] not in wanted:
                continue
            if instant is not None and fact.is_instant != instant:
                continue
            if not self._eligible(fact, known_by):
                continue
            out.append(fact)
        return out

    def latest_instant(
        self,
        security_id: str,
        concepts: Sequence[str],
        known_by: datetime | None = None,
        as_of: date | None = None,
    ) -> FinancialFact | None:
        facts = self.for_concepts(security_id, concepts, known_by=known_by, instant=True)
        facts = [f for f in facts if (as_of is None or f.period_end <= as_of) and isfinite(f.value)]
        if not facts:
            return None
        # Prefer an explicit semantic scope within the latest reported period.
        # A later filing of a restricted/noncurrent alternative is not an
        # amendment of the first concept in the caller's preference list.
        priority = {concept.split(":")[-1]: i for i, concept in enumerate(concepts)}
        return max(facts, key=lambda f: (
            f.period_end, -priority[f.concept.split(":")[-1]],
            _knowledge_time(f) or datetime.min.replace(tzinfo=timezone.utc), f.accession,
        ))

    def instant_history(
        self,
        security_id: str,
        concepts: Sequence[str],
        known_by: datetime | None = None,
    ) -> list[FinancialFact]:
        facts = self.for_concepts(security_id, concepts, known_by=known_by, instant=True)
        priority = {concept.split(":")[-1]: i for i, concept in enumerate(concepts)}
        preference = lambda f: (-priority[f.concept.split(":")[-1]],
                                _knowledge_time(f) or datetime.min.replace(tzinfo=timezone.utc), f.accession)
        chosen: dict[date, FinancialFact] = {}
        for fact in facts:
            if not isfinite(fact.value):
                continue
            existing = chosen.get(fact.period_end)
            if existing is None or preference(fact) > preference(existing):
                chosen[fact.period_end] = fact
        return [chosen[d] for d in sorted(chosen)]

    def debt_balance(
        self, security_id: str, known_by: datetime | None = None, as_of: date | None = None,
    ) -> DebtValue:
        """Use disclosed totals, or an explicit matched long-term-debt pair.

        DebtCurrent can include short-term borrowings; DebtSecuritiesCurrent is
        an asset.  Neither may be silently added to LongTermDebt, which already
        includes the current portion of long-term debt.
        """
        names = (*CONCEPTS["debt"], "LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent")
        facts = self.for_concepts(security_id, names, known_by=known_by, instant=True)
        facts = [f for f in facts if (as_of is None or f.period_end <= as_of) and isfinite(f.value)]
        if not facts:
            return DebtValue("missing", reason=MissingReason.NO_DATA.value)
        latest = max(f.period_end for f in facts)
        current = [f for f in facts if f.period_end == latest]

        def at_period(concept: str) -> FinancialFact | None:
            matching = [f for f in current if f.concept.split(":")[-1] == concept]
            if not matching:
                return None
            return max(matching, key=lambda f: (
                _knowledge_time(f) or datetime.min.replace(tzinfo=timezone.utc), f.accession,
            ))

        scopes = {
            "DebtAndCapitalLeaseObligations": "debt_and_capital_lease_obligations",
            "LongTermDebtAndCapitalLeaseObligations": "long_term_debt_and_capital_leases_including_current_portion",
            "LongTermDebt": "long_term_debt_including_current_portion",
        }
        for concept in CONCEPTS["debt"]:
            total = at_period(concept)
            if total is not None:
                if len({f.unit for f in current if f.concept.split(":")[-1] == concept}) != 1:
                    return DebtValue("missing", period_end=latest, reason="ambiguous_units",
                                     components=[_fact_detail(f) for f in current])
                return DebtValue(
                    "ok", total.value, total.unit, latest, scopes[concept],
                    "disclosed_total", components=[_fact_detail(total)],
                    available_at=_knowledge_time(total),
                )
        noncurrent, due_current = at_period("LongTermDebtNoncurrent"), at_period("LongTermDebtCurrent")
        components = [f for f in (noncurrent, due_current) if f is not None]
        if noncurrent is None or due_current is None:
            return DebtValue("missing", period_end=latest, reason="incomplete_debt_scope",
                             debt_scope="long_term_debt_total_not_reconstructed",
                             components=[_fact_detail(f) for f in current])
        if noncurrent.unit != due_current.unit or any(len({f.unit for f in current if f.concept.split(":")[-1] == name}) != 1
                for name in ("LongTermDebtNoncurrent", "LongTermDebtCurrent")):
            return DebtValue("missing", period_end=latest, reason="mixed_units",
                             components=[_fact_detail(f) for f in components])
        return DebtValue(
            "ok", noncurrent.value + due_current.value, noncurrent.unit, latest,
            "long_term_debt_including_current_portion", "sum_noncurrent_and_current_long_term_debt",
            components=[_fact_detail(f) for f in components],
            available_at=_latest_component_time(components),
        )

    def duration_facts(
        self,
        security_id: str,
        concepts: Sequence[str],
        known_by: datetime | None = None,
    ) -> list[FinancialFact]:
        return self.for_concepts(security_id, concepts, known_by=known_by, instant=False)

    def _duration_stream(
        self, security_id: str, concepts: Sequence[str], known_by: datetime | None,
        as_of: date | None = None,
    ) -> tuple[list[FinancialFact], str]:
        facts = [f for f in self.duration_facts(security_id, concepts, known_by=known_by)
                 if f.period_start is not None and 60 <= (_duration_days(f) or 0) <= 410
                 and isfinite(f.value) and (as_of is None or f.period_end <= as_of)]
        if not facts:
            return [], MissingReason.NO_DATA.value
        latest = max(f.period_end for f in facts)
        present = {f.concept.split(":")[-1] for f in facts if f.period_end == latest}
        concept = next(c.split(":")[-1] for c in concepts if c.split(":")[-1] in present)
        facts = [f for f in facts if f.concept.split(":")[-1] == concept]
        latest_units = {f.unit for f in facts if f.period_end == latest}
        if len(latest_units) != 1:
            return [], "ambiguous_units"
        unit = next(iter(latest_units))
        facts = [f for f in facts if f.unit == unit]
        # Versions only supersede the same concept, unit and actual interval.
        # Tags with different financial scopes never become subtraction operands.
        chosen: dict[tuple[date, date], FinancialFact] = {}
        for fact in facts:
            key = (fact.period_start, fact.period_end)
            existing = chosen.get(key)
            version_key = lambda f: (_knowledge_time(f) or datetime.min.replace(tzinfo=timezone.utc), f.accession)
            if existing is None or version_key(fact) > version_key(existing):
                chosen[key] = fact
        return list(chosen.values()), ""

    def quarters(
        self,
        security_id: str,
        concepts: Sequence[str],
        known_by: datetime | None = None,
        as_of: date | None = None,
    ) -> list[QuarterValue]:
        facts, _ = self._duration_stream(security_id, concepts, known_by, as_of)
        if not facts:
            return []
        quarters: dict[date, QuarterValue] = {}

        def record(start: date, end: date, value: float, components: list[FinancialFact], method: str) -> None:
            source = components[-1]
            q = QuarterValue(start, end, value, source.unit, source.accession,
                             _latest_component_time(components), method,
                             list(dict.fromkeys(f.accession for f in components if f.accession)),
                             source.concept, [_fact_detail(f) for f in components])
            existing = quarters.get(end)
            # Explicit quarter facts take precedence, including when reported
            # cumulative figures differ by rounding or later comparative edits.
            priority = lambda item: (item.derived == "direct_quarter",
                                     item.available_at or datetime.min.replace(tzinfo=timezone.utc))
            if existing is None or priority(q) > priority(existing):
                quarters[end] = q

        for f in facts:
            if 60 <= (_duration_days(f) or 0) <= 110:
                record(f.period_start, f.period_end, f.value, [f], "direct_quarter")
        # Group by actual fiscal start, whether or not an annual fact exists yet.
        # Q3-Q1 is a two-quarter gap and must never be labelled a single quarter.
        starts = sorted({f.period_start for f in facts})
        for start in starts:
            cumulative = sorted((f for f in facts if f.period_start == start), key=lambda f: f.period_end)
            for previous, current in zip(cumulative, cumulative[1:]):
                q_start = previous.period_end + timedelta(days=1)
                if 60 <= (current.period_end - q_start).days <= 110:
                    record(q_start, current.period_end, current.value - previous.value,
                           [previous, current], "cumulative_difference")
        return [quarters[d] for d in sorted(quarters)]

    def ttm(
        self,
        security_id: str,
        concepts: Sequence[str],
        as_of: date,
        known_by: datetime | None = None,
    ) -> TTMValue:
        facts, reason = self._duration_stream(security_id, concepts, known_by, as_of)
        if not facts:
            return TTMValue("missing", reason=reason)
        latest = max(f.period_end for f in facts)
        quarters = self.quarters(security_id, concepts, known_by=known_by, as_of=as_of)
        last_four = quarters[-4:]
        quarter_reason = ""
        if len(last_four) < 4:
            quarter_reason = MissingReason.WINDOW_INSUFFICIENT.value
        for prev, curr in zip(last_four, last_four[1:]):
            if curr.period_start <= prev.period_end:
                quarter_reason = "overlapping_quarters"
            elif curr.period_start != prev.period_end + timedelta(days=1):
                quarter_reason = "non_consecutive_quarters"
        if not last_four or last_four[-1].period_end != latest:
            quarter_reason = "latest_period_not_reconstructed"
        quarter_sum = sum(q.value for q in last_four) if not quarter_reason else None

        def result(value: float, start: date, method: str, components: list[FinancialFact]) -> TTMValue:
            comparable_sum = quarter_sum if last_four and last_four[0].period_start == start else None
            return TTMValue(
                "ok", value, quarters=last_four, unit=components[-1].unit,
                period_start=start, period_end=latest, method=method,
                components=[_fact_detail(f) for f in components],
                available_at=_latest_component_time(components), quarter_sum=comparable_sum,
                reconciliation_difference=None if comparable_sum is None else value - comparable_sum,
            )

        annuals = [f for f in facts if 330 <= (_duration_days(f) or 0) <= 410]
        annual_at_latest = [f for f in annuals if f.period_end == latest]
        if annual_at_latest:
            annual = max(annual_at_latest, key=lambda f: _knowledge_time(f) or datetime.min.replace(tzinfo=timezone.utc))
            return result(annual.value, annual.period_start, "reported_annual", [annual])
        # This independently auditable reconstruction works for an unfinished
        # current fiscal year and keeps the known comparative version intact.
        current_candidates = [f for f in facts if f.period_end == latest and (_duration_days(f) or 0) < 330]
        for current in sorted(current_candidates, key=lambda f: f.period_start):
            previous_annuals = [f for f in annuals if f.period_end + timedelta(days=1) == current.period_start]
            for annual in sorted(previous_annuals, key=lambda f: f.period_end, reverse=True):
                prior_ytds = [f for f in facts if f.period_start == annual.period_start and f.period_end < annual.period_end]
                matched = _comparable_year_ago(current.period_end, (f.period_end for f in prior_ytds))
                prior_ytd = next((f for f in prior_ytds if f.period_end == matched), None)
                if prior_ytd is None:
                    continue
                if abs((annual.period_start - _one_year_before(current.period_start)).days) > PRIOR_PERIOD_MATCH_DAYS:
                    continue
                return result(annual.value + current.value - prior_ytd.value,
                              prior_ytd.period_end + timedelta(days=1),
                              "latest_annual_plus_current_ytd_minus_comparable_prior_ytd",
                              [annual, current, prior_ytd])
        if quarter_reason:
            return TTMValue("missing", reason=quarter_reason, quarters=last_four,
                            unit=facts[-1].unit, period_end=latest)
        component_map = { (f.concept, f.unit, f.period_start, f.period_end, f.accession): f
                          for f in facts }
        used = { (c["concept"], c["unit"], parse_date(c["period_start"]), parse_date(c["period_end"]), c["accession"])
                 for q in last_four for c in q.component_facts }
        components = [f for key, f in component_map.items() if key in used]
        return result(quarter_sum, last_four[0].period_start, "sum_four_consecutive_quarters", components)


def _obs_ok(name: str, value: float, cutoff: date, inputs: dict) -> FeatureObservation:
    return FeatureObservation(name=name, value=float(value), as_of=cutoff, inputs=inputs)


def _obs_miss(name: str, cutoff: date, reason: MissingReason | str, inputs: dict | None = None) -> FeatureObservation:
    return FeatureObservation(
        name=name,
        value=None,
        as_of=cutoff,
        status="missing",
        reason=reason.value if isinstance(reason, MissingReason) else str(reason),
        inputs=inputs or {},
    )


def _latest_close(prices: PriceSeries, cutoff: date) -> tuple[float | None, date | None]:
    sliced = prices.slice(cutoff)
    if not sliced.bars:
        return None, None
    bar = sliced.bars[-1]
    return bar.close, bar.date


def _one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _comparable_year_ago(current: date, periods: Iterable[date]) -> date | None:
    """Match an already-known anniversary, allowing 52/53-week fiscal calendars.

    SEC fact fiscal-year/period tags can describe the filing rather than the
    comparative fact.  A narrow date match is safer than silently substituting
    an earlier quarter when the anniversary moves by a few days.  Callers must
    first filter versions by their actual knowledge cutoff.
    """
    target = _one_year_before(current)
    candidates = [day for day in periods if day < current and
                  abs((day - target).days) <= PRIOR_PERIOD_MATCH_DAYS]
    return min(candidates, key=lambda day: (abs((day - target).days), day)) if candidates else None


def _same_ttm_window(*values: TTMValue) -> bool:
    return bool(values) and all(value.status == "ok" and value.period_start is not None
        and value.period_end is not None for value in values) and len({
            (value.period_start, value.period_end, value.unit) for value in values
        }) == 1


def compute_financial_features(
    identity: SecurityIdentity,
    store: FinancialStore,
    prices: PriceSeries,
    ledger: ActionLedger,
    cutoff: date | str,
    known_by=None,
) -> dict[str, FeatureObservation]:
    cutoff = parse_date(cutoff)
    if known_by is None:
        from ..common.timeutil import ensure_utc

        known_by = ensure_utc(datetime.combine(cutoff, datetime.max.time()))
    prices = prices.slice(cutoff, known_by=known_by)
    out: dict[str, FeatureObservation] = {}
    security_id = identity.security_id

    def ttm(concepts: Sequence[str], as_of: date | None = None) -> TTMValue:
        return store.ttm(security_id, concepts, as_of or cutoff, known_by=known_by)

    def instant(concepts: Sequence[str]) -> FinancialFact | None:
        return store.latest_instant(security_id, concepts, known_by=known_by, as_of=cutoff)

    def prior_instant(current: FinancialFact | None, concepts: Sequence[str]) -> FinancialFact | None:
        if current is None:
            return None
        history = [f for f in store.instant_history(security_id, concepts, known_by=known_by)
                   if f.unit == current.unit and f.period_end <= cutoff]
        matched = _comparable_year_ago(current.period_end, (f.period_end for f in history))
        return next((f for f in history if f.period_end == matched), None)

    def prior_ttm(current: TTMValue, concepts: Sequence[str]) -> TTMValue:
        if current.status != "ok" or current.period_end is None:
            return TTMValue("missing", reason=current.reason or MissingReason.NO_DATA.value)
        historical = store.duration_facts(security_id, concepts, known_by=known_by)
        matched = _comparable_year_ago(current.period_end,
            (f.period_end for f in historical if f.unit == current.unit and f.period_end <= cutoff))
        if matched is None:
            return TTMValue("missing", reason="comparable_year_ago_period_missing")
        previous = ttm(concepts, as_of=matched)
        if previous.status == "ok" and previous.unit != current.unit:
            return replace(previous, status="missing", value=None, reason="currency_mismatch")
        current_scopes = {f["concept"].split(":")[-1] for f in current.components}
        previous_scopes = {f["concept"].split(":")[-1] for f in previous.components}
        if previous.status == "ok" and current_scopes != previous_scopes:
            return replace(previous, status="missing", value=None, reason="financial_scope_mismatch")
        return previous

    flows = {name: ttm(CONCEPTS[name]) for name in
             ("revenue", "net_income", "cfo", "capex", "gross_profit", "operating_income", "interest_expense")}

    assets = instant(CONCEPTS["assets"])
    debt = store.debt_balance(security_id, known_by=known_by, as_of=cutoff)
    cash = instant(CONCEPTS["cash"])
    shares = instant(CONCEPTS["shares"])
    equity = instant(CONCEPTS["equity"])

    reported_periods = [value.period_end for value in flows.values() if value.period_end is not None]
    reported_periods.extend(f.period_end for f in (assets, equity) if f is not None)
    reporting_concepts = tuple(concept for name in flows for concept in CONCEPTS[name]) + CONCEPTS["assets"] + CONCEPTS["equity"]
    # An incomplete or ambiguous latest financial stream still establishes a
    # reported period.  It cannot silently make older complete streams current.
    reported_periods.extend(f.period_end for f in store.for_concepts(security_id, reporting_concepts, known_by=known_by)
        if f.period_end <= cutoff and isfinite(f.value) and (f.is_instant or 60 <= (_duration_days(f) or 0) <= 410))
    financial_period_end = max(reported_periods) if reported_periods else None
    for name, value in flows.items():
        value.expected_period_end = financial_period_end
        if value.status == "ok" and value.period_end != financial_period_end:
            flows[name] = replace(value, status="missing", value=None, reason="financial_period_mismatch")
        elif value.status == "ok" and value.unit != identity.currency:
            flows[name] = replace(value, status="missing", value=None, reason="currency_mismatch")
    revenue, net_income, cfo, capex, gross_profit, operating_income, interest = (
        flows[name] for name in ("revenue", "net_income", "cfo", "capex", "gross_profit", "operating_income", "interest_expense")
    )

    close, close_date = _latest_close(prices, cutoff)
    market_cap = None
    market_cap_detail: dict = {}
    if close is None or shares is None or close <= 0 or shares.value <= 0:
        market_cap_reason = MissingReason.NO_DATA.value
    else:
        q = ledger.cumulative_q(security_id, shares.period_end, cutoff, known_by=known_by)
        adjusted_shares = shares.value * q
        market_cap = close * adjusted_shares
        market_cap_detail = {
            "close": close,
            "close_date": close_date.isoformat() if close_date else None,
            "shares": shares.value,
            "shares_period_end": shares.period_end.isoformat(),
            "shares_available_at": (_knowledge_time(shares).isoformat() if _knowledge_time(shares) else None),
            "shares_quality": shares.quality,
            "split_factor": q,
            "market_cap": market_cap,
        }
        market_cap_reason = ""

    common = {"revenue_ttm": revenue.as_dict(), "net_income_ttm": net_income.as_dict(), "market_cap": market_cap_detail,
              "financial_period_end": financial_period_end.isoformat() if financial_period_end else None}

    if market_cap is not None and market_cap > 0:
        out["size_log"] = _obs_ok("size_log", log(market_cap), cutoff, market_cap_detail)
        if equity is not None and equity.unit == identity.currency:
            out["bp"] = _obs_ok("bp", equity.value / market_cap, cutoff, {**market_cap_detail, "equity": equity.value, "equity_period_end": equity.period_end.isoformat()})
        else:
            out["bp"] = _obs_miss("bp", cutoff, MissingReason.NO_DATA if equity is None else "currency_mismatch")
    else:
        out["size_log"] = _obs_miss("size_log", cutoff, market_cap_reason or MissingReason.NO_DATA, market_cap_detail)
        out["bp"] = _obs_miss("bp", cutoff, market_cap_reason or MissingReason.NO_DATA)

    if market_cap is None or market_cap <= 0:
        out["ep_ttm"] = _obs_miss("ep_ttm", cutoff, market_cap_reason or MissingReason.ZERO_DENOMINATOR, common)
        out["fcf_yield_ttm"] = _obs_miss("fcf_yield_ttm", cutoff, market_cap_reason or MissingReason.ZERO_DENOMINATOR, common)
    else:
        if net_income.status == "ok":
            out["ep_ttm"] = _obs_ok("ep_ttm", net_income.value / market_cap, cutoff, common)
        else:
            out["ep_ttm"] = _obs_miss("ep_ttm", cutoff, net_income.reason, common)
        cash_flow_inputs = {**common, "cfo_ttm": cfo.value, "capex_ttm": capex.value,
                            "cfo_window": cfo.as_dict(), "capex_window": capex.as_dict(), "capex_sign": "abs"}
        if _same_ttm_window(cfo, capex):
            capex_spend = abs(capex.value)
            out["fcf_yield_ttm"] = _obs_ok(
                "fcf_yield_ttm", (cfo.value - capex_spend) / market_cap, cutoff,
                cash_flow_inputs,
            )
        else:
            out["fcf_yield_ttm"] = _obs_miss("fcf_yield_ttm", cutoff,
                                           cfo.reason or capex.reason or "financial_period_mismatch", cash_flow_inputs)

    assets_prior = prior_instant(assets, CONCEPTS["assets"])
    avg_assets = (assets.value + assets_prior.value) / 2.0 if assets is not None and assets_prior is not None and assets.unit == identity.currency and assets.period_end == financial_period_end else None
    asset_base_reason = (MissingReason.NO_DATA.value if assets is None or assets_prior is None else
                         "currency_mismatch" if assets.unit != identity.currency else
                         "financial_period_mismatch" if assets.period_end != financial_period_end else
                         MissingReason.NONPOSITIVE_BASE.value if avg_assets is not None and avg_assets <= 0 else "")
    asset_inputs = {
        "assets": None if assets is None else assets.value,
        "assets_period_end": None if assets is None else assets.period_end.isoformat(),
        "assets_prior": None if assets_prior is None else assets_prior.value,
        "assets_prior_period_end": None if assets_prior is None else assets_prior.period_end.isoformat(),
        "prior_period_match": f"nearest_anniversary_within_{PRIOR_PERIOD_MATCH_DAYS}_days",
        "financial_period_end": financial_period_end.isoformat() if financial_period_end else None,
    }

    if assets is not None and assets_prior is not None and assets_prior.value > 0:
        out["asset_growth_yoy"] = _obs_ok("asset_growth_yoy", assets.value / assets_prior.value - 1.0, cutoff, asset_inputs)
    else:
        out["asset_growth_yoy"] = _obs_miss("asset_growth_yoy", cutoff, MissingReason.NO_DATA, asset_inputs)
    equity_prior = prior_instant(equity, CONCEPTS["equity"])
    avg_equity = (equity.value + equity_prior.value) / 2.0 if equity is not None and equity_prior is not None and equity.unit == identity.currency and equity.period_end == financial_period_end else None
    equity_base_reason = (MissingReason.NO_DATA.value if equity is None or equity_prior is None else
                          "currency_mismatch" if equity.unit != identity.currency else
                          "financial_period_mismatch" if equity.period_end != financial_period_end else
                          MissingReason.NONPOSITIVE_BASE.value if avg_equity is not None and avg_equity <= 0 else "")
    equity_inputs = {"equity": equity.value if equity else None, "equity_prior": equity_prior.value if equity_prior else None,
                     "equity_period_end": equity.period_end.isoformat() if equity else None,
                     "equity_prior_period_end": equity_prior.period_end.isoformat() if equity_prior else None,
                     "financial_period_end": financial_period_end.isoformat() if financial_period_end else None,
                     "prior_period_match": f"nearest_anniversary_within_{PRIOR_PERIOD_MATCH_DAYS}_days"}
    if avg_equity is not None and avg_equity > 0 and net_income.status == "ok":
        out["roe_ttm"] = _obs_ok("roe_ttm", net_income.value / avg_equity, cutoff, equity_inputs)
    else:
        out["roe_ttm"] = _obs_miss("roe_ttm", cutoff, net_income.reason or equity_base_reason or MissingReason.NO_DATA, equity_inputs)

    if avg_assets is None or avg_assets <= 0:
        out["roa_ttm"] = _obs_miss("roa_ttm", cutoff, asset_base_reason or MissingReason.NO_DATA, asset_inputs)
        out["accruals_ttm"] = _obs_miss("accruals_ttm", cutoff, asset_base_reason or MissingReason.NO_DATA, asset_inputs)
        out["profit_improvement_yoy"] = _obs_miss("profit_improvement_yoy", cutoff, asset_base_reason or MissingReason.NO_DATA, asset_inputs)
    else:
        out["roa_ttm"] = _obs_ok("roa_ttm", net_income.value / avg_assets, cutoff, asset_inputs) if net_income.status == "ok" else _obs_miss("roa_ttm", cutoff, net_income.reason, asset_inputs)
        if _same_ttm_window(net_income, cfo):
            out["accruals_ttm"] = _obs_ok("accruals_ttm", -(net_income.value - cfo.value) / avg_assets, cutoff, {**asset_inputs, "net_income_ttm": net_income.value, "cfo_ttm": cfo.value})
        else:
            out["accruals_ttm"] = _obs_miss("accruals_ttm", cutoff,
                                          net_income.reason or cfo.reason or "financial_period_mismatch", asset_inputs)
        prior_ni = prior_ttm(net_income, CONCEPTS["net_income"])
        if net_income.status == "ok" and prior_ni.status == "ok":
            out["profit_improvement_yoy"] = _obs_ok("profit_improvement_yoy", (net_income.value - prior_ni.value) / avg_assets, cutoff, {**asset_inputs, "prior_net_income_ttm": prior_ni.as_dict()})
        else:
            out["profit_improvement_yoy"] = _obs_miss("profit_improvement_yoy", cutoff, prior_ni.reason or net_income.reason, asset_inputs)

    industry = (identity.industry or "").strip().lower()
    if industry in BANK_INDUSTRIES:
        out["gross_margin_ttm"] = _obs_miss("gross_margin_ttm", cutoff, MissingReason.INDUSTRY_NOT_APPLICABLE, {"industry": industry})
    elif _same_ttm_window(gross_profit, revenue) and revenue.value > 0:
        out["gross_margin_ttm"] = _obs_ok("gross_margin_ttm", gross_profit.value / revenue.value, cutoff, {"gross_profit_ttm": gross_profit.value, "revenue_ttm": revenue.value})
    elif revenue.status == "ok" and revenue.value <= 0:
        out["gross_margin_ttm"] = _obs_miss("gross_margin_ttm", cutoff, MissingReason.NONPOSITIVE_BASE)
    else:
        out["gross_margin_ttm"] = _obs_miss("gross_margin_ttm", cutoff, gross_profit.reason or revenue.reason or "financial_period_mismatch")

    prior_revenue = prior_ttm(revenue, CONCEPTS["revenue"])
    if revenue.status == "ok" and prior_revenue.status == "ok" and prior_revenue.value > 0:
        out["revenue_growth_yoy"] = _obs_ok("revenue_growth_yoy", revenue.value / prior_revenue.value - 1.0, cutoff,
            {"revenue_ttm": revenue.value, "prior_revenue_ttm": prior_revenue.value,
             "revenue_period_end": revenue.period_end.isoformat(),
             "prior_revenue_period_end": prior_revenue.period_end.isoformat(),
             "prior_period_match": f"nearest_anniversary_within_{PRIOR_PERIOD_MATCH_DAYS}_days"})
    elif prior_revenue.status == "ok" and prior_revenue.value <= 0:
        out["revenue_growth_yoy"] = _obs_miss("revenue_growth_yoy", cutoff, MissingReason.NONPOSITIVE_BASE)
    else:
        out["revenue_growth_yoy"] = _obs_miss("revenue_growth_yoy", cutoff, revenue.reason or prior_revenue.reason)

    debt_inputs = {**debt.as_dict(), "assets": assets.value if assets else None,
                   "assets_period_end": assets.period_end.isoformat() if assets else None,
                   "cash": cash.value if cash else None,
                   "cash_period_end": cash.period_end.isoformat() if cash else None,
                   "cash_concept": cash.concept if cash else None}
    if debt.status == "ok" and assets is not None and assets.value > 0 and debt.period_end == assets.period_end and debt.unit == assets.unit:
        out["leverage"] = _obs_ok("leverage", debt.value / assets.value, cutoff, debt_inputs)
        if cash is not None and cash.period_end == debt.period_end and cash.unit == debt.unit:
            out["net_debt_ratio"] = _obs_ok("net_debt_ratio", (debt.value - cash.value) / assets.value, cutoff, debt_inputs)
        else:
            out["net_debt_ratio"] = _obs_miss("net_debt_ratio", cutoff,
                                            MissingReason.NO_DATA if cash is None else "financial_period_or_unit_mismatch", debt_inputs)
    else:
        reason = debt.reason or (MissingReason.NO_DATA if assets is None else "financial_period_or_unit_mismatch")
        out["leverage"] = _obs_miss("leverage", cutoff, reason, debt_inputs)
        out["net_debt_ratio"] = _obs_miss("net_debt_ratio", cutoff, reason, debt_inputs)

    interest_inputs = {"operating_income_ttm": operating_income.value, "interest_expense_ttm": interest.value,
                       "operating_income_window": operating_income.as_dict(), "interest_expense_window": interest.as_dict()}
    if _same_ttm_window(operating_income, interest) and interest.value > 0:
        out["interest_coverage"] = _obs_ok("interest_coverage", operating_income.value / interest.value, cutoff, interest_inputs)
    else:
        reason = interest.reason or operating_income.reason
        if not reason:
            reason = MissingReason.ZERO_DENOMINATOR if _same_ttm_window(operating_income, interest) else "financial_period_mismatch"
        out["interest_coverage"] = _obs_miss("interest_coverage", cutoff, reason, interest_inputs)

    if shares is not None:
        prior_share = prior_instant(shares, CONCEPTS["shares"])
        if prior_share is not None and prior_share.value > 0:
            q = ledger.cumulative_q(security_id, prior_share.period_end, shares.period_end, known_by=known_by)
            prior_adjusted = prior_share.value * q
            if prior_adjusted > 0:
                out["share_change_yoy"] = _obs_ok("share_change_yoy", shares.value / prior_adjusted - 1.0, cutoff,
                    {"shares": shares.value, "prior_shares": prior_share.value, "split_factor": q,
                     "shares_period_end": shares.period_end.isoformat(), "prior_period_end": prior_share.period_end.isoformat(),
                     "prior_period_match": f"nearest_anniversary_within_{PRIOR_PERIOD_MATCH_DAYS}_days"})
            else:
                out["share_change_yoy"] = _obs_miss("share_change_yoy", cutoff, MissingReason.NONPOSITIVE_BASE)
        else:
            out["share_change_yoy"] = _obs_miss("share_change_yoy", cutoff, MissingReason.NO_DATA)
    else:
        out["share_change_yoy"] = _obs_miss("share_change_yoy", cutoff, MissingReason.NO_DATA)

    return out
