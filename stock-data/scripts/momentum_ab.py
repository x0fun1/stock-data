#!/usr/bin/env python3
"""Pre-registered A vs B comparison for the 20-session composite momentum feature.

Signal A is the raw 20-session rate of change - the incumbent quantity the
probability path conditions on. Signal B is the frozen composite score
(0.5 risk-adjusted + 0.3 regression trend + 0.2 relative strength) added as an
independent diagnostic.

This module is an *evaluation*, not a model change. It never writes ``prob_up``,
never fits weights, never re-tunes the tanh scale, and never re-weights the
ensemble. The decision rule is registered in ``PREREGISTRATION`` and emitted
verbatim into every report, so the verdict cannot be chosen after the numbers
are visible.

Metric: mean cross-sectional Spearman IC. Per session the signal is ranked
across the pool against the forward return of the same horizon, then the
per-session ICs are averaged. Ranking cross-sectionally removes the common
market factor, which is what makes pooling several names informative instead of
just re-measuring one market move many times.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PREREGISTRATION: dict[str, Any] = {
    "protocol": "momentum-ab-v1",
    "registered": "2026-10-19",
    "signal_a": "raw_20_session_roc",
    "signal_b": "composite_20_session_score",
    "metric": "mean_cross_sectional_spearman_ic",
    "primary_horizons": [5, 10],
    "report_only_horizons": [20],
    "min_names_per_date": 12,
    "bootstrap": "circular_block",
    "bootstrap_block_length": "horizon_plus_one",
    "bootstrap_iterations": 2000,
    "bootstrap_confidence": 0.95,
    "bootstrap_seed": 20261019,
    "verdict_rule": (
        "composite_better: delta_ic > 0 and the 95% paired interval excludes zero on every "
        "primary horizon; composite_worse: delta_ic < 0 and the 95% paired interval excludes "
        "zero on every primary horizon; otherwise indistinguishable"
    ),
    "report_only_note": (
        "20-session results are reported but never decide the verdict: roughly ten "
        "independent blocks cannot support a claim at that horizon."
    ),
    "weight_claim": (
        "the 50/30/20 weights stay frozen regardless of the outcome; no re-fitting is "
        "performed by this protocol"
    ),
}

COMPONENT_WEIGHTS: dict[str, float] = {
    "risk_adjusted": 0.5,
    "regression_trend": 0.3,
    "relative_strength": 0.2,
}


def load_panel(directory: Path, pool_manifest: Path | None = None) -> dict[str, Any]:
    """Load collected market payloads into a date-aligned close panel."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise SystemExit(f"no collection payloads under {directory}")

    series: dict[str, dict[str, float]] = {}
    dropped: dict[str, str] = {}
    for path in files:
        symbol = path.stem.upper()
        payload = json.loads(path.read_text(encoding="utf-8"))
        market = payload.get("market") if isinstance(payload, dict) else None
        data = (market or {}).get("data") or {}
        bars = data.get("bars") or []
        closes: dict[str, float] = {}
        for bar in bars:
            day = bar.get("date")
            close = bar.get("adj_close")
            if not day or not isinstance(close, (int, float)) or close <= 0:
                continue
            closes[str(day)] = float(close)
        if not closes:
            dropped[symbol] = "no_usable_bars"
            continue
        series[symbol] = closes

    if "SPY" not in series:
        raise SystemExit("benchmark SPY missing from the panel")

    axis = sorted(series["SPY"])
    aligned: dict[str, list[float]] = {}
    for symbol, closes in series.items():
        if symbol == "SPY":
            continue
        missing = [day for day in axis if day not in closes]
        if missing:
            dropped[symbol] = f"axis_mismatch:{len(missing)}_missing_sessions"
            continue
        aligned[symbol] = [closes[day] for day in axis]

    return {
        "dates": axis,
        "closes": aligned,
        "benchmark": {day: series["SPY"][day] for day in axis},
        "dropped": dropped,
        "asset_types": _asset_types(pool_manifest, aligned),
        "pool_manifest": str(pool_manifest) if pool_manifest else None,
    }


