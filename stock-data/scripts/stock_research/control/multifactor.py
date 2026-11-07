"""Native current factor analysis and explicit per-channel data admission."""
from __future__ import annotations

from datetime import timedelta
from importlib.util import find_spec
from math import exp

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash
from ..contracts import Market, to_dict
from ..corporate_actions.returns import PriceSeries, build_total_return_series
from ..features.financial import FinancialStore, compute_financial_features
from ..features.macro import MacroStore, resolve_macro_series_map
from ..features.metadata import metadata_asof
from ..features.panel import build_feature_row
from ..features.price import compute_price_features
from ..features.registry import ALL_FACTORS, FINANCIAL_FACTORS, MACRO_FACTORS, PRICE_FACTORS
from ..multifactor.catalog import factor_group, group_equal_weight_score
from ..multifactor.preprocessing import confirmed, prepare_cross_section
from ..multifactor.risk import estimate_factor_risk

VERSION = "multifactor-1.0"
ACCEPTANCE_QUALITIES = {"acceptance_matched", "submission_acceptance_matched"}
EQUITY_TYPES = {"common_stock", "equity", "stock", "ordinary_share"}
LABELS = {
    "mom_60": ("60日总收益动量", "log_return"), "mom_12_1": ("12减1月动量", "log_return"),
    "trend_diff": ("趋势强度", "ratio"), "residual_reversal": ("5日残差反转", "ratio"),
    "relative_strength": ("60日相对强弱", "percent"), "rv_20": ("20日年化波动", "percent"),
    "downside_vol_20": ("20日下行波动", "percent"), "vol_change": ("短期波动变化", "percent"),
    "dd_60": ("距60日高点回撤", "percent"), "max_dd_60": ("60日最大回撤", "percent"),
    "abnormal_volume": ("成交量/过去20日中位数", "log_volume_ratio"),
    "amihud_20": ("20日非流动性", "per_currency"), "beta_252": ("市场Beta", "ratio"),
    "idio_vol_252": ("年化特异波动", "percent"), "ep_ttm": ("TTM盈利收益率", "percent"),
    "fcf_yield_ttm": ("TTM自由现金流收益率", "percent"), "bp": ("账面权益/市值", "percent"),
    "size_log": ("对数市值", "log_currency"), "roa_ttm": ("TTM资产收益率", "percent"),
    "roe_ttm": ("TTM净资产收益率", "percent"), "asset_growth_yoy": ("资产同比增长", "percent"),
    "accruals_ttm": ("现金支持质量（负应计）", "percent"), "gross_margin_ttm": ("TTM毛利率", "percent"),
    "revenue_growth_yoy": ("TTM营收同比", "percent"), "profit_improvement_yoy": ("利润改善/资产", "percent"),
    "leverage": ("披露口径债务/资产", "percent"), "net_debt_ratio": ("披露口径债务减现金/资产", "percent"),
    "interest_coverage": ("利息保障倍数", "multiple"), "share_change_yoy": ("拆股统一后股数同比", "percent"),
    "curve_10y_2y": ("10年减2年利差", "percentage_points"), "cpi_yoy": ("CPI同比", "percentage_points"),
    "fx_20d": ("20观测汇率变化", "log_return"), "policy_change_20d": ("20观测政策利率变化", "percentage_points"),
}


def factor_label(name: str, inputs: dict | None = None) -> str:
    label = LABELS.get(name, (name, "ratio"))[0]
    if name not in {"leverage", "net_debt_ratio"}:
        return label
    debt = {
        "debt_and_capital_lease_obligations": "债务及资本租赁",
        "long_term_debt_and_capital_leases_including_current_portion": "长期债及资本租赁（含流动部分）",
        "long_term_debt_including_current_portion": "长期债（含流动部分）",
    }.get((inputs or {}).get("debt_scope"))
    return label if debt is None else debt + ("/资产" if name == "leverage" else "减现金/资产")


