"""Render the news-focused report and the mandatory response lines."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .forecast_policy import NEWS_WINDOW_DAYS
from .news import news_assessed, news_status_label
from .security import markdown_text, safe_url

AGENT_SUMMARY_BUDGET = 9000


def _window(result: dict[str, Any]) -> dict[str, Any]:
    return (result.get("snapshot") or {}).get("policy") or {}


def _asof_date(result: dict[str, Any]) -> date | None:
    value = (result.get("snapshot") or {}).get("asof_timestamp")
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).date()
    except ValueError:
        return None


def news_window(result: dict[str, Any]) -> tuple[str, str]:
    end = _asof_date(result) or date.today()
    start = end - timedelta(days=NEWS_WINDOW_DAYS)
    return start.isoformat(), end.isoformat()


def reporting_status_for(result: dict[str, Any]) -> str:
    news = result.get("news_result") or {}
    return "reportable" if news_assessed(news.get("status")) else "unavailable"


def _news_line(result: dict[str, Any]) -> str:
    news = result.get("news_result") or {}
    status = news.get("status")
    if not news_assessed(status):
        return "新闻：未提供（全部来源失败或未配置）"
    state = news_status_label(status)
    warnings = len(news.get("warnings") or [])
    ai = news.get("ai_lean") or {}
    coverage = ai.get("coverage") or {}
    counts = coverage.get("direct_label_counts") or {}
    if ai.get("status") in {"assessed", "partial"}:
        ai_label = "已完成" if ai.get("status") == "assessed" else "部分完成"
        judgement = (f"AI逐条{ai_label}，覆盖{coverage.get('assessed_count', 0)}/"
                     f"{coverage.get('candidate_count', 0)}；直接相关利多{counts.get('bullish', 0)}条、"
                     f"利空{counts.get('bearish', 0)}条、中性{counts.get('neutral', 0)}条、"
                     f"混合{counts.get('mixed', 0)}条、不明{counts.get('unclear', 0)}条")
    else:
        judgement = f"AI逐条未评估，候选{coverage.get('candidate_count', 0)}条"
    regime = str((news.get("expected_volatility_regime") or {}).get("regime") or "unknown")
    coverage_block = news.get("coverage") or {}
    start = str(coverage_block.get("observed_publication_start") or "")[:10]
    end = str(coverage_block.get("observed_publication_end") or "")[:10]
    span = coverage_block.get("observed_publication_span_days")
    if start and end and isinstance(span, int):
        coverage_text = f"新闻覆盖：观测首末 {start}～{end}（{span} 天）"
    else:
        coverage_text = "新闻覆盖：观测首末日期不可用"
    if coverage_block.get("coverage_short"):
        coverage_text += "；短覆盖 NEWS_COVERAGE_SHORT"
    return f"新闻：已采集（{state}；{warnings} 条警告；{judgement}；波动状态 {regime}；{coverage_text}）"


def _news_link(item: dict[str, Any]) -> str:
    title = markdown_text(item.get("headline") or "原文", 200)
    url = safe_url(item.get("url"))
    if not url:
        return title + "（来源链接缺失）"
    return f"[{title}](<{url.replace('<', '%3C').replace('>', '%3E')}> )".replace("> )", ">)")


def _major_news_lines(news: dict[str, Any], *, limit: int = 5) -> list[str]:
    ai = news.get("ai_lean") or {}
    if ai.get("status") not in {"assessed", "partial"}:
        return ["- 重点条目：AI 逐条判断尚未完成"]
    rows = ai.get("major_direct_items") or []
    lines = []
    for lean, label in (("bullish", "重点利多"), ("bearish", "重点利空")):
        selected = [row for row in rows if row.get("lean") == lean]
        if not selected:
            lines.append(f"- {label}：本次未识别到有原文链接的条目")
            continue
        for row in selected[:limit]:
            lines.append(f"- {label}：{_news_link(row)}；{row.get('published_at') or '时间未提供'}；"
                         f"理由：{markdown_text(row.get('reason'), 400)}")
    return lines


def required_response_lines(result: dict[str, Any]) -> dict[str, str]:
    """The exact fixed lines checked by ``validate-response``."""

    start, end = news_window(result)
    snapshot = result.get("snapshot") or {}
    return {
        "ticker": f"标的：{snapshot.get('ticker') or '未知'} / {snapshot.get('market') or '未知'} / {snapshot.get('asset_type') or 'unknown'}",
        "news_window": f"新闻窗口：({start}, {end}]；PIT：published_at_only",
        "news": _news_line(result),
        "trading_signal": "交易信号：不提供",
    }


def build_agent_summary(result: dict[str, Any]) -> dict[str, Any]:
    lines = required_response_lines(result)
    news = result.get("news_result") or {}
    ai = news.get("ai_lean") or {}
    coverage = ai.get("coverage") or {}
    return {
        "schema": "agent-summary-4.0",
        "ticker": (result.get("snapshot") or {}).get("ticker"),
        "market": (result.get("snapshot") or {}).get("market"),
        "asof": (result.get("snapshot") or {}).get("asof_timestamp"),
        "headline": {key: lines[key] for key in ("news_window", "news")},
        "sections": {
            "news": {
                "status": news.get("status"),
                "state": news_status_label(news.get("status")),
                "assessed": news_assessed(news.get("status")),
                "warning_count": news.get("warning_count"),
                "ai_assessment_status": ai.get("status"),
                "overall_direction": news.get("overall_direction"),
                "direct_label_counts": coverage.get("direct_label_counts"),
                "volatility_regime": (news.get("expected_volatility_regime") or {}).get("regime"),
            },
        },
        "contract": {"ticker": lines["ticker"], "news_window": lines["news_window"],
                     "news": lines["news"], "trading_signal": lines["trading_signal"]},
    }


def enforce_agent_summary_budget(summary: dict[str, Any], budget: int = AGENT_SUMMARY_BUDGET) -> dict[str, Any]:
    encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
    if len(encoded.encode("utf-8")) <= budget:
        return summary
    trimmed = dict(summary)
    trimmed["truncated"] = True
    return trimmed


def render_final_response(result: dict[str, Any], summary: dict[str, Any] | None = None) -> str:
    del summary
    lines = required_response_lines(result)
    blocks = [
        lines["ticker"],
        lines["news_window"],
        lines["news"],
        lines["trading_signal"],
        "",
        "局限：",
        "- 标题与摘要级判断，未核验正文。",
        "- 历史 as-of 仅能保证 published_at <= as-of，不能重建当时可见性。",
        "- 描述性价格反应只使用已走完的历史窗口，不构成方向预测。",
        "",
        "重点条目（AI 标题判断，未核验正文）：",
    ]
    blocks.extend(_major_news_lines(result.get("news_result") or {}))
    return "\n".join(blocks)


def render_markdown(result: dict[str, Any]) -> str:
    lines = required_response_lines(result)
    news = result.get("news_result") or {}
    snapshot = result.get("snapshot") or {}
    out = ["# Stock Data 新闻消息面报告", ""]
    out.extend(f"- {value}" for value in lines.values())
    out.extend(["", "## 消息面", ""])
    out.append(f"- 状态：{news.get('status') or 'not_assessed'}（{news_status_label(news.get('status'))}）；"
               f"警告：{news.get('warning_count') if news.get('warning_count') is not None else len(news.get('warnings') or [])} 条")
    out.append(f"- 预期波动状态：{(news.get('expected_volatility_regime') or {}).get('regime') or 'unknown'}")
    out.append(f"- 发布时段分布：{news.get('event_time_bucket_counts') or {}}")
    out.extend(_major_news_lines(news))
    ai = news.get("ai_lean") or {}
    if ai.get("status") in {"assessed", "partial"}:
        out.extend(["", "### 逐条 AI 新闻判断", ""])
        for item in ai.get("items") or []:
            out.append(f"- {item.get('published_at') or '时间未提供'}；{item.get('target_relevance') or 'unclear'}；"
                       f"{item.get('lean') or 'unclear'}；{item.get('importance') or 'routine'}；"
                       f"{_news_link(item)}；理由：{markdown_text(item.get('reason'), 400)}")
    out.extend(["", "## 审计与限制", ""])
    out.append("- AI 逐条判断基于标题与摘要，不声称读过正文。")
    out.append("- 价格反应为已发生窗口的描述性关联，不是因果证明，也不是方向结论。")
    out.append(f"- 快照：{snapshot.get('snapshot_id')}；政策：{_window(result).get('policy_version')}")
    return "\n".join(out)