def _asset_types(
    pool_manifest: Path | None, symbols: dict[str, list[float]]
) -> dict[str, str]:
    """Declared asset types from the pool manifest; never inferred from prices.

    Returns an empty mapping when no manifest is supplied, which disables the
    stock-only robustness slice rather than guessing instrument types.
    """
    if pool_manifest is None or not pool_manifest.exists():
        return {}
    payload = json.loads(pool_manifest.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    for entry in payload.get("symbols") or []:
        symbol = str(entry.get("symbol") or "").upper()
        asset_type = entry.get("asset_type")
        if symbol in symbols and isinstance(asset_type, str):
            declared[symbol] = asset_type
    return declared


def subset_panel(panel: dict[str, Any], symbols: set[str]) -> dict[str, Any]:
    return {
        "dates": panel["dates"],
        "closes": {s: c for s, c in panel["closes"].items() if s in symbols},
        "benchmark": panel["benchmark"],
        "dropped": {},
        "asset_types": {s: t for s, t in (panel.get("asset_types") or {}).items() if s in symbols},
    }


def roc(closes: list[float], index: int, window: int) -> float | None:
    """Plain 20-session rate of change; the incumbent signal, left unadjusted."""
    if index - window < 0:
        return None
    base = closes[index - window]
    if base <= 0:
        return None
    return closes[index] / base - 1.0


def forward_return(closes: list[float], index: int, horizon: int) -> float | None:
    if index + horizon >= len(closes):
        return None
    base = closes[index]
    if base <= 0:
        return None
    return closes[index + horizon] / base - 1.0


def ablation_scores(contributions: dict[str, float | None]) -> dict[str, float | None]:
    """Re-normalised composite with one component removed, for ablation only.

    ``contribution`` is ``100 * weight * normalised``, so dropping a component and
    dividing by the remaining weight mass yields the composite the frozen formula
    would have produced with that weight set to zero.
    """
    scores: dict[str, float | None] = {}
    for removed, weight in COMPONENT_WEIGHTS.items():
        remaining = 1.0 - weight
        if remaining <= 0:
            scores[removed] = None
            continue
        total = 0.0
        usable = True
        for name, contribution in contributions.items():
            if name == removed:
                continue
            if contribution is None:
                usable = False
                break
            total += contribution
        scores[removed] = total / remaining if usable else None
    return scores


def cross_sectional_ic(
    scores: list[float], labels: list[float]
) -> float | None:
    if len(scores) != len(labels):
        raise ValueError("score and label counts differ")
    from quant_research.math_utils import spearman

    return spearman(scores, labels)


def block_bootstrap_mean_ci(
    values: list[float],
    *,
    block_length: int,
    iterations: int,
    confidence: float,
    seed: int,
) -> tuple[float, float] | None:
    """Circular block bootstrap interval for the mean of a session-indexed series.

    Sessions are the resampling unit because overlapping forward returns make
    neighbouring sessions dependent; resampling individual observations would
    understate the interval.
    """
    count = len(values)
    if count == 0:
        return None
    block = max(1, min(int(block_length), count))
    needed = math.ceil(count / block)
    # Fixed seed is the whole point: the registered interval must be reproducible,
    # so this generator is intentionally not a cryptographic source.
    rng = random.Random(seed)  # noqa: S311 - deterministic resampling, not security
    means: list[float] = []
    for _ in range(max(1, iterations)):
        total = 0.0
        taken = 0
        for _ in range(needed):
            start = rng.randrange(count)
            for offset in range(block):
                if taken >= count:
                    break
                total += values[(start + offset) % count]
                taken += 1
        means.append(total / count)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    low_index = max(0, math.floor(alpha * (len(means) - 1)))
    high_index = min(len(means) - 1, math.ceil((1.0 - alpha) * (len(means) - 1)))
    return means[low_index], means[high_index]


def _collect_rows(
    panel: dict[str, Any], horizon: int
) -> dict[str, list[dict[str, Any]]]:
    """Per-session cross-sections of both signals, their ablations, and the label."""
    from quant_research.momentum import score_at

    dates: list[str] = panel["dates"]
    benchmark: dict[str, float] = panel["benchmark"]
    closes_by_symbol: dict[str, list[float]] = panel["closes"]
    by_date: dict[str, list[dict[str, Any]]] = {}

    last_index = len(dates) - horizon - 1
    for index in range(last_index, -1, -1):
        rows: list[dict[str, Any]] = []
        for symbol, closes in closes_by_symbol.items():
            label = forward_return(closes, index, horizon)
            if label is None:
                continue
            signal_a = roc(closes, index, 20)
            if signal_a is None:
                continue
            evaluated = score_at(closes, dates, benchmark, index)
            if evaluated.get("status") != "valid":
                continue
            components = evaluated.get("components") or {}
            contributions: dict[str, float | None] = {}
            for name in COMPONENT_WEIGHTS:
                component = components.get(name) or {}
                value = component.get("contribution")
                contributions[name] = float(value) if isinstance(value, (int, float)) else None
            if any(value is None for value in contributions.values()):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "a": signal_a,
                    "b": float(evaluated["score"]),
                    "ablations": ablation_scores(contributions),
                    "label": label,
                }
            )
        if rows:
            by_date[dates[index]] = rows
    return by_date