def format_factor(name: str, value) -> str:
    if value is None or not np.isfinite(value):
        return "缺失"
    unit = LABELS.get(name, (name, "ratio"))[1]
    if unit == "percent":
        return f"{value:.2%}"
    if unit == "log_return":
        return f"{np.expm1(value):.2%}"
    if unit == "log_volume_ratio":
        return f"{exp(value):.2f}倍"
    if unit == "per_currency":
        return f"{value:.3e} /货币单位"
    if unit == "percentage_points":
        return f"{value:.2f}个百分点"
    if unit == "multiple":
        return f"{value:.2f}倍"
    return f"{value:.4f}"


def _dataset(inputs, name):
    frozen = inputs.snapshot.get("datasets", {}).get(name)
    if frozen is None:
        return pd.DataFrame(), None
    entry = inputs.store.catalog.get_dataset(name, frozen["version"])
    return (pd.DataFrame(), None) if entry is None else (inputs.store.load(name, entry["version"]), entry)


def _channel_admission(inputs) -> dict:
    channels = {}
    for name in ("prices", "actions", "action_coverage", "financial_facts", "macro", "news", "securities", "security_metadata", "factor_returns"):
        frame, entry = _dataset(inputs, name)
        if entry is None:
            channels[name] = {"status": "missing", "rows": 0, "reason": "not_registered_in_selected_runtime"}
            continue
        times = pd.to_datetime(frame.get("available_at", pd.Series(None, index=frame.index)), utc=True, errors="coerce")
        known = inputs.calendar.session_close_utc(inputs.cutoff) if inputs.calendar else None
        channels[name] = {"status": "observed", "rows": len(frame), "version": entry["version"],
                          "known_timestamp_rows": int(times.notna().sum()),
                          "rows_within_cutoff": int((times.notna() & (times <= known)).sum()) if known else 0,
                          "quality_counts": frame["quality"].fillna("unknown").value_counts().to_dict() if "quality" in frame else {},
                          "historical_training_ready": False}
        if name == "financial_facts":
            channels[name]["accepted_timestamp_rows"] = int(frame.get("quality", pd.Series("", index=frame.index)).astype(str).isin(ACCEPTANCE_QUALITIES).sum())
        channels[name]["reason"] = "field_history_and_publication_readiness_must_be_verified_separately"
    return channels


def _historical_percentiles(prices, ledger, calendar, cutoff, current, config, lookback):
    if calendar is None:
        return {}
    days = [d for d in prices.dates if d < cutoff and calendar.is_session(d)][-lookback:]
    values = {name: [] for name in PRICE_FACTORS}
    for day in days:
        historical = compute_price_features(prices, ledger, day, known_by=calendar.session_close_utc(day),
                                            price_config=config, include_display=False)
        for name in values:
            obs = historical.get(name)
            if obs and obs.status == "ok" and obs.value is not None:
                values[name].append(obs.value)
    result = {}
    for name, seq in values.items():
        obs = current.get(name)
        if obs is None or obs.value is None or len(seq) < 20:
            continue
        population = np.asarray(seq)
        result[name] = {"percentile": float((np.sum(population < obs.value) + .5 * np.sum(population == obs.value)) / len(population)),
                        "observations": len(seq), "window_sessions": lookback,
                        "kind": "own_past_history_descriptive", "is_alpha_score": False,
                        "historical_availability_verified": False}
    return result


def _raw_macro_context(inputs):
    rows = []
    by_series = {}
    known_by = inputs.calendar.session_close_utc(inputs.cutoff) if inputs.calendar else None
    for obs in inputs.macro:
        if obs.observation_date <= inputs.cutoff and (obs.available_at is None or
                (known_by is not None and obs.available_at <= known_by)):
            by_series.setdefault(obs.series_id, []).append(obs)
    for series, observations in sorted(by_series.items()):
        latest = max(observations, key=lambda x: (x.observation_date, str(x.available_at or "")))
        rows.append({"series_id": series, "value": latest.value, "observation_date": latest.observation_date.isoformat(),
                     "available_at": latest.available_at.isoformat() if latest.available_at else None,
                     "vintage_id": latest.vintage_id, "quality": latest.quality,
                     "use": "saved_vintage_background_not_historical_forecast_feature"})
    return rows


