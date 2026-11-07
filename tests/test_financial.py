from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from stock_research.contracts import ActionType, CorporateAction, FinancialFact, PriceBar, SecurityIdentity
from stock_research.corporate_actions import ActionLedger, PriceSeries
from stock_research.features.financial import CONCEPTS, FinancialStore, compute_financial_features

UTC = timezone.utc
SEC = "US:XNAS:X"


def fact(concept, value, start, end, available, instant=False, unit="USD"):
    return FinancialFact(
        security_id=SEC,
        concept=concept,
        value=value,
        unit=unit,
        period_end=end,
        period_start=None if instant else start,
        fiscal_year=end.year,
        fiscal_period="FY",
        form="10-K",
        accession=f"acc-{concept}-{end}",
        filed_at=available,
        available_at=available,
        source_id="sec",
    )


def build_facts(amended: bool = False):
    facts = []
    ends = {
        2023: [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)],
        2024: [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)],
    }
    cumulative = {
        "Revenues": {2023: [90, 190, 300, 410], 2024: [100, 220, 330, 450]},
        "NetIncomeLoss": {2023: [8, 20, 36, 55], 2024: [10, 25, 45, 70]},
        "NetCashProvidedByUsedInOperatingActivities": {2023: [10, 26, 44, 70], 2024: [12, 30, 50, 80]},
        "PaymentsToAcquirePropertyPlantAndEquipment": {2023: [4, 8, 12, 16], 2024: [5, 10, 15, 20]},
        "GrossProfit": {2023: [36, 76, 120, 164], 2024: [40, 90, 135, 180]},
        "OperatingIncomeLoss": {2023: [12, 28, 48, 72], 2024: [15, 35, 60, 90]},
        "InterestExpense": {2023: [2, 4, 6, 8], 2024: [2, 4, 6, 8]},
    }
    for concept, years in cumulative.items():
        for year, values in years.items():
            start = date(year, 1, 1)
            for end, value in zip(ends[year], values):
                available = datetime(2025, 2, 15, tzinfo=UTC)
                facts.append(fact(concept, value, start, end, available))
                if amended and concept == "Revenues" and year == 2024 and end == date(2024, 12, 31):
                    facts.append(fact(concept, 500, start, end, datetime(2025, 4, 1, tzinfo=UTC)))
    for year, end in [(2023, date(2023, 12, 31)), (2024, date(2024, 12, 31))]:
        available = datetime(2025, 2, 15, tzinfo=UTC)
        facts.append(fact("Assets", 1000 if year == 2023 else 1200, None, end, available, instant=True))
        facts.append(fact("CommonStockSharesOutstanding", 100, None, end, available, instant=True))
        facts.append(fact("CashAndCashEquivalentsAtCarryingValue", 100, None, end, available, instant=True))
        facts.append(fact("LongTermDebt", 300, None, end, available, instant=True))
    return facts


def price_series(close=10.0, day=date(2025, 3, 31)):
    return PriceSeries([PriceBar(SEC, day, close, close, close, close, 1000.0)])


@pytest.fixture
def store():
    return FinancialStore(build_facts())


def identity(industry="technology"):
    return SecurityIdentity(SEC, "X", "US", "XNAS", "common_stock", "USD", date(2000, 1, 1), industry=industry)


def known_by():
    return datetime(2025, 3, 31, 23, 59, tzinfo=UTC)


def test_cumulative_quarters_and_ttm(store):
    quarters = store.quarters(SEC, ["Revenues"], known_by=known_by())
    assert [q.period_end for q in quarters[-4:]] == [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)]
    assert [q.value for q in quarters[-4:]] == [100, 120, 110, 120]
    ttm = store.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=known_by())
    assert ttm.status == "ok"
    assert ttm.value == 450


