from datetime import date, datetime, timedelta, timezone
from math import log
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
import pytest

from stock_research.contracts import CorporateAction, FinancialFact, MacroObservation
from stock_research.corporate_actions import ActionLedger
from stock_research.features.financial import FinancialStore, compute_financial_features
from stock_research.features.metadata import metadata_asof
from stock_research.features.macro import MacroStore
from stock_research.multifactor.factor_returns import parse_daily_archive, import_factor_archives
from stock_research.multifactor.risk import estimate_factor_risk

UTC = timezone.utc


def archive(tmp_path, filename, text):
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("daily.csv", text)
    return path


def test_daily_factor_units_and_missing_codes(tmp_path):
    path = archive(tmp_path, "ff.zip", "Notes\n,Mkt-RF,SMB,HML,RF\n20250102,1.0,-2.0,-99.99,0.01\n20250103,0.5,0.3,0.2,0.01\n2025,10,20,30,4\n")
    frame = parse_daily_archive(path)
    assert len(frame) == 2
    assert frame.iloc[0]["MKT"] == pytest.approx(.01)
    assert frame.iloc[0]["SMB"] == pytest.approx(-.02)
    assert frame.iloc[0]["RF"] == pytest.approx(.0001)
    assert pd.isna(frame.iloc[0]["HML"])


@pytest.mark.parametrize("rows", ["20250102,1\n20250102,2\n", "20250102,nan\n", "20250102,1234\n"])
def test_daily_factor_rejects_bad_records(tmp_path, rows):
    with pytest.raises(ValueError):
        parse_daily_archive(archive(tmp_path, "bad.zip", ",Mom\n" + rows))


def test_import_risk_factors_does_not_claim_known_historical_times(tmp_path):
    ff = archive(tmp_path, "ff.zip", ",Mkt-RF,SMB,HML,RF\n20250102,1,2,3,0.01\n")
    mom = archive(tmp_path, "mom.zip", ",Mom\n20250102,4\n")
    result = import_factor_archives(ff, mom, tmp_path / "runtime")
    assert result["rows"] == 1
    assert result["historical_availability_verified"] is False
    assert result["not_prediction_input"] is True


def risk_inputs():
    rng = np.random.default_rng(91)
    days = pd.bdate_range("2024-01-02", periods=180)
    factors = pd.DataFrame({"date": days, **{f: rng.normal(0, .005, len(days)) for f in ("MKT", "SMB", "HML", "MOM")}, "RF": .0001})
    returns = []
    for sid, loading in (("A", 1.5), ("B", .7)):
        y = .0001 + loading * factors["MKT"] - .3 * factors["HML"] + rng.normal(0, .0002, len(days))
        returns.extend({"date": d, "security_id": sid, "return_value": v} for d, v in zip(days, y))
    return pd.DataFrame(returns), factors, days[149].date()


def test_risk_exposures_and_covariance_are_separate_from_forecasts():
    panel, factors, cutoff = risk_inputs()
    result = estimate_factor_risk(panel, factors, cutoff)
    assert result["status"] == "descriptive"
    assert result["historical_availability_verified"] is False
    assert result["exposures"][0]["loadings"]["MKT"] == pytest.approx(1.5, abs=.03)
    covariance = np.asarray(result["covariance_annualized"])
    assert np.linalg.eigvalsh(covariance).min() >= 0
    assert "probability" not in result


def test_future_prices_and_common_factors_do_not_change_old_risk():
    panel, factors, cutoff = risk_inputs()
    before = estimate_factor_risk(panel, factors, cutoff)
    panel.loc[pd.to_datetime(panel["date"]).dt.date > cutoff, "return_value"] = 99
    factors.loc[pd.to_datetime(factors["date"]).dt.date > cutoff, ["MKT", "SMB", "HML", "MOM"]] = 99
    assert estimate_factor_risk(panel, factors, cutoff) == before
    assert estimate_factor_risk(panel, factors, cutoff, strict_pit=True)["reason"] == "historical_available_at_unknown"


def test_metadata_revisions_are_not_backfilled():
    frame = pd.DataFrame([{"security_id": "A", "effective_from": "2024-01-01", "available_at": "2024-01-01T10:00:00Z", "industry": "old", "pit_verified": True},
                          {"security_id": "A", "effective_from": "2024-01-01", "available_at": "2024-06-01T10:00:00Z", "industry": "revised", "pit_verified": True}])
    view = metadata_asof(frame, "A", date(2024, 3, 1), datetime(2024, 3, 1, 21, tzinfo=UTC))
    assert view["industry"] == "old"
    assert view["size_pit_confirmed"] is False
    assert metadata_asof(frame, "A", date(2024, 7, 1), datetime(2024, 7, 1, 21, tzinfo=UTC))["industry"] == "revised"