def _macro_requirements(inputs):
    custom_map = inputs.policies.defaults.get("macro_series_map", {})
    mapping = resolve_macro_series_map(custom_map)
    known_by = inputs.calendar.session_close_utc(inputs.cutoff) if inputs.calendar else None
    store = MacroStore(inputs.macro)
    minimum = {"curve_10y_2y": 1, "cpi_yoy": 13, "fx_20d": 21, "policy_change_20d": 21}
    requirements = {}
    for name, identifiers in mapping.items():
        raw = {sid: [o for o in inputs.macro if o.series_id == sid and o.observation_date <= inputs.cutoff]
               for sid in identifiers}
        counts = {sid: len(store.history(sid, known_by)) if known_by else 0 for sid in identifiers}
        missing = [sid for sid in identifiers if not raw[sid]]
        requirements[name] = {
            "required_series": list(identifiers), "missing_series": missing,
            "unconfigured_aliases": [sid for sid in missing if sid in {"FX", "RATE"} and name not in custom_map],
            "series_without_cutoff_published_observations": [sid for sid in identifiers if raw[sid] and not counts[sid]],
            "unknown_publication_rows": {sid: sum(o.available_at is None for o in rows) for sid, rows in raw.items()},
            "available_observation_counts": counts, "minimum_observations_per_series": minimum[name],
            "insufficient_published_history": [sid for sid, count in counts.items() if 0 < count < minimum[name]],
        }
    return requirements


def _risk_panel(inputs, known_by):
    rows = []
    for sid, original in inputs.price_map.items():
        prices = PriceSeries([b for b in original.bars if b.date <= inputs.cutoff and (b.available_at is None or b.available_at <= known_by)], sid)
        series = build_total_return_series(prices, inputs.ledger, inputs.cutoff, known_by=known_by)
        for prior, day, value in zip(series.dates, series.dates[1:], series.simple_returns):
            if inputs.calendar.prev_session(day) != prior:
                continue
            rows.append({"security_id": sid, "date": day.isoformat(), "return_value": value,
                         "available_at": prices.by_date[day].available_at})
    return pd.DataFrame(rows)