def _ic_by_date(
    by_date: dict[str, list[dict[str, Any]]], key: str
) -> dict[str, float]:
    """Per-session IC keyed by date, so different signals can be paired exactly."""
    series: dict[str, float] = {}
    for day in sorted(by_date):
        rows = by_date[day]
        if key == "a":
            scores = [row["a"] for row in rows]
        elif key == "b":
            scores = [row["b"] for row in rows]
        else:
            name = key.split(":", 1)[1]
            scores = [row["ablations"].get(name) for row in rows]
            if any(score is None for score in scores):
                continue
        labels = [row["label"] for row in rows]
        ic = cross_sectional_ic([float(s) for s in scores], labels)
        if ic is not None:
            series[day] = ic
    return series


def evaluate_horizon(panel: dict[str, Any], horizon: int) -> dict[str, Any]:
    by_date = _collect_rows(panel, horizon)
    minimum = int(PREREGISTRATION["min_names_per_date"])
    usable = {day: rows for day, rows in by_date.items() if len(rows) >= minimum}
    ic_a = _ic_by_date(usable, "a")
    ic_b = _ic_by_date(usable, "b")
    shared = sorted(set(ic_a) & set(ic_b))
    if not shared:
        return {"horizon": horizon, "status": "no_usable_sessions", "usable_sessions": 0}

    series_a = [ic_a[day] for day in shared]
    series_b = [ic_b[day] for day in shared]
    count = len(shared)
    delta = [b - a for a, b in zip(series_a, series_b, strict=True)]
    block = horizon + 1
    settings = {
        "block_length": block,
        "iterations": int(PREREGISTRATION["bootstrap_iterations"]),
        "confidence": float(PREREGISTRATION["bootstrap_confidence"]),
        "seed": int(PREREGISTRATION["bootstrap_seed"]),
    }
    mean_a = math.fsum(series_a) / count
    mean_b = math.fsum(series_b) / count
    result: dict[str, Any] = {
        "horizon": horizon,
        "status": "evaluated",
        "usable_sessions": count,
        "independent_blocks": max(1, count // block),
        "mean_ic_a": mean_a,
        "mean_ic_b": mean_b,
        "delta_ic": mean_b - mean_a,
        "ci_a": block_bootstrap_mean_ci(series_a, **settings),
        "ci_b": block_bootstrap_mean_ci(series_b, **settings),
        "ci_delta": block_bootstrap_mean_ci(delta, **settings),
        "first_session": shared[0],
        "last_session": shared[-1],
    }

    ablations: dict[str, Any] = {}
    for name in COMPONENT_WEIGHTS:
        series = _ic_by_date(usable, f"ablate:{name}")
        paired = [series[day] for day in shared if day in series]
        if len(paired) != count:
            ablations[name] = {
                "status": "incomplete",
                "sessions": len(paired),
                "expected_sessions": count,
            }
            continue
        mean = math.fsum(paired) / count
        ablations[name] = {
            "status": "evaluated",
            "sessions": count,
            "mean_ic_without": mean,
            "delta_vs_full": mean - mean_b,
        }
    result["ablation"] = ablations
    return result


def decide_verdict(results: dict[int, dict[str, Any]]) -> str:
    """Apply the registered rule; nothing here looks at the ablation output."""
    primary = [int(h) for h in PREREGISTRATION["primary_horizons"]]
    better: list[bool] = []
    worse: list[bool] = []
    for horizon in primary:
        record = results.get(horizon) or {}
        if record.get("status") != "evaluated":
            return "indistinguishable"
        interval = record.get("ci_delta")
        delta = record.get("delta_ic")
        if interval is None or delta is None:
            return "indistinguishable"
        low, high = interval
        better.append(delta > 0 and low > 0)
        worse.append(delta < 0 and high < 0)
    if better and all(better):
        return "composite_better"
    if worse and all(worse):
        return "composite_worse"
    return "indistinguishable"


VERDICT_CLAIMS: dict[str, str] = {
    "composite_better": "supported_in_registered_pool",
    "composite_worse": "refuted_in_registered_pool",
    "indistinguishable": "not_established",
}


def assess_durability(verdict: str, robustness: dict[str, Any]) -> str:
    """Whether the verdict survives dropping the declared non-stock slice.

    A verdict that flips on the disclosure slice is still the registered verdict,
    but it must not be reported as a settled property of the feature.
    """
    if robustness.get("status") != "evaluated":
        return "not_assessed"
    if robustness.get("verdict") == verdict:
        return "consistent_across_pool_slices"
    return "pool_dependent"


def build_report(panel: dict[str, Any]) -> dict[str, Any]:
    horizons = [int(h) for h in PREREGISTRATION["primary_horizons"]]
    horizons += [int(h) for h in PREREGISTRATION["report_only_horizons"]]
    results = {horizon: evaluate_horizon(panel, horizon) for horizon in horizons}
    verdict = decide_verdict(results)
    report: dict[str, Any] = {
        "preregistration": PREREGISTRATION,
        "pool": {
            "symbols": sorted(panel["closes"]),
            "symbol_count": len(panel["closes"]),
            "sessions": len(panel["dates"]),
            "first_session": panel["dates"][0] if panel["dates"] else None,
            "last_session": panel["dates"][-1] if panel["dates"] else None,
            "benchmark": "SPY",
            "dropped": panel.get("dropped") or {},
            "manifest": panel.get("pool_manifest"),
        },
        "results": {str(h): r for h, r in results.items()},
        "verdict": verdict,
        "incremental_value_claim": VERDICT_CLAIMS[verdict],
    }
    report["robustness"] = _robustness_slice(panel, horizons)
    report["durability"] = assess_durability(verdict, report["robustness"])
    return report


def _robustness_slice(panel: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    """Disclosure slice: the same protocol restricted to declared stocks.

    This never feeds the verdict. It is reported so a pool-dependent result is
    visibly pool-dependent instead of being presented as robust.
    """
    types = panel.get("asset_types") or {}
    stocks = {symbol for symbol, kind in types.items() if kind == "stock"}
    others = {symbol for symbol, kind in types.items() if kind != "stock"}
    if not stocks or not others:
        return {"status": "not_applicable", "reason": "no_declared_mixed_pool"}
    subset = subset_panel(panel, stocks)
    results = {horizon: evaluate_horizon(subset, horizon) for horizon in horizons}
    return {
        "status": "evaluated",
        "label": "stocks_only",
        "excluded": sorted(others),
        "symbol_count": len(stocks),
        "results": {str(h): r for h, r in results.items()},
        "verdict": decide_verdict(results),
        "note": "disclosure only; the registered verdict is computed on the full pool",
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def _fmt_ci(interval: Any) -> str:
    if not interval or len(interval) != 2:
        return "n/a"
    return f"[{interval[0]:+.3f}, {interval[1]:+.3f}]"


def render_markdown(report: dict[str, Any]) -> str:
    pool = report["pool"]
    lines = [
        "# 20 日动量 A/B 对照（预登记协议 momentum-ab-v1）",
        "",
        f"- 池：{pool['symbol_count']} 只标的，各自 {pool['sessions']} 个 session"
        f"（{pool['first_session']} → {pool['last_session']}），基准 {pool['benchmark']}",
        f"- 判定：**{report['verdict']}** → `incremental_value_claim = "
        f"{report['incremental_value_claim']}`",
        f"- 结论强度：`{report['durability']}`"
        + (
            "（同一协议在剔除 ETF 的股票子集上给出不同判定，说明结论依赖池子构成）"
            if report["durability"] == "pool_dependent"
            else ""
        ),
        "",
        "## 逐期限结果",
        "",
        "| 期限 | 可用 session | 独立块 | IC(A 裸 ROC20) | IC(B 综合分) | ΔIC | Δ 的 95% 区间 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(report["results"], key=int):
        record = report["results"][key]
        if record.get("status") != "evaluated":
            lines.append(f"| {key}D | 0 | 0 | n/a | n/a | n/a | n/a |")
            continue
        lines.append(
            f"| {key}D | {record['usable_sessions']} | {record['independent_blocks']} | "
            f"{_fmt(record['mean_ic_a'])} {_fmt_ci(record['ci_a'])} | "
            f"{_fmt(record['mean_ic_b'])} {_fmt_ci(record['ci_b'])} | "
            f"{_fmt(record['delta_ic'])} | {_fmt_ci(record['ci_delta'])} |"
        )

    lines += ["", "## 单因子消融（去掉该因子后重新归一化）", ""]
    lines += ["| 期限 | 去掉风险调整 | 去掉回归趋势 | 去掉相对强度 |", "| --- | --- | --- | --- |"]
    for key in sorted(report["results"], key=int):
        record = report["results"][key]
        cells = []
        for name in ("risk_adjusted", "regression_trend", "relative_strength"):
            entry = (record.get("ablation") or {}).get(name) or {}
            if entry.get("status") != "evaluated":
                cells.append("n/a")
            else:
                cells.append(
                    f"{_fmt(entry['mean_ic_without'])} (Δ {_fmt(entry['delta_vs_full'])})"
                )
        lines.append(f"| {key}D | " + " | ".join(cells) + " |")

    robustness = report.get("robustness") or {}
    lines += ["", "## 稳健性披露（不参与判定）", ""]
    if robustness.get("status") != "evaluated":
        lines.append(f"- 不适用：{robustness.get('reason', 'unknown')}")
    else:
        lines.append(
            f"- 仅 {robustness['symbol_count']} 只股票（剔除 {len(robustness['excluded'])} 只 ETF）"
            f"按同一协议复算 → **{robustness['verdict']}**"
        )
        lines += [
            "",
            "| 期限 | ΔIC | Δ 的 95% 区间 |",
            "| --- | --- | --- |",
        ]
        for key in sorted(robustness["results"], key=int):
            record = robustness["results"][key]
            if record.get("status") != "evaluated":
                lines.append(f"| {key}D | n/a | n/a |")
                continue
            lines.append(
                f"| {key}D | {_fmt(record['delta_ic'])} | {_fmt_ci(record['ci_delta'])} |"
            )
        lines.append(
            "- 若该子集在某个主判定期限上的区间包含零，说明主判定依赖于池子构成。"
        )

    lines += [
        "",
        "## 预登记判定规则（结果出来前已固定）",
        "",
        f"- 指标：`{PREREGISTRATION['metric']}`，按 session 做横截面排序后取均值",
        f"- 主判定期限：{PREREGISTRATION['primary_horizons']}；仅报告："
        f"{PREREGISTRATION['report_only_horizons']}",
        f"- 自助法：{PREREGISTRATION['bootstrap']}，块长 = 期限 + 1，"
        f"{PREREGISTRATION['bootstrap_iterations']} 次，"
        f"{int(PREREGISTRATION['bootstrap_confidence'] * 100)}%，"
        f"seed {PREREGISTRATION['bootstrap_seed']}",
        f"- 判定：{PREREGISTRATION['verdict_rule']}",
        f"- 权重：{PREREGISTRATION['weight_claim']}",
        f"- 说明：{PREREGISTRATION['report_only_note']}",
        "",
        "## 解释边界",
        "",
        "- 本协议只评估排序信息（IC），不评估校准，也不评估可交易性；"
        "没有计入交易成本、换手或回撤。",
        "- 结论是关于**这个标的池**在这个 52 周窗口内的，不是关于任何单只标的的。",
        "- 无论结果如何，`prob_up`、路径权重与聚合顺序都不因本协议改变。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-registered A/B momentum comparison")
    parser.add_argument("--collections-dir", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--pool-manifest", default=None)
    args = parser.parse_args(argv)

    manifest = Path(args.pool_manifest) if args.pool_manifest else None
    panel = load_panel(Path(args.collections_dir), manifest)
    report = build_report(panel)
    Path(args.output_json).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    Path(args.output_md).write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "claim": report["incremental_value_claim"],
                "symbols": report["pool"]["symbol_count"],
                "sessions": report["pool"]["sessions"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
