"""Message-research orchestration: freeze inputs, interpret news and macro context, assemble one report."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..common.errors import DataError
from ..common.hashing import short_hash
from ..common.jsonio import write_json
from ..common.timeutil import ensure_utc, now_utc
from ..config import default_runtime_root, load_policies
from ..context.macro import MacroStore, macro_background
from ..context.news import build_context
from ..context.news_impact import synthesize_news_impacts
from ..contracts import Market, ReportBundle, RunManifest, to_dict
from ..identity_calendar.identity import default_identity
from ..reporting.report import render_report
from ..storage.curated_store import CuratedStore
from ..storage.retention import current_retention, retained_runtime
from .ingest import macro_from_frame, news_from_frame, securities_from_frame
from .news_workflow import apply_workflow_dispositions, latest_workflow_coverage

CODE_VERSION = "skill-0.6.0-news"
DATASETS = ("securities", "news", "macro")


@retained_runtime()
def run_research(
    request,
    runtime_root: str | Path | None = None,
    skill_root: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[ReportBundle, str]:
    policies = load_policies(skill_root)
    runtime = Path(runtime_root) if runtime_root else default_runtime_root(policies.skill_root)
    store = CuratedStore(runtime)
    snapshot = store.snapshot(DATASETS)
    limitations = [f"缺少数据集 {name}；依赖它的部分降级，不影响其他部分。" for name in snapshot["missing"]]
    stages: list[dict] = []

    def load(name: str) -> list[dict]:
        if name in snapshot["missing"]:
            return []
        try:
            return store.load(name, snapshot["datasets"][name]["version"])
        except Exception as exc:
            limitations.append(f"读取数据集 {name} 失败：{type(exc).__name__}")
            return []

    market = Market(request.market)
    # Nothing first available after this instant may enter the report.
    cutoff = ensure_utc(request.cutoff or now or now_utc())
    master = securities_from_frame(load("securities"))
    securities = []
    for ticker in request.tickers:
        try:
            identity = master.resolve(market, ticker, cutoff.date())
        except DataError:
            identity = default_identity(market, ticker)
            limitations.append(f"{ticker} 不在证券主表；使用默认身份 {identity.security_id}，代码变更与退市范围未验证。")
        securities.append(identity)
    security_ids = {identity.security_id for identity in securities}

    news_config = policies.defaults.get("news", {})
    # Events without a security are market-wide (macro or policy) messages.
    selected = [event for event in news_from_frame(load("news")) if event.market is market
                and (not event.security_ids or security_ids.intersection(event.security_ids))]
    selected = apply_workflow_dispositions(selected, store.root, request.tickers, cutoff)
    context_events = build_context(selected, cutoff, request.news_lookback_days, news_config)
    stages.append({"name": "news_context", "status": "ok" if context_events else "degraded",
                   "detail": f"{len(context_events)} events"})
    coverage = {ticker: latest_workflow_coverage(store.root, ticker, cutoff) for ticker in request.tickers}
    # Synthesis uses every eligible reviewed event, before the display limit.
    impact_events = build_context(selected, cutoff, request.news_lookback_days, news_config,
                                  limit=len(selected), deduplicate=False)
    news_impact_summary = synthesize_news_impacts(impact_events, cutoff, news_config.get("impact", {}), coverage)

    observations = [obs for obs in macro_from_frame(load("macro")) if obs.market in (None, market)]
    background = macro_background(MacroStore(observations), cutoff,
                                  policies.defaults.get("macro_series_map")) if observations else []
    stages.append({"name": "macro_background",
                   "status": "ok" if any(item["status"] == "ok" for item in background) else "degraded",
                   "detail": f"{sum(item['status'] == 'ok' for item in background)} readings"})

    request_data = to_dict(request)
    run_id = "run-" + now_utc().strftime("%Y%m%dT%H%M%S%fZ") + "-" + short_hash(
        {"request": request_data, "cutoff": cutoff.isoformat()}, 6)
    manifest = RunManifest(
        run_id=run_id,
        created_at=now_utc(),
        request=request_data,
        policy_version=policies.version,
        code_version=CODE_VERSION,
        snapshot={**snapshot, "policy_hash": policies.hash, "knowledge_cutoff": cutoff.isoformat()},
        stages=stages,
        artifacts={"retention": current_retention(),
                   "news_impact": {"policy_sha256": news_impact_summary["policy_sha256"],
                                   "coverage": news_impact_summary["coverage"],
                                   "event_count": news_impact_summary["event_count"]}},
        errors=[{"kind": "limitation", "detail": x} for x in limitations],
    )
    bundle = ReportBundle(
        request=request,
        securities=securities,
        context_events=context_events,
        run_manifest=manifest,
        macro_background=background,
        limitations=limitations,
        news_impact_summary=news_impact_summary,
    )
    markdown = render_report(bundle)
    reports_dir = store.root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{run_id}.md"
    json_path = reports_dir / f"{run_id}.json"
    manifest.outputs = {"markdown": str(md_path), "json": str(json_path)}
    md_path.write_text(markdown, encoding="utf-8")
    write_json(json_path, to_dict(bundle))
    store.catalog.register_run(run_id, json_path, {"request": request_data, "cutoff": cutoff.isoformat(),
                                                   "events": len(context_events), "limitations": len(limitations)})
    return bundle, markdown