def build_multifactor_snapshot(inputs, request, history_sessions: int | None = None) -> dict:
    settings = inputs.policies.defaults.get("multifactor", {})
    history_sessions = history_sessions or int(settings.get("history_sessions", 126))
    names = [name for name, spec in ALL_FACTORS.items() if not spec.display_only]
    known_by = inputs.calendar.session_close_utc(inputs.cutoff) if inputs.calendar else None
    channels = _channel_admission(inputs)
    identities = [i for i in inputs.securities.all() if i.market is Market(request.market) and i.active_on(inputs.cutoff)]
    if request.universe:
        identities = [i for i in identities if i.ticker in {t.upper() for t in request.universe}]
    metadata, _ = _dataset(inputs, "security_metadata")
    strict_facts = [f for f in inputs.financials if f.quality in ACCEPTANCE_QUALITIES]
    financial_store = FinancialStore(inputs.financials)
    macro_store = MacroStore([m for m in inputs.macro if m.observation_date <= inputs.cutoff])
    observations_by_sid, rows, securities = {}, [], []
    excluded = []
    for identity in identities:
        original = inputs.price_map.get(identity.security_id)
        if identity.security_type.lower() not in EQUITY_TYPES:
            excluded.append({"security_id": identity.security_id, "ticker": identity.ticker, "reason": "not_operating_company_equity"})
            continue
        if not original or not known_by:
            excluded.append({"security_id": identity.security_id, "ticker": identity.ticker, "reason": "no_prices_or_calendar"})
            continue
        prices = PriceSeries([b for b in original.bars if b.date <= inputs.cutoff and (b.available_at is None or b.available_at <= known_by)], identity.security_id)
        if prices.close(inputs.cutoff) is None:
            excluded.append({"security_id": identity.security_id, "ticker": identity.ticker, "reason": "no_available_completed_price_at_cutoff"})
            continue
        benchmark = inputs.benchmark
        if benchmark:
            benchmark = PriceSeries([b for b in benchmark.bars if b.date <= inputs.cutoff and (b.available_at is None or b.available_at <= known_by)], benchmark.security_id)
        observations = build_feature_row(identity, prices, inputs.ledger, inputs.cutoff, benchmark=benchmark,
            benchmark_ledger=inputs.benchmark_ledger, calendar=inputs.calendar, financial_facts=financial_store,
            macro_observations=macro_store, price_config=inputs.policies.defaults.get("price", {}),
            macro_series_map=inputs.policies.defaults.get("macro_series_map"), include_display=False)
        strict = compute_financial_features(identity, FinancialStore(strict_facts), prices, inputs.ledger, inputs.cutoff, known_by=known_by)
        observations_by_sid[identity.security_id] = observations
        own_history = _historical_percentiles(prices, inputs.ledger, inputs.calendar, inputs.cutoff, observations,
            inputs.policies.defaults.get("price", {}), history_sessions) if identity.ticker in request.tickers else {}
        controls = metadata_asof(metadata, identity.security_id, inputs.cutoff, known_by)
        industry = controls.get("industry", identity.industry)
        industry_confirmed = controls.get("industry_pit_confirmed", False)
        price_pit = bool(prices.bars) and all(b.available_at is not None and b.quality in {"ok", "pit_verified", "availability_verified"} for b in prices.bars)
        row = {"security_id": identity.security_id, "ticker": identity.ticker, "cutoff_date": inputs.cutoff.isoformat(),
               "security_type": identity.security_type, "industry": industry,
               "industry_pit_confirmed": industry_confirmed,
               "size_pit_confirmed": bool(controls.get("size_pit_confirmed", False) and price_pit and strict.get("size_log") and strict["size_log"].status == "ok")}
        raw = {}
        for name in names:
            obs = observations.get(name)
            value = obs.value if obs else None
            row["f__" + name], row["m__" + name] = value, int(obs is None or obs.status != "ok")
            strict_obs = strict.get(name)
            measured_with_acceptance = bool(strict_obs and strict_obs.status == "ok" and value is not None and np.isclose(strict_obs.value, value, rtol=1e-10, atol=1e-12))
            warnings = []
            if name in FINANCIAL_FACTORS and not measured_with_acceptance:
                warnings.append("财务事实的精确公开时刻或单股口径尚未全部核验")
            if name in PRICE_FACTORS or name in {"ep_ttm", "fcf_yield_ttm", "size_log", "bp"}:
                if not price_pit:
                    warnings.append("行情历史可知时点/版本未核验；仅描述")
                warnings.append("公司行动覆盖与公开时点须独立核验")
            raw[name] = {"name": factor_label(name, obs.inputs if obs else {}), "value": value,
                         "formatted": format_factor(name, value), "unit": LABELS.get(name, (name, "ratio"))[1],
                         "group": factor_group(name), "status": obs.status if obs else "missing",
                         "reason": obs.reason if obs else "no_data", "inputs": obs.inputs if obs else {},
                         "exact_financial_timestamp_value_reproduced": measured_with_acceptance if name in FINANCIAL_FACTORS else None,
                         "historical_training_ready": False, "warnings": warnings,
                         "own_history": own_history.get(name)}
        rows.append(row)
        securities.append({"security_id": identity.security_id, "ticker": identity.ticker,
                           "currency": identity.currency, "industry": industry, "factors": raw})
    frame = pd.DataFrame(rows)
    processed = prepare_cross_section(frame, names, neutralize=bool(settings.get("neutralize", True)),
                                     min_universe=int(settings.get("min_cross_section", 20)))
    transformed = processed["frame"]
    scores = group_equal_weight_score(transformed, names, column_prefix="n__")
    for security in securities:
        matches = transformed.index[transformed["security_id"] == security["security_id"]] if "security_id" in transformed else []
        idx = matches[0] if len(matches) else None
        security["group_equal_weight_score"] = float(scores.loc[idx]) if idx is not None and np.isfinite(scores.loc[idx]) else None
        for name, factor in security["factors"].items():
            factor["cross_section_z"] = float(transformed.loc[idx, "z__" + name]) if idx is not None and pd.notna(transformed.loc[idx, "z__" + name]) else None
            factor["neutralized_z"] = float(transformed.loc[idx, "n__" + name]) if idx is not None and pd.notna(transformed.loc[idx, "n__" + name]) else None
    factor_returns, factor_entry = _dataset(inputs, "factor_returns")
    if Market(request.market) is not Market.US:
        risk = {"status": "unavailable", "reason": "HK_requires_independent_market_factor_returns"}
    elif known_by and factor_entry:
        risk = estimate_factor_risk(_risk_panel(inputs, known_by), factor_returns, known_by,
            lookback=int(settings.get("risk_factor_lookback", 252)),
            shrinkage=float(settings.get("risk_covariance_diagonal_shrinkage", .1)))
    else:
        risk = {"status": "unavailable", "reason": "factor_returns_not_registered"}
    result = {"version": VERSION, "status": "descriptive" if securities else "unavailable",
              "cutoff": known_by.isoformat() if known_by else inputs.cutoff.isoformat(),
              "frequency": "daily", "factors_requested": len(names), "securities": securities,
              "cross_section": processed["diagnostics"], "excluded": excluded, "risk_model": risk,
              "macro_context": _raw_macro_context(inputs), "macro_requirements": _macro_requirements(inputs),
              "channels": channels,
              "models": {"equal_weight": "available_research_control", "ridge": "available_local_candidate",
                         "lightgbm": "available_local_candidate" if find_spec("lightgbm") else "dependency_missing", "kronos": "independent_frozen_baseline_requires_sealed_OOF_features_for_fusion",
                         "published_probability": None},
              "limitations": ["因子值、历史分位及风险暴露均是研究描述，合成分数未校准为上涨概率。",
                               "少量科技大盘股不能代表全美股截面；ETF只作市场对照。",
                               "当前因子historical_training_ready=false，补齐字段级PIT证据后再登记训练。",
                               "日频基本面和小时方向分别建模；低频财报不能当作每小时新信息。"]}
    result["feature_sha256"] = canonical_hash(to_dict(securities))
    result["analysis_sha256"] = canonical_hash(to_dict(result))
    return result