def test_new_fundamental_factors_and_late_split_are_causal():
    from test_financial import build_facts, identity, price_series, known_by, SEC
    facts = build_facts()
    for year, value in ((2023, 400), (2024, 500)):
        facts.append(FinancialFact(SEC, "StockholdersEquity", value, "USD", date(year, 12, 31),
            available_at=datetime(2025, 2, 15, tzinfo=UTC)))
    store = FinancialStore(facts)
    before = compute_financial_features(identity(), store, price_series(), ActionLedger(), date(2025, 3, 31), known_by=known_by())
    assert before["bp"].value == pytest.approx(.5)
    assert before["roe_ttm"].value == pytest.approx(70 / 450)
    assert before["asset_growth_yoy"].value == pytest.approx(.2)
    assert before["size_log"].value == pytest.approx(log(1000))
    late = CorporateAction(SEC, date(2025, 2, 1), "split", q=100,
        available_at=datetime(2025, 4, 15, tzinfo=UTC))
    after = compute_financial_features(identity(), store, price_series(), ActionLedger([late]), date(2025, 3, 31), known_by=known_by())
    assert after["size_log"].value == before["size_log"].value
    assert after["bp"].value == before["bp"].value


def test_financial_future_statistical_period_is_excluded():
    future = FinancialFact("A", "Assets", 100, "USD", date(2026, 12, 31),
        available_at=datetime(2025, 1, 1, tzinfo=UTC))
    assert FinancialStore([future]).latest_instant("A", ["Assets"], datetime(2025, 2, 1, tzinfo=UTC)) is None


def test_macro_revisions_keep_actual_publication_boundaries_in_features_and_context():
    from types import SimpleNamespace
    from stock_research.control.multifactor import _raw_macro_context
    cutoff = date(2025, 2, 1)
    known = datetime(2025, 2, 1, 21, tzinfo=UTC)
    unknown = MacroObservation("DGS10", date(2025, 1, 1), 1.0, "percent")
    published = MacroObservation("DGS10", date(2025, 1, 1), 2.0, "percent",
        available_at=datetime(2025, 1, 2, 10, tzinfo=UTC))
    revision = MacroObservation("DGS10", date(2025, 1, 1), 99.0, "percent",
        available_at=datetime(2025, 3, 1, 10, tzinfo=UTC))
    impossible_period = MacroObservation("DGS10", date(2026, 1, 1), 999.0, "percent",
        available_at=datetime(2025, 1, 2, 10, tzinfo=UTC))
    assert MacroStore([unknown, published]).history("DGS10")[0].value == 2.0
    observations = [unknown, published, revision, impossible_period]
    assert MacroStore(observations).latest("DGS10", known).value == 2.0
    context = _raw_macro_context(SimpleNamespace(cutoff=cutoff, macro=observations,
        calendar=SimpleNamespace(session_close_utc=lambda _: known)))
    assert len(context) == 1 and context[0]["value"] == 2.0


def test_macro_mapping_uses_full_identifier_and_reports_separate_channel_gaps():
    from types import SimpleNamespace
    from stock_research.control.multifactor import _macro_requirements, render_multifactor
    from stock_research.features.macro import compute_macro_features, resolve_macro_series_map
    known = datetime(2025, 2, 1, 21, tzinfo=UTC)
    observations = [MacroObservation("DFEDTARU", date(2025, 1, 1) + timedelta(days=i),
        5.0 if i < 20 else 4.75, "percent", available_at=datetime(2025, 1, 23, 10, tzinfo=UTC)) for i in range(21)]
    for supplied in ("DFEDTARU", ["DFEDTARU"]):
        factor = compute_macro_features(MacroStore(observations), known.date(),
            series_map={"policy_change_20d": supplied}, known_by=known)["policy_change_20d"]
        assert factor.value == pytest.approx(-.25)
    with pytest.raises(ValueError):
        resolve_macro_series_map({"curve_10y_2y": "DGS10"})
    inputs = SimpleNamespace(cutoff=known.date(), macro=observations + [
        MacroObservation("DGS10", date(2025, 1, 31), 4, "percent"),
        MacroObservation("DGS2", date(2025, 1, 31), 3, "percent")],
        policies=SimpleNamespace(defaults={}), calendar=SimpleNamespace(session_close_utc=lambda _: known))
    gaps = _macro_requirements(inputs)
    assert gaps["curve_10y_2y"]["missing_series"] == []
    assert set(gaps["curve_10y_2y"]["series_without_cutoff_published_observations"]) == {"DGS10", "DGS2"}
    assert gaps["policy_change_20d"]["unconfigured_aliases"] == ["RATE"]
    assert gaps["fx_20d"]["missing_series"] == ["FX"]
    rendered = render_multifactor({"macro_requirements": gaps})
    assert "缺序列：FX" in rendered and "占位映射尚未指定实际序列" in rendered
    inputs.policies.defaults = {"macro_series_map": {"policy_change_20d": "DFEDTARU"}}
    configured = _macro_requirements(inputs)["policy_change_20d"]
    assert configured["missing_series"] == [] and configured["available_observation_counts"]["DFEDTARU"] == 21