def test_financial_factors_have_expected_values(store):
    features = compute_financial_features(identity(), store, price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert features["ep_ttm"].value == pytest.approx(70 / 1000)
    assert features["fcf_yield_ttm"].value == pytest.approx(60 / 1000)
    assert features["roa_ttm"].value == pytest.approx(70 / 1100)
    assert features["accruals_ttm"].value == pytest.approx(10 / 1100)
    assert features["gross_margin_ttm"].value == pytest.approx(180 / 450)
    assert features["revenue_growth_yoy"].value == pytest.approx(450 / 410 - 1)
    assert features["profit_improvement_yoy"].value == pytest.approx(15 / 1100)
    assert features["leverage"].value == pytest.approx(0.25)
    assert features["net_debt_ratio"].value == pytest.approx(200 / 1200)
    assert features["interest_coverage"].value == pytest.approx(90 / 8)
    assert features["share_change_yoy"].value == pytest.approx(0.0)


def test_amendment_does_not_change_earlier_as_of(store):
    before = store.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=known_by())
    assert before.value == 450
    store_amended = FinancialStore(build_facts(amended=True))
    old_view = store_amended.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=known_by())
    new_view = store_amended.ttm(SEC, ["Revenues"], date(2025, 4, 30), known_by=datetime(2025, 4, 30, 23, 59, tzinfo=UTC))
    assert old_view.value == 450
    assert new_view.value == 500


def test_split_adjusts_market_cap_but_not_reported_shares(store):
    ledger = ActionLedger([CorporateAction(SEC, date(2025, 2, 1), ActionType.SPLIT, q=2)])
    features = compute_financial_features(identity(), store, price_series(), ledger, date(2025, 3, 31), known_by=known_by())
    assert features["ep_ttm"].value == pytest.approx(70 / 2000)
    assert features["ep_ttm"].inputs["market_cap"]["split_factor"] == 2


def test_bank_industry_has_no_gross_margin(store):
    features = compute_financial_features(identity("banks"), store, price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert features["gross_margin_ttm"].status == "missing"
    assert features["gross_margin_ttm"].reason == "industry_not_applicable"


@pytest.mark.parametrize("last_quarter,expected_ttm,expected_quarter", [(2, 95, 25), (3, 105, 30)])
def test_incomplete_current_fiscal_year_decomposes_ytd_without_annual(last_quarter, expected_ttm, expected_quarter):
    concept = "NetCashProvidedByUsedInOperatingActivities"
    facts = [f for f in build_facts() if f.concept == concept]
    ends = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]
    for end, value in zip(ends[:last_quarter], [20, 45, 75]):
        facts.append(fact(concept, value, date(2025, 1, 1), end,
                          datetime.combine(end + timedelta(days=30), datetime.min.time(), UTC)))
    # The later annual fact must not become a prerequisite or a future operand.
    facts.append(fact(concept, 120, date(2025, 1, 1), date(2025, 12, 31),
                      datetime(2026, 2, 15, tzinfo=UTC)))
    cutoff = ends[last_quarter - 1] + timedelta(days=35)
    known = datetime.combine(cutoff, datetime.max.time(), UTC)
    local = FinancialStore(facts)
    quarters = local.quarters(SEC, [concept], known_by=known)
    assert quarters[-1].period_end == ends[last_quarter - 1]
    assert quarters[-1].value == expected_quarter
    assert quarters[-1].period_start == ends[last_quarter - 2] + timedelta(days=1)
    result = local.ttm(SEC, [concept], cutoff, known_by=known)
    assert result.status == "ok" and result.value == expected_ttm
    assert result.period_end == ends[last_quarter - 1]
    assert result.quarter_sum == expected_ttm
    assert all(c["period_end"] != "2025-12-31" for c in result.components)


def test_a_two_quarter_cumulative_gap_is_not_a_quarter():
    concept = "NetCashProvidedByUsedInOperatingActivities"
    local = FinancialStore([
        fact(concept, 10, date(2025, 1, 1), date(2025, 3, 31), datetime(2025, 4, 30, tzinfo=UTC)),
        fact(concept, 50, date(2025, 1, 1), date(2025, 9, 30), datetime(2025, 10, 30, tzinfo=UTC)),
    ])
    known = datetime(2025, 11, 1, tzinfo=UTC)
    assert [q.value for q in local.quarters(SEC, [concept], known_by=known)] == [10]
    result = local.ttm(SEC, [concept], date(2025, 11, 1), known_by=known)
    assert result.status == "missing" and result.value is None
    assert result.period_end == date(2025, 9, 30)