def render_multifactor(result: dict) -> str:
    lines = ["## 多因子分析", "", f"数据截止：{result.get('cutoff', '未知')}；架构：{result.get('version', VERSION)}。"]
    cross = result.get("cross_section", {})
    lines += [f"经营公司截面：{cross.get('equity_rows', 0)}家；排序最小工程样本：{cross.get('min_universe', 20)}家；ETF剔除后仅参与市场对照。"]
    if cross.get("status") != "ok":
        lines += ["截面排名、行业/市值中性化及合成分数未形成：合格经营公司样本不足。仍展示可计算因子和个股自身历史位置。"]
    elif cross.get("neutralize_requested") and cross.get("neutralization_status") != "ok":
        lines += ["截面原始标准化可计算，行业/市值中性化因PIT控制、秩或覆盖不足未形成；原因保存在逐因子诊断中。"]
    for security in result.get("securities", []):
        lines += ["", f"### {security['ticker']} 因子画像", "", "| 因子族 | 因子 | 当前值 | 自身过去历史分位 | 状态/口径 |", "|---|---|---|---|---|"]
        for name, factor in security["factors"].items():
            history = factor.get("own_history")
            position = f"{history['percentile']:.1%}（{history['observations']}日）" if history else "未建立"
            state = "仅描述" if factor["status"] == "ok" else f"缺失：{factor.get('reason', 'no_data')}"
            if factor.get("warnings"):
                state += "；" + factor["warnings"][0]
            lines.append(f"| {factor['group']} | {factor['name']} | {factor['formatted']} | {position} | {state} |")
        score = security.get("group_equal_weight_score")
        score_label = "行业/市值处理后的等权研究分数" if cross.get("neutralize_requested") else "未中性化的等权研究分数"
        lines += ["", f"{score_label}：{score:.4f}" if score is not None else f"{score_label}：未形成。"]
        raw = security["factors"]
        momentum, growth, ep = raw.get("mom_60", {}), raw.get("revenue_growth_yoy", {}), raw.get("ep_ttm", {})
        bits = []
        if momentum.get("value") is not None:
            bits.append(f"60日总收益动量为{momentum['formatted']}")
        if growth.get("value") is not None:
            bits.append(f"已披露TTM营收同比为{growth['formatted']}")
        if ep.get("value") is not None:
            bits.append(f"当前TTM盈利收益率为{ep['formatted']}")
        if bits:
            lines.append("观察：" + "；".join(bits) + "。这些维度反映不同信息，尚需同业比较和样本外检验来确定权重。")
    risk = result.get("risk_model", {})
    lines += ["", "### 独立风险解释", ""]
    if risk.get("status") == "descriptive":
        lines += [f"FF3＋Momentum风险归因截至{risk['common_data_end']}，共同因子样本{risk['common_observations']}日；Σ = B F Bᵀ + D；协方差向对角线收缩{risk['diagonal_shrinkage']:.0%}。这是已实现的研究风险模型。",
                  "", "| 证券 | 市场 | 规模SMB | 价值HML | 动量MOM | 年化特异波动 |", "|---|---|---|---|---|---|"]
        for item in risk["exposures"]:
            load = item["loadings"]
            lines.append(f"| {item['security_id']} | {load.get('MKT', 0):.3f} | {load.get('SMB', 0):.3f} | {load.get('HML', 0):.3f} | {load.get('MOM', 0):.3f} | {item['specific_vol_annualized']:.2%} |")
        lines.append("因子收益为已实现的事后收益，历史发布时间未核验；仅用于归因，不进入预测训练，也不称作Barra成品或预期收益。")
    else:
        lines.append(f"共同因子风险模型尚无可用输入：{risk.get('reason', 'no_data')}。价格Beta/波动指标仍在因子表中。")
    if result.get("macro_context"):
        lines += ["", "### 已保存的宏观背景", "", "以下来自本地载荷版本；首次发布时间未核验的观测未用于上述截止时点的预测特征。", "", "| 序列 | 数值 | 统计期 | 数据版本 |", "|---|---|---|---|"]
        for item in result["macro_context"]:
            lines.append(f"| {item['series_id']} | {item['value']:.4f} | {item['observation_date']} | {item.get('vintage_id', '')} |")
    gaps = []
    for name, item in result.get("macro_requirements", {}).items():
        reasons = []
        if item["missing_series"]:
            reasons.append("缺序列：" + "、".join(item["missing_series"]))
        if item["unconfigured_aliases"]:
            reasons.append("占位映射尚未指定实际序列")
        if item["series_without_cutoff_published_observations"]:
            reasons.append("缺截止前可验证的已发布观测：" + "、".join(item["series_without_cutoff_published_observations"]))
        if item["insufficient_published_history"]:
            reasons.append("已发布历史长度不足：" + "、".join(item["insufficient_published_history"]))
        if reasons:
            gaps.append(f"| {LABELS[name][0]} | {'、'.join(item['required_series'])} | {'；'.join(reasons)} |")
    if gaps:
        lines += ["", "### 宏观因子输入缺口", "", "| 因子 | 配置序列 | 当前缺口 |", "|---|---|---|", *gaps]
    lines += ["", "自身历史分位表示当前指标在过去观测中的位置；该百分比不表示上涨概率。", ""]
    return "\n".join(lines)
