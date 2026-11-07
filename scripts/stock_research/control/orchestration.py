"""Current-research orchestration: freeze inputs, infer with active models, assemble one report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..common.errors import CoverageError
from ..common.hashing import canonical_hash, short_hash
from ..common.jsonio import write_json
from ..common.timeutil import ensure_utc, now_utc, parse_date, parse_dt
from ..config import Policies, default_runtime_root, load_policies
from ..context.news import build_context
from ..contracts import (
    ContextEvent,
    FinancialFact,
    MacroObservation,
    Market,
    ReportBundle,
    RunManifest,
    SecurityIdentity,
    Target,
    to_dict,
)
from ..corporate_actions.ledger import ActionLedger
from ..corporate_actions.returns import PriceSeries, compute_path_risk, compute_window_return
from ..datasets.builder import feature_frame
from ..features.panel import build_feature_row
from ..features.registry import INITIAL_MODEL_CANDIDATES
from ..identity_calendar.calendar import TradingCalendar, build_calendar
from ..identity_calendar.identity import SecurityMaster, default_identity
from ..models.registry import ModelRegistry
from ..publication.capabilities import infer_security
from ..reporting.report import render_report
from ..storage.catalog import Catalog
from ..storage.curated_store import CuratedStore
from .ingest import (
    actions_from_frame,
    financials_from_frame,
    macro_from_frame,
    news_from_frame,
    prices_from_frame,
    securities_from_frame,
)


@dataclass
class ResearchInputs:
    policies: Policies
    store: CuratedStore
    registry: ModelRegistry
    securities: SecurityMaster
    price_map: dict[str, PriceSeries]
    ledger: ActionLedger
    benchmark: PriceSeries | None
    benchmark_ledger: ActionLedger | None
    calendar: TradingCalendar | None
    cutoff: date
    financials: list[FinancialFact]
    macro: list[MacroObservation]
    news: list[ContextEvent]
    snapshot: dict[str, Any]
    limitations: list[str] = field(default_factory=list)


def _resolve_ticker(master: SecurityMaster, market: Market, ticker: str, day: date) -> SecurityIdentity | None:
    try:
        return master.resolve(market, ticker, day)
    except Exception:
        return None


def _observed_dates(price_map: dict[str, PriceSeries], benchmark: PriceSeries | None) -> list[date]:
    if benchmark is not None and benchmark.dates:
        return benchmark.dates
    dates = set()
    for series in price_map.values():
        dates.update(series.dates)
    return sorted(dates)


def resolve_cutoff(
    requested: datetime | None,
    calendar: TradingCalendar,
    price_dates: list[date],
    now: datetime | None = None,
) -> tuple[date, list[str]]:
    notes: list[str] = []
    now = ensure_utc(now or now_utc())
    if requested is not None:
        requested = ensure_utc(requested)
        day = requested.date()
        if calendar.is_session(day) and requested >= calendar.session_close_utc(day):
            return day, notes
        if calendar.is_session(day):
            previous = calendar.prev_session(day)
            notes.append(f"截止时刻早于 {day} 收盘；使用上一交易日 {previous} 的数据")
            return previous, notes
        previous = calendar.prev_session(day)
        notes.append(f"{day} 不是交易日；使用上一交易日 {previous}")
        return previous, notes
    if price_dates:
        day = min(now.date(), max(price_dates))
    else:
        day = now.date()
    while not calendar.is_session(day):
        day = calendar.prev_session(day)
    return day, notes


def load_inputs(
    request,
    runtime_root: str | Path | None = None,
    skill_root: str | Path | None = None,
    now: datetime | None = None,
) -> ResearchInputs:
    policies = load_policies(skill_root)
    runtime = Path(runtime_root) if runtime_root else default_runtime_root(policies.skill_root)
    store = CuratedStore(runtime)
    registry = ModelRegistry(runtime, store.catalog)
    required = ["prices", "securities", "actions", "financial_facts", "macro", "news", "action_coverage"]
    snapshot = store.snapshot(required)
    optional_snapshot = store.snapshot(["security_metadata", "factor_returns"])
    snapshot["datasets"].update(optional_snapshot["datasets"])
    limitations: list[str] = []
    for name in snapshot.get("missing", []):
        limitations.append(f"缺少数据集 {name}；依赖它的能力会降级，不影响其他维度。")

    def load_optional(name: str) -> pd.DataFrame:
        if name in snapshot.get("missing", []):
            return pd.DataFrame()
        try:
            return store.load(name, snapshot["datasets"][name]["version"])
        except Exception as exc:
            limitations.append(f"读取数据集 {name} 失败：{type(exc).__name__}")
            return pd.DataFrame()

    securities_frame = load_optional("securities")
    master = securities_from_frame(securities_frame)
    market = Market(request.market)
    if not master.all():
        for ticker in request.tickers:
            master.add(default_identity(market, ticker))
        if request.benchmark:
            master.add(default_identity(market, request.benchmark))
        limitations.append("没有证券主表；使用默认身份，历史代码变更与退市范围未验证。")
    prices_frame = load_optional("prices")
    price_map = prices_from_frame(prices_frame, market)
    actions_frame = load_optional("actions")
    coverage_frame = load_optional("action_coverage")
    ledger = actions_from_frame(actions_frame, coverage_frame)
    financials = financials_from_frame(load_optional("financial_facts"))
    macro = macro_from_frame(load_optional("macro"))
    news = news_from_frame(load_optional("news"))

    benchmark = None
    benchmark_ledger = None
    benchmark_identity = None
    if request.benchmark:
        benchmark_identity = _resolve_ticker(master, market, request.benchmark, date.today())
        if benchmark_identity is not None:
            benchmark = price_map.get(benchmark_identity.security_id)
            if benchmark is None:
                limitations.append(f"基准 {request.benchmark} 没有价格数据；超额概率不发布。")
        else:
            limitations.append(f"基准 {request.benchmark} 不在证券主表；超额概率不发布。")

    observed = _observed_dates(price_map, benchmark)
    calendar = None
    calendar_config = (policies.calendars or {}).get(market.value, {})
    try:
        if market is Market.US:
            years = [d.year for d in observed] or [date.today().year]
            calendar = build_calendar("US", calendar_config, observed_dates=observed, start_year=min(years) - 1, end_year=max(years) + 1)
        else:
            calendar = build_calendar("HK", calendar_config, observed_dates=observed)
    except CoverageError as exc:
        limitations.append(f"没有可验证的 {market.value} 交易日历：{exc}；标签与期限无法生成。")

    cutoff = date.today()
    if calendar is not None:
        cutoff, cutoff_notes = resolve_cutoff(request.cutoff, calendar, observed, now=now)
        limitations.extend(cutoff_notes)
    elif request.cutoff is not None:
        cutoff = parse_date(request.cutoff)

    return ResearchInputs(
        policies=policies,
        store=store,
        registry=registry,
        securities=master,
        price_map=price_map,
        ledger=ledger,
        benchmark=benchmark,
        benchmark_ledger=benchmark_ledger,
        calendar=calendar,
        cutoff=cutoff,
        financials=financials,
        macro=macro,
        news=news,
        snapshot=snapshot,
        limitations=limitations,
    )


def historical_risk_summary(
    prices: PriceSeries,
    ledger: ActionLedger,
    calendar: TradingCalendar,
    cutoff: date,
    horizon: int,
    lookback_sessions: int = 252,
) -> dict[str, Any]:
    sessions = calendar.sessions(cutoff - timedelta(days=500), cutoff)
    windows = []
    for entry_cutoff in sessions[-lookback_sessions - horizon:]:
        try:
            entry = calendar.next_session(entry_cutoff)
            end = calendar.session_offset(entry, horizon - 1)
        except CoverageError:
            continue
        if end > cutoff:
            continue
        risk = compute_path_risk(prices, ledger, entry, end, calendar, thresholds=(-0.05, -0.1, -0.2))
        if risk.get("status") == "ok":
            windows.append(risk)
    if len(windows) < 20:
        return {"reason": "insufficient_history", "windows": len(windows)}
    returns = np.array([w["return"] for w in windows], dtype=float)
    worst = np.array([w["worst_floating_return"] for w in windows], dtype=float)
    drawdowns = np.array([w["max_drawdown_close_path"] for w in windows], dtype=float)
    touches = {key: float(np.mean([w["first_touch"].get(key) is not None for w in windows])) for key in windows[0]["first_touch"]}
    return {
        "windows": len(windows),
        "return_median": float(np.median(returns)),
        "return_q10": float(np.quantile(returns, 0.1)),
        "return_q90": float(np.quantile(returns, 0.9)),
        "worst_floating_return": float(np.min(worst)),
        "max_drawdown_close_path": float(np.min(drawdowns)),
        "touch_probability": touches,
        "note": "historical close-path distribution; not a forward probability",
    }


def _feature_row_dict(observations) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for name, obs in observations.items():
        row[f"f__{name}"] = obs.value
        row[f"m__{name}"] = 1 if obs.status != "ok" else 0
        row[f"r__{name}"] = obs.reason
    return row


def _attach_reactions(
    events: list[ContextEvent],
    price_map: dict[str, PriceSeries],
    ledger: ActionLedger,
    calendar: TradingCalendar | None,
    cutoff: date,
) -> None:
    if calendar is None:
        return
    for event in events:
        when = event.available_at or event.received_at or event.published_at
        if when is None:
            continue
        day = when.date()
        try:
            entry = calendar.next_session(day)
            end = calendar.session_offset(entry, 4)
        except CoverageError:
            continue
        if end > cutoff:
            continue
        for security_id in event.security_ids:
            series = price_map.get(security_id)
            if series is None:
                continue
            result = compute_window_return(series, ledger, entry, end, calendar=calendar, require_complete_path=True)
            if result.status == "ok":
                event.price_reaction[security_id] = {
                    "window": "5 trading days from next open",
                    "return": result.return_value,
                    "status": "observed",
                    "causal": False,
                }
                break


def run_research(
    request,
    runtime_root: str | Path | None = None,
    skill_root: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[ReportBundle, str]:
    inputs = load_inputs(request, runtime_root=runtime_root, skill_root=skill_root, now=now)
    policies = inputs.policies
    limitations = list(inputs.limitations)
    results = []
    decisions = []
    features_by_security: dict[str, dict] = {}
    baseline_forecasts: dict[str, dict] = {}
    stages: list[dict[str, Any]] = []
    for ticker in request.tickers:
        identity = _resolve_ticker(inputs.securities, request.market, ticker, inputs.cutoff)
        if identity is None:
            limitations.append(f"{ticker} 无法在当前日期解析证券身份。")
            continue
        prices = inputs.price_map.get(identity.security_id)
        if prices is not None and inputs.calendar is not None:
            prices = prices.slice(inputs.cutoff, known_by=inputs.calendar.session_close_utc(inputs.cutoff))
        if prices is None or not prices.bars:
            limitations.append(f"{ticker} 没有价格数据；量化和新闻均降级。")
            data_ready = False
            feature_row = {}
        else:
            data_ready = inputs.calendar is not None and prices.close(inputs.cutoff) is not None
            if not data_ready:
                limitations.append(f"{ticker} 在截止 {inputs.cutoff} 没有收盘价；不生成该时点特征。")
            observations = build_feature_row(
                identity,
                prices,
                inputs.ledger,
                inputs.cutoff,
                benchmark=inputs.benchmark,
                benchmark_ledger=inputs.benchmark_ledger,
                calendar=inputs.calendar,
                financial_facts=inputs.financials,
                macro_observations=inputs.macro,
                price_config=policies.defaults.get("price", {}),
                macro_series_map=policies.defaults.get("macro_series_map"),
                include_display=True,
            )
            features_by_security[identity.security_id] = observations
            feature_row = _feature_row_dict(observations)
        stages.append({"name": f"features:{ticker}", "status": "ok" if data_ready else "degraded", "detail": ""})
        if request.quant_baseline and policies.defaults.get('quant_baseline', {}).get('enabled', False):
            from .kronos_baseline import daily_baseline
            try:
                baseline = daily_baseline(prices, inputs.calendar, inputs.cutoff,
                    runtime_root=inputs.store.root, ticker=ticker, market=request.market,
                    config={**policies.defaults.get('quant_baseline', {}), 'package_root': str(policies.skill_root)})
            except Exception as exc:
                baseline = {'model_id': 'kronos-small', 'status': 'failed',
                            'reason': f'{type(exc).__name__}: {exc}'}
            baseline_forecasts[identity.security_id] = baseline
            stages.append({'name': f'kronos_baseline:{ticker}',
                'status': 'ok' if baseline.get('status') == 'ok' else 'degraded',
                'detail': baseline.get('reason', 'frozen_next_bar_direction')})
        own_results, own_decisions = infer_security(
            row=feature_row,
            security_id=identity.security_id,
            market=request.market,
            horizons=list(request.horizons),
            targets=list(request.targets),
            registry=inputs.registry,
            policy=policies.defaults.get("publication", {}),
            data_ready=data_ready,
            data_reason="no_price_at_cutoff" if not data_ready else "",
        )
        if data_ready and inputs.calendar is not None and prices is not None:
            for result in own_results:
                if result.target in {Target.ABSOLUTE, Target.EXCESS}:
                    result.risk = historical_risk_summary(prices, inputs.ledger, inputs.calendar, inputs.cutoff, result.horizon)
        results.extend(own_results)
        decisions.extend(own_decisions)

    context_events: list[ContextEvent] = []
    if inputs.news:
        selected = [e for e in inputs.news if not e.security_ids or any(s in e.security_ids for s in [i.security_id for i in inputs.securities.all()])]
        context_events = build_context(
            selected,
            inputs.calendar.session_close_utc(inputs.cutoff) if inputs.calendar else inputs.cutoff,
            request.news_lookback_days,
            policies.defaults.get("news", {}),
        )
        _attach_reactions(context_events, inputs.price_map, inputs.ledger, inputs.calendar, inputs.cutoff)
        stages.append({"name": "news_context", "status": "ok", "detail": f"{len(context_events)} events"})
    else:
        stages.append({"name": "news_context", "status": "degraded", "detail": "no news dataset"})

    multifactor_snapshot = {}
    if request.multifactor and policies.defaults.get("multifactor", {}).get("enabled", True):
        from .multifactor import build_multifactor_snapshot
        try:
            multifactor_snapshot = build_multifactor_snapshot(inputs, request)
            stages.append({"name": "multifactor", "status": "ok" if multifactor_snapshot.get("status") == "descriptive" else "degraded",
                           "detail": multifactor_snapshot.get("cross_section", {}).get("status", "unavailable")})
        except Exception as exc:
            multifactor_snapshot = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            stages.append({"name": "multifactor", "status": "degraded", "detail": multifactor_snapshot["reason"]})

    run_id = "run-" + now_utc().strftime("%Y%m%dT%H%M%S%fZ") + "-" + short_hash({"request": request, "cutoff": str(inputs.cutoff)}, 6)
    manifest = RunManifest(
        run_id=run_id,
        created_at=now_utc(),
        request=to_dict(request),
        policy_version=policies.version,
        code_version="skill-0.3.0-multifactor",
        snapshot={
            **inputs.snapshot,
            "policy_hash": policies.hash,
            "calendar": inputs.calendar.coverage_label() if inputs.calendar else None,
            "cutoff": inputs.cutoff.isoformat(),
        },
        stages=stages,
        outputs={},
        artifacts={'kronos_baselines': {sid: {'input': value.get('input_file'),
            'forecast': value.get('forecast_file'), 'input_sha256': value.get('input_sha256'),
            'model_version': value.get('model_version')} for sid, value in baseline_forecasts.items()}},
        errors=[{"kind": "limitation", "detail": x} for x in limitations],
    )
    if multifactor_snapshot:
        manifest.artifacts["multifactor"] = {"version": multifactor_snapshot.get("version"),
                                             "analysis_sha256": multifactor_snapshot.get("analysis_sha256")}
    bundle = ReportBundle(
        request=request,
        securities=[i for i in inputs.securities.all() if i.ticker in request.tickers and i.market is Market(request.market)],
        results=results,
        decisions=decisions,
        context_events=context_events,
        features=features_by_security,
        run_manifest=manifest,
        limitations=limitations,
        headline="",
        baseline_forecasts=baseline_forecasts,
        multifactor_snapshot=multifactor_snapshot,
    )
    markdown = render_report(bundle)
    reports_dir = inputs.store.root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{run_id}.md"
    json_path = reports_dir / f"{run_id}.json"
    manifest.outputs = {"markdown": str(md_path), "json": str(json_path)}
    md_path.write_text(markdown, encoding="utf-8")
    write_json(json_path, to_dict(bundle))
    inputs.store.catalog.register_run(
        run_id,
        json_path,
        {"request": to_dict(request), "cutoff": inputs.cutoff.isoformat(), "results": len(results), "limitations": len(limitations)},
    )
    return bundle, markdown