def test_four_direct_quarters_require_contiguous_actual_intervals():
    intervals = [(date(2024, 1, 1), date(2024, 3, 31)),
                 (date(2024, 4, 2), date(2024, 6, 30)),
                 (date(2024, 7, 1), date(2024, 9, 30)),
                 (date(2024, 10, 1), date(2024, 12, 31))]
    local = FinancialStore([fact("Revenues", 100, start, end, datetime(2025, 2, 15, tzinfo=UTC))
                            for start, end in intervals])
    result = local.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=known_by())
    assert result.status == "missing" and result.value is None
    assert result.reason == "non_consecutive_quarters"


def test_cumulative_difference_tracks_both_operands_and_late_revision():
    first = fact("Revenues", 100, date(2025, 1, 1), date(2025, 3, 31), datetime(2025, 5, 1, tzinfo=UTC))
    revised = replace(first, value=110, accession="q1-amendment",
                      filed_at=datetime(2025, 8, 15, tzinfo=UTC), available_at=datetime(2025, 8, 15, tzinfo=UTC))
    second = fact("Revenues", 250, date(2025, 1, 1), date(2025, 6, 30), datetime(2025, 8, 1, tzinfo=UTC))
    local = FinancialStore([first, revised, second])
    before = local.quarters(SEC, ["Revenues"], datetime(2025, 8, 10, tzinfo=UTC))[-1]
    after = local.quarters(SEC, ["Revenues"], datetime(2025, 8, 20, tzinfo=UTC))[-1]
    assert before.value == 150 and before.available_at == second.available_at
    assert after.value == 140 and after.available_at == revised.available_at
    assert before.components == [first.accession, second.accession]
    assert after.components == [revised.accession, second.accession]
    assert [component["value"] for component in after.component_facts] == [110, 250]
    assert local.quarters(SEC, ["Revenues"], datetime(2025, 8, 10, tzinfo=UTC))[-1].value == 150


def test_unknown_operand_knowledge_is_not_replaced_with_a_fictitious_early_time():
    first = fact("Revenues", 100, date(2025, 1, 1), date(2025, 3, 31), None)
    second = fact("Revenues", 250, date(2025, 1, 1), date(2025, 6, 30), datetime(2025, 8, 1, tzinfo=UTC))
    local = FinancialStore([first, second])
    assert local.quarters(SEC, ["Revenues"])[-1].available_at is None
    assert local.quarters(SEC, ["Revenues"], datetime(2025, 8, 10, tzinfo=UTC)) == []


def test_ttm_prefers_auditable_ytd_formula_and_retains_quarter_discrepancy():
    facts = [f for f in build_facts() if f.concept == "Revenues" and f.period_end.year == 2024]
    for f, value in zip(facts, [10, 20, 30, 40]):
        f.value = value
    facts.extend([
        fact("Revenues", 15, date(2025, 1, 1), date(2025, 3, 31), datetime(2025, 4, 30, tzinfo=UTC)),
        fact("Revenues", 34, date(2025, 1, 1), date(2025, 6, 30), datetime(2025, 7, 31, tzinfo=UTC)),
        fact("Revenues", 18, date(2025, 4, 1), date(2025, 6, 30), datetime(2025, 7, 31, tzinfo=UTC)),
    ])
    local = FinancialStore(facts)
    known = datetime(2025, 8, 10, tzinfo=UTC)
    assert local.quarters(SEC, ["Revenues"], known)[-1].value == 18
    result = local.ttm(SEC, ["Revenues"], date(2025, 8, 10), known_by=known)
    assert result.value == 54 and result.quarter_sum == 53
    assert result.reconciliation_difference == 1
    assert result.method == "latest_annual_plus_current_ytd_minus_comparable_prior_ytd"
    assert [component["value"] for component in result.components] == [40, 34, 20]
    detail = result.as_dict()
    assert detail["period_start"] == "2024-07-01" and detail["period_end"] == "2025-06-30"
    assert detail["quarters"][-1]["component_facts"][0]["value"] == 18


def test_comparable_ytd_allows_52_week_fiscal_anniversary_shift():
    concept = "NetCashProvidedByUsedInOperatingActivities"
    prior_start = date(2024, 1, 29)
    prior_ends = [date(2024, 4, 28), date(2024, 7, 28), date(2024, 10, 27), date(2025, 1, 26)]
    available = datetime(2025, 8, 26, tzinfo=UTC)
    facts = [fact(concept, value, prior_start, end, available) for value, end in zip([10, 20, 30, 40], prior_ends)]
    facts.extend([fact(concept, 15, date(2025, 1, 27), date(2025, 4, 27), available),
                  fact(concept, 35, date(2025, 1, 27), date(2025, 7, 27), available)])
    result = FinancialStore(facts).ttm(SEC, [concept], date(2025, 8, 26), known_by=available)
    assert result.status == "ok" and result.value == 55
    assert result.components[-1]["period_end"] == "2024-07-28"
    assert result.period_start == date(2024, 7, 29)