def weekly_fiscal_facts():
    from test_financial import SEC
    ends = [date(2024, 10, 27), date(2025, 1, 26), date(2025, 4, 27), date(2025, 7, 27),
            date(2025, 10, 26), date(2026, 1, 25), date(2026, 4, 26), date(2026, 7, 26)]
    facts = []
    start = date(2024, 7, 29)
    for end, revenue in zip(ends, [10, 20, 30, 40, 20, 40, 60, 80]):
        for concept, value in (("Revenues", revenue), ("NetIncomeLoss", revenue / 10)):
            facts.append(FinancialFact(SEC, concept, value, "USD", end, period_start=start,
                available_at=datetime(2026, 8, 30, tzinfo=UTC)))
        start = end + timedelta(days=1)
    for end, assets, equity in ((date(2025, 4, 27), 80, 40), (date(2025, 7, 27), 100, 50),
                               (date(2026, 7, 26), 200, 100)):
        for concept, value in (("Assets", assets), ("StockholdersEquity", equity)):
            facts.append(FinancialFact(SEC, concept, value, "USD", end,
                available_at=datetime(2026, 8, 30, tzinfo=UTC)))
    for end, shares in ((date(2025, 5, 23), 120), (date(2025, 8, 22), 100), (date(2026, 8, 21), 90)):
        facts.append(FinancialFact(SEC, "CommonStockSharesOutstanding", shares, "shares", end,
            available_at=datetime(2026, 8, 30, tzinfo=UTC)))
    return facts


def test_52_week_fiscal_anniversary_matches_the_same_quarter():
    from test_financial import identity, price_series
    cutoff = date(2026, 11, 5)
    known = datetime(2026, 11, 5, 21, tzinfo=UTC)
    facts = weekly_fiscal_facts()
    result = compute_financial_features(identity(), FinancialStore(facts), price_series(day=cutoff),
        ActionLedger(), cutoff, known_by=known)
    assert result["revenue_growth_yoy"].value == pytest.approx(1)
    assert result["revenue_growth_yoy"].inputs["prior_revenue_period_end"] == "2025-07-27"
    assert result["asset_growth_yoy"].value == pytest.approx(1)
    assert result["roa_ttm"].value == pytest.approx(20 / 150)
    assert result["roe_ttm"].value == pytest.approx(20 / 75)
    assert result["share_change_yoy"].value == pytest.approx(-.1)
    assert result["share_change_yoy"].inputs["prior_period_end"] == "2025-08-22"
    late = FinancialFact(facts[0].security_id, "Assets", 999, "USD", date(2025, 7, 27),
        available_at=datetime(2027, 1, 1, tzinfo=UTC))
    amended = compute_financial_features(identity(), FinancialStore(facts + [late]), price_series(day=cutoff),
        ActionLedger(), cutoff, known_by=known)
    assert amended["asset_growth_yoy"].value == result["asset_growth_yoy"].value


def test_missing_year_ago_quarter_is_not_replaced_by_the_previous_quarter():
    from test_financial import identity, price_series
    cutoff = date(2026, 11, 5)
    facts = weekly_fiscal_facts()
    for item in facts:
        if item.period_end in {date(2025, 7, 27), date(2025, 8, 22)}:
            item.available_at = datetime(2027, 1, 1, tzinfo=UTC)
    result = compute_financial_features(identity(), FinancialStore(facts), price_series(day=cutoff),
        ActionLedger(), cutoff, known_by=datetime(2026, 11, 5, 21, tzinfo=UTC))
    for factor in ("asset_growth_yoy", "roa_ttm", "roe_ttm", "revenue_growth_yoy", "share_change_yoy"):
        assert result[factor].status == "missing"
    assert result["revenue_growth_yoy"].reason == "comparable_year_ago_period_missing"