def test_a_reported_annual_value_is_valid_ttm_without_inventing_quarters():
    available = datetime(2025, 2, 15, tzinfo=UTC)
    local = FinancialStore([fact("Revenues", 400, date(2024, 1, 1), date(2024, 12, 31), available)])
    result = local.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=known_by())
    assert result.status == "ok" and result.value == 400
    assert result.method == "reported_annual" and result.quarters == []
    assert result.quarter_sum is None and result.reconciliation_difference is None


def test_current_incomplete_interest_scope_never_falls_back_to_old_full_ttm():
    facts = [f for f in build_facts() if f.concept == "InterestExpense"]
    facts.append(fact("InterestExpenseNonoperating", 5, date(2025, 1, 1), date(2025, 6, 30), datetime(2025, 7, 31, tzinfo=UTC)))
    known = datetime(2025, 8, 10, tzinfo=UTC)
    result = FinancialStore(facts).ttm(SEC, CONCEPTS["interest_expense"], date(2025, 8, 10), known_by=known)
    assert result.status == "missing" and result.value is None
    assert result.period_end == date(2025, 6, 30)


@pytest.mark.parametrize("new_interest", [False, True])
def test_interest_tag_switch_and_current_operating_period_alignment(new_interest):
    facts = build_facts()
    available = datetime(2025, 7, 31, tzinfo=UTC)
    facts.extend([
        fact("OperatingIncomeLoss", 30, date(2025, 1, 1), date(2025, 3, 31), available),
        fact("OperatingIncomeLoss", 70, date(2025, 1, 1), date(2025, 6, 30), available),
    ])
    if new_interest:
        facts.extend([replace(f, concept="InterestExpenseNonoperating", value=value)
                      for f, value in zip([f for f in facts if f.concept == "InterestExpense" and f.period_end.year == 2024], [1, 3, 4, 6])])
        facts.extend([
            fact("InterestExpenseNonoperating", 2, date(2025, 1, 1), date(2025, 3, 31), available),
            fact("InterestExpenseNonoperating", 5, date(2025, 1, 1), date(2025, 6, 30), available),
        ])
    cutoff = date(2025, 8, 10)
    features = compute_financial_features(identity(), FinancialStore(facts), price_series(day=cutoff), ActionLedger(), cutoff,
                                          known_by=datetime(2025, 8, 10, tzinfo=UTC))
    obs = features["interest_coverage"]
    if new_interest:
        assert obs.status == "ok" and obs.value == pytest.approx(125 / 8)
        assert obs.inputs["interest_expense_window"]["period_end"] == "2025-06-30"
        assert obs.inputs["interest_expense_window"]["components"][0]["concept"] == "InterestExpenseNonoperating"
    else:
        assert obs.status == "missing" and obs.value is None
        assert obs.reason == "financial_period_mismatch"
        assert obs.inputs["interest_expense_window"]["period_end"] == "2024-12-31"


def test_interest_ratio_rejects_different_windows_even_with_the_same_end():
    facts = [f for f in build_facts() if f.concept != "InterestExpense"]
    facts.append(fact("InterestExpenseNonoperating", 8, date(2024, 1, 2), date(2024, 12, 31), datetime(2025, 2, 15, tzinfo=UTC)))
    features = compute_financial_features(identity(), FinancialStore(facts), price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert features["interest_coverage"].status == "missing"
    assert features["interest_coverage"].reason == "financial_period_mismatch"


def test_debt_total_wins_over_noncurrent_and_does_not_add_current_twice():
    facts = build_facts()
    available = datetime(2025, 2, 15, tzinfo=UTC)
    for concept, value in [("LongTermDebtNoncurrent", 250), ("LongTermDebtCurrent", 50), ("DebtCurrent", 50), ("DebtSecuritiesCurrent", 900)]:
        facts.append(fact(concept, value, None, date(2024, 12, 31), available, instant=True))
    local = FinancialStore(facts)
    result = local.debt_balance(SEC, known_by=known_by())
    assert result.value == 300 and result.method == "disclosed_total"
    assert result.debt_scope == "long_term_debt_including_current_portion"
    assert [component["concept"] for component in result.components] == ["LongTermDebt"]
    features = compute_financial_features(identity(), local, price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert features["leverage"].value == pytest.approx(300 / 1200)
    assert features["leverage"].inputs["debt_scope"] == result.debt_scope
    assert features["net_debt_ratio"].value == pytest.approx(200 / 1200)


@pytest.mark.parametrize("current_value", [50, 0])
def test_explicit_noncurrent_and_current_debt_pair_has_both_sources_and_knowledge_times(current_value):
    noncurrent = fact("LongTermDebtNoncurrent", 250, None, date(2024, 12, 31), datetime(2025, 3, 1, tzinfo=UTC), instant=True)
    due_current = fact("LongTermDebtCurrent", current_value, None, date(2024, 12, 31), datetime(2025, 2, 15, tzinfo=UTC), instant=True)
    local = FinancialStore([noncurrent, due_current])
    result = local.debt_balance(SEC, known_by=known_by())
    assert result.status == "ok" and result.value == 250 + current_value
    assert result.available_at == noncurrent.available_at
    assert result.method == "sum_noncurrent_and_current_long_term_debt"
    assert len(result.components) == 2
    assert local.debt_balance(SEC, known_by=datetime(2025, 2, 20, tzinfo=UTC)).status == "missing"


@pytest.mark.parametrize("current_concept,current_end,current_unit", [
    (None, date(2024, 12, 31), "USD"),
    ("DebtCurrent", date(2024, 12, 31), "USD"),
    ("DebtSecuritiesCurrent", date(2024, 12, 31), "USD"),
    ("LongTermDebtCurrent", date(2024, 9, 30), "USD"),
    ("LongTermDebtCurrent", date(2024, 12, 31), "EUR"),
])
def test_incomplete_or_incompatible_debt_pair_is_missing(current_concept, current_end, current_unit):
    available = datetime(2025, 2, 15, tzinfo=UTC)
    facts = [fact("LongTermDebtNoncurrent", 250, None, date(2024, 12, 31), available, instant=True)]
    if current_concept:
        facts.append(fact(current_concept, 50, None, current_end, available, instant=True, unit=current_unit))
    result = FinancialStore(facts).debt_balance(SEC, known_by=known_by())
    assert result.status == "missing" and result.value is None


def test_new_partial_debt_balance_never_uses_a_stale_total():
    available = datetime(2025, 2, 15, tzinfo=UTC)
    local = FinancialStore([
        fact("LongTermDebt", 300, None, date(2023, 12, 31), available, instant=True),
        fact("LongTermDebtNoncurrent", 350, None, date(2024, 12, 31), available, instant=True),
    ])
    result = local.debt_balance(SEC, known_by=known_by())
    assert result.status == "missing" and result.period_end == date(2024, 12, 31)


@pytest.mark.parametrize("stale_concept", ["LongTermDebt", "CashAndCashEquivalentsAtCarryingValue"])
def test_debt_ratios_reject_mismatched_balance_dates(stale_concept):
    facts = [f for f in build_facts() if not (f.concept == stale_concept and f.period_end.year == 2024)]
    features = compute_financial_features(identity(), FinancialStore(facts), price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert features["net_debt_ratio"].status == "missing"
    if stale_concept == "LongTermDebt":
        assert features["leverage"].status == "missing"
    else:
        assert features["leverage"].status == "ok"


def test_market_cap_excludes_a_late_available_older_price(store):
    cutoff = date(2025, 3, 31)
    timely = PriceBar(SEC, date(2025, 3, 28), 10, 10, 10, 10, 1000,
                      available_at=datetime(2025, 3, 28, 21, tzinfo=UTC))
    late = PriceBar(SEC, cutoff, 20, 20, 20, 20, 1000,
                    available_at=datetime(2025, 4, 1, 1, tzinfo=UTC))
    features = compute_financial_features(identity(), store, PriceSeries([timely, late]), ActionLedger(), cutoff, known_by=known_by())
    assert features["ep_ttm"].value == pytest.approx(70 / 1000)
    assert features["ep_ttm"].inputs["market_cap"]["close_date"] == "2025-03-28"


def test_a_currency_change_cannot_form_a_mixed_ttm():
    available = datetime(2025, 2, 15, tzinfo=UTC)
    facts = [fact("Revenues", 100, date(2024, 1, 1), date(2024, 12, 31), available),
             fact("Revenues", 30, date(2025, 1, 1), date(2025, 3, 31), datetime(2025, 4, 30, tzinfo=UTC), unit="EUR")]
    result = FinancialStore(facts).ttm(SEC, ["Revenues"], date(2025, 5, 1), known_by=datetime(2025, 5, 1, tzinfo=UTC))
    assert result.status == "missing" and result.value is None


def test_ambiguous_latest_report_does_not_make_old_income_current():
    facts = build_facts()
    available = datetime(2025, 4, 30, tzinfo=UTC)
    facts.extend([fact("Revenues", 100, date(2025, 1, 1), date(2025, 3, 31), available),
                  fact("Revenues", 90, date(2025, 1, 1), date(2025, 3, 31), available, unit="EUR")])
    local = FinancialStore(facts)
    old = compute_financial_features(identity(), local, price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert old["ep_ttm"].value == pytest.approx(70 / 1000)
    cutoff = date(2025, 5, 1)
    current = compute_financial_features(identity(), local, price_series(day=cutoff), ActionLedger(), cutoff,
                                         known_by=datetime(2025, 5, 1, tzinfo=UTC))
    assert current["ep_ttm"].status == "missing" and current["ep_ttm"].value is None
    assert current["ep_ttm"].reason == "financial_period_mismatch"
    assert current["ep_ttm"].inputs["financial_period_end"] == "2025-03-31"


def test_historical_period_cutoff_does_not_change_when_later_periods_are_known():
    available = datetime(2025, 7, 31, tzinfo=UTC)
    facts = build_facts()
    facts.extend([fact("Revenues", 100, date(2025, 1, 1), date(2025, 3, 31), available),
                  fact("Revenues", 350, date(2025, 1, 1), date(2025, 6, 30), available)])
    local = FinancialStore(facts)
    result = local.ttm(SEC, ["Revenues"], date(2025, 3, 31), known_by=available)
    assert result.period_end == date(2025, 3, 31) and result.value == 450
    assert all(component["period_end"] != "2025-06-30" for component in result.components)
    assert local.quarters(SEC, ["Revenues"], available, as_of=date(2025, 3, 31))[-1].period_end == date(2025, 3, 31)


def test_ambiguous_total_debt_units_are_missing():
    available = datetime(2025, 2, 15, tzinfo=UTC)
    local = FinancialStore([fact("LongTermDebt", 300, None, date(2024, 12, 31), available, instant=True),
                            fact("LongTermDebt", 280, None, date(2024, 12, 31), available, instant=True, unit="EUR")])
    result = local.debt_balance(SEC, known_by=known_by())
    assert result.status == "missing" and result.value is None
    assert result.reason == "ambiguous_units"


@pytest.mark.parametrize("base,feature", [("Assets", "roa_ttm"), ("StockholdersEquity", "roe_ttm")])
def test_current_income_cannot_be_divided_by_a_stale_balance_window(base, feature):
    facts = build_facts()
    available = datetime(2025, 7, 31, tzinfo=UTC)
    facts.extend([fact("NetIncomeLoss", 25, date(2025, 1, 1), date(2025, 3, 31), available),
                  fact("NetIncomeLoss", 55, date(2025, 1, 1), date(2025, 6, 30), available)])
    if base == "StockholdersEquity":
        facts.extend([fact(base, value, None, date(year, 12, 31), datetime(2025, 2, 15, tzinfo=UTC), instant=True)
                      for year, value in [(2023, 500), (2024, 600)]])
    cutoff = date(2025, 8, 10)
    features = compute_financial_features(identity(), FinancialStore(facts), price_series(day=cutoff), ActionLedger(), cutoff,
                                          known_by=datetime(2025, 8, 10, tzinfo=UTC))
    assert features["ep_ttm"].status == "ok"
    assert features[feature].status == "missing" and features[feature].value is None
    assert features[feature].reason == "financial_period_mismatch"
