"""Descriptive news summary and fixed delivery lines (strength_v1).

No probability, composite score or direction conclusion is produced.  The host
AI supplies per-headline ``lean``/``reason`` and may supply ``strength``; this
module validates strength against the frozen rules and formats the matrix.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..security import markdown_text, safe_url

STRENGTH_VERSION = "strength_v1"
LEANS = ("bullish", "bearish", "neutral", "mixed", "unclear")
LEAN_LABELS = {"bullish": "利多", "bearish": "利空", "neutral": "中性", "mixed": "混合", "unclear": "不明"}
STRENGTHS = ("strong", "medium", "weak")
STRENGTH_LABELS = {"strong": "强", "medium": "中", "weak": "弱"}
_QUANTITY = re.compile(
    r"(\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?\s?(?:million|billion|trillion|万|亿|美元|港元|元)"
    r"|\d{4}[-/年]\d{1,2}|\b\d+(?:\.\d+)?\b)", re.IGNORECASE)


def has_quantity(text: str) -> bool:
    return bool(_QUANTITY.search(text or ""))


def _table(refs: dict[str, Any], name: str) -> dict[str, Any]:
    return ((refs.get("event_tables") or {}).get(name) or {})


def derive_strength(item: dict[str, Any], article: dict[str, Any], verification: dict[str, Any],
                    refs: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(strength, basis)`` under the fixed strength_v1 rules."""
    tables = refs.get("event_tables") or {}
    event_type = str(item.get("event_type") or article.get("event_type") or "")
    tier = str(article.get("source_tier") or item.get("source_tier") or "")
    status = str(verification.get("status") or item.get("verification_status") or "not_assessed")
    pit = str(verification.get("verification_pit") or item.get("verification_pit") or "live")
    direct = item.get("target_relevance") == "direct"
    basis: list[str] = []
    if not direct:
        return "weak", ["indirect"]
    if status in {"single_source", "unverified", "not_assessed"}:
        basis.append(f"verification={status}")
        return "weak", basis
    if pit == "post_hoc":
        basis.append("verification_pit=post_hoc")
        return "weak", basis
    if tier != "A_primary" and status not in {"primary_confirmed", "multi_source"}:
        basis.append("source_not_primary_or_multi")
        return "weak", basis
    if event_type in _table(refs, "weak"):
        basis.append("weak_event_table")
        return "weak", basis
    text = " ".join(str(part or "") for part in (item.get("headline"), article.get("title"),
                                                 item.get("reason"), article.get("summary")))
    quantity = has_quantity(text)
    if event_type in _table(refs, "strong") and quantity:
        basis.extend(["primary_or_multi", "strong_event_table", "has_quantity"])
        return "strong", basis
    if event_type in _table(refs, "strong"):
        basis.extend(["primary_or_multi", "strong_event_table", "no_quantity"])
        return "medium", basis
    if event_type in _table(refs, "medium"):
        basis.extend(["primary_or_multi", "medium_event_table"])
        return "medium", basis
    basis.extend(["primary_or_multi", "unclassified_event_table"])
    return "medium", basis


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {lean: {strength: 0 for strength in STRENGTHS} for lean in LEANS}


def _matrix(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix = _empty_matrix()
    for row in rows:
        lean = row.get("lean") if row.get("lean") in LEANS else "unclear"
        strength = row.get("strength") if row.get("strength") in STRENGTHS else "weak"
        matrix[lean][strength] += 1
    return matrix


def _matrix_text(matrix: dict[str, dict[str, int]]) -> str:
    groups = []
    for lean in LEANS:
        counts = matrix[lean]
        groups.append(f"{LEAN_LABELS[lean]} 强{counts['strong']}/中{counts['medium']}/弱{counts['weak']}")
    return "；".join(groups)


def _link(item: dict[str, Any]) -> str:
    title = markdown_text(item.get("headline") or item.get("title") or "原文", 160)
    url = safe_url(item.get("url"))
    if not url:
        return title + "（链接缺失）"
    return f"[{title}](<{url.replace('<', '%3C').replace('>', '%3E')}> )".replace("> )", ">)")


def _item_date(item: dict[str, Any]) -> str:
    return str(item.get("published_at") or "")[:10]


def _source_status(collected: dict[str, Any], verification: dict[str, Any]) -> str:
    parts = []
    sources = collected.get("sources") or {}
    for name in ("sec_edgar", "ir_rss", "finnhub_rest"):
        row = sources.get(name) or {}
        status = row.get("status") or "not_configured"
        count = row.get("count") or 0
        extra = ""
        if row.get("partial_window"):
            first = str(row.get("observed_first") or "")[:10]
            extra = f"（partial_window，最早 {first or '未知'}）"
        parts.append(f"{name} {status} {count}{extra}")
    parts.append(f"tinyfish {verification.get('tinyfish_status') or 'not_assessed'}")
    return "；".join(parts)


def _coverage_text(collected: dict[str, Any]) -> str:
    sources = collected.get("sources") or {}
    pieces = []
    for name in ("sec_edgar", "ir_rss", "finnhub_rest"):
        row = sources.get(name) or {}
        first, last = row.get("observed_first"), row.get("observed_last")
        if first and last:
            pieces.append(f"{name} 观测 {str(first)[:10]}～{str(last)[:10]}")
        else:
            pieces.append(f"{name} 无观测")
    return "；".join(pieces)


def _price_reaction_text(news_result: dict[str, Any]) -> str:
    events = news_result.get("major_events") or []
    mature = {"confirmed_positive", "confirmed_negative",
              "event_price_divergence_positive", "event_price_divergence_negative"}
    for event in events:
        reaction = event.get("market_reaction") or {}
        if reaction.get("status") in mature and reaction.get("market_session_date"):
            ret = reaction.get("daily_close_return")
            ret_text = f"{float(ret) * 100:.2f}%" if isinstance(ret, (int, float)) and not isinstance(ret, bool) else "不可估计"
            return (f"价格反应（描述）：已发生事件后的实际价格窗口 "
                    f"{reaction.get('market_session_date')}（{reaction.get('status')}，{ret_text}）；仅描述性关联，不构成方向判断")
    return "价格反应（描述）：无成熟观察窗"


def _limitations(collected: dict[str, Any], verification: dict[str, Any], direct_count: int,
                 minimum: int) -> str:
    pieces = [_coverage_text(collected)]
    ir = (collected.get("sources") or {}).get("ir_rss") or {}
    if ir.get("partial_window"):
        pieces.append("IR feed 未覆盖整个 60 天窗口（partial_window）")
    pieces.extend([
        "核实按可注册域名计独立，无法识别转载同源",
        "事件表与“少于 5 条”阈值为约定，无统计依据",
        "标题与摘要级判断，未核验正文",
        "A_primary 发行人新闻稿属于发行人陈述",
        "历史 as-of 不能重建当时可见性（pit_status=published_at_only）",
        "Finnhub 仅限个人使用、不得再分发",
    ])
    if direct_count < minimum:
        pieces.append(f"直接相关仅 {direct_count} 条（<{minimum}），样本过少，不作倾向性描述")
    return "；".join(pieces)


def build_summary(*, collected: dict[str, Any], verification: dict[str, Any],
                  ai_items: list[dict[str, Any]], ai_status: str, snapshot: dict[str, Any],
                  news_result: dict[str, Any] | None, refs: dict[str, Any],
                  analysis_at: str) -> dict[str, Any]:
    articles = {str(row.get("article_id")): row for row in collected.get("articles") or []
                if isinstance(row, dict)}
    verifications = {str(row.get("article_id")): row for row in verification.get("items") or []
                     if isinstance(row, dict)}
    as_of = str(collected.get("as_of") or snapshot.get("asof_timestamp") or "")
    cutoff = None
    try:
        cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        cutoff = None
    recent_cutoff = cutoff - timedelta(days=30) if cutoff else None
    direct_rows: list[dict[str, Any]] = []
    indirect_rows: list[dict[str, Any]] = []
    for item in ai_items:
        article = articles.get(str(item.get("news_id")), {})
        verify_row = verifications.get(str(item.get("news_id")), {})
        strength, basis = derive_strength(item, article, verify_row, refs)
        declared = item.get("strength")
        if declared is not None and declared != strength:
            raise ValueError(
                f"news assessment strength {declared!r} conflicts with {STRENGTH_VERSION} "
                f"for {item.get('news_id')!r}: derived {strength!r} ({', '.join(basis)})"
            )
        row = {
            **item,
            "strength": strength,
            "strength_basis": basis,
            "source_tier": article.get("source_tier"),
            "verification_status": verify_row.get("status") or item.get("verification_status"),
            "verification_pit": verify_row.get("verification_pit") or item.get("verification_pit"),
            "event_type": item.get("event_type") or article.get("event_type"),
        }
        if item.get("target_relevance") == "direct":
            direct_rows.append(row)
        elif item.get("target_relevance") == "indirect":
            indirect_rows.append(row)
    recent = [row for row in direct_rows if _is_recent(row.get("published_at"), recent_cutoff)]
    older = [row for row in direct_rows if row not in recent]
    recent_matrix = _matrix(recent)
    older_matrix = _matrix(older)
    tables = refs.get("event_tables") or {}
    minimum = int(tables.get("minimum_direct_items") or 5)

    rank = {strength: index for index, strength in enumerate(("strong", "medium", "weak"))}
    top: dict[str, list[dict[str, Any]]] = {}
    for lean in LEANS:
        rows = [row for row in direct_rows if row.get("lean") == lean]
        rows.sort(key=lambda row: (rank.get(row.get("strength"), 3), _negative_time(row)))
        top[lean] = rows[:3]

    counts = collected.get("counts") or {}
    kept = int(counts.get("kept") or 0)
    collected_count = int(counts.get("collected") or 0)
    deduped = int(counts.get("deduplicated") or 0)
    by_tier = counts.get("by_tier") or {}
    direct_count = len(direct_rows)
    indirect_count = len(indirect_rows)
    dropped = max(0, collected_count - kept)
    ai_coverage = {
        "status": ai_status,
        "assessed_count": len(ai_items),
        "direct_count": direct_count,
        "candidate_count": len(ai_items),
    }
    ai_line = (
        f"AI 逐条判断：已完成，覆盖 {len(ai_items)}/{len(ai_items)}；规则版本 {STRENGTH_VERSION}"
        if ai_status == "assessed" else
        f"AI 逐条判断：未完成，覆盖 {len(ai_items)} 条；规则版本 {STRENGTH_VERSION}"
    )
    matrix_line = (
        f"汇总矩阵：近30天（{_matrix_text(recent_matrix)}）；第31–60天（{_matrix_text(older_matrix)}）"
        if ai_items else "汇总矩阵：未输出（AI 逐条判断未完成）"
    )
    if ai_items and direct_count < minimum:
        matrix_line += "；样本过少，不作倾向性描述"
    top_parts = []
    for lean in LEANS:
        if not top[lean]:
            continue
        entries = []
        for row in top[lean]:
            entries.append(
                f"{_link(row)}（{_item_date(row)}；{row.get('source') or '来源未知'}/"
                f"{row.get('source_tier') or '未分级'}；{row.get('verification_status') or 'not_assessed'}；"
                f"{row.get('event_type') or 'unclassified'}；{markdown_text(row.get('reason'), 200)}）"
            )
        top_parts.append(f"{LEAN_LABELS[lean]}：" + "｜".join(entries))
    top_line = "重点条目：" + ("；".join(top_parts) if top_parts else "无")
    background_parts = []
    for row in indirect_rows[:5]:
        background_parts.append(f"{_link(row)}（{_item_date(row)}）")
    background_line = "间接背景：" + ("；".join(background_parts) if background_parts else "无")
    pit = str(collected.get("pit_status") or "published_at_only")
    pit_line = f"新闻窗口：({str(collected.get('window', {}).get('start_exclusive') or '')[:10]}, " \
               f"{str(collected.get('window', {}).get('end_inclusive') or as_of)[:10]}]；" \
               f"近30天 {int(counts.get('recent_30_days') or 0)} 条，第31–60天 {int(counts.get('days_31_60') or 0)} 条；" \
               f"PIT：{pit}"
    source_line = "来源状态：" + _source_status(collected, verification)
    candidate_line = (
        f"候选：采集 {collected_count} → 去重 {deduped} → 相关 {kept}"
        f"（直接 {direct_count}、间接 {indirect_count}、丢弃 {dropped}）；"
        f"来源分级 A_primary/{int(by_tier.get('A_primary') or 0)} "
        f"B_media/{int(by_tier.get('B_media') or 0)} C_aggregator/{int(by_tier.get('C_aggregator') or 0)}"
    )
    verification_counts = verification.get("counts") or {}
    pits = {row.get("verification_pit") for row in verification.get("items") or [] if isinstance(row, dict)}
    pit_kind = "live" if pits == {"live"} else "post_hoc" if pits == {"post_hoc"} else "live/post_hoc"
    verify_line = (
        f"核实：primary_confirmed {int(verification_counts.get('primary_confirmed') or 0)}、"
        f"multi_source {int(verification_counts.get('multi_source') or 0)}、"
        f"single_source {int(verification_counts.get('single_source') or 0)}、"
        f"unverified {int(verification_counts.get('unverified') or 0)}、"
        f"not_assessed {int(verification_counts.get('not_assessed') or 0)}"
        f"（上限 {int((verification.get('limits') or {}).get('max_items') or 30)} 条；verification_pit={pit_kind}）"
    )
    regime = str((news_result or {}).get("expected_volatility_regime", {}).get("regime") or "unknown")
    reference = collected.get("reference_links") or {}
    ticker = snapshot.get("ticker") or collected.get("ticker") or ""
    yahoo = reference.get("yahoo_finance_news") or f"https://finance.yahoo.com/quote/{ticker}/news/"
    economist = reference.get("economist") or "https://www.economist.com/"
    lines = {
        "analysis_time": f"分析时点：{analysis_at}（Asia/Shanghai）；新闻截止 as-of（UTC）{as_of}",
        "ticker": f"标的：{ticker} / {snapshot.get('market') or collected.get('market') or '未知'} / {snapshot.get('asset_type') or 'unknown'}",
        "news_window": pit_line,
        "source_status": source_line,
        "candidates": candidate_line,
        "verification": verify_line,
        "ai_assessment": ai_line,
        "matrix": matrix_line,
        "top_items": top_line,
        "disclosures": f"披露类：Form 4 共 {int(collected.get('form4_count') or 0)} 笔（不逐条判断）"
                       f"；Form 144 共 {int(collected.get('form144_count') or 0)} 笔（不逐条判断）",
        "background": background_line,
        "price_reaction": _price_reaction_text(news_result or {}),
        "volatility": f"波动状态：expected_volatility_regime={regime}（不含方向）",
        "limitations": "局限：" + _limitations(collected, verification, direct_count, minimum),
        "reference_links": f"参考链接（未抓取、未判断）：Yahoo Finance 个股新闻页 {yahoo}；The Economist 官网 {economist}",
        "trading_signal": "交易信号：不提供",
    }
    return {
        "status": "assessed" if ai_items else "not_assessed",
        "strength_version": STRENGTH_VERSION,
        "as_of": as_of,
        "pit_status": pit,
        "direct_count": direct_count,
        "indirect_count": indirect_count,
        "minimum_direct_items": minimum,
        "matrices": {"recent_30_days": recent_matrix, "days_31_60": older_matrix},
        "top_items": top,
        "direct_items": direct_rows,
        "indirect_items": indirect_rows,
        "ai_coverage": ai_coverage,
        "verification_counts": verification_counts,
        "source_status": collected.get("sources") or {},
        "collection_counts": counts,
        "form4_count": int(collected.get("form4_count") or 0),
        "form144_count": int(collected.get("form144_count") or 0),
        "price_reaction": lines["price_reaction"],
        "volatility_regime": regime,
        "limitations": _limitations(collected, verification, direct_count, minimum),
        "reference_links": {"yahoo_finance_news": yahoo, "economist": economist},
        "lines": lines,
    }


def _is_recent(value: Any, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return False
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return False
    return moment > cutoff


def _negative_time(row: dict[str, Any]) -> float:
    value = str(row.get("published_at") or "")
    try:
        return -datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def collector_candidates(collected: dict[str, Any], verification: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build the host-AI candidate list directly from a collector artifact."""
    verification_map = {str(row.get("article_id")): row for row in (verification or {}).get("items") or []
                        if isinstance(row, dict)}
    ticker = str(collected.get("ticker") or "").upper()
    rows = []
    for article in collected.get("articles") or []:
        if not isinstance(article, dict):
            continue
        article_id = str(article.get("article_id") or "")
        if not article_id:
            continue
        verify = verification_map.get(article_id, {})
        related = [str(item).upper() for item in article.get("related") or []]
        tickers = [str(item).upper() for item in article.get("tickers") or []]
        direct = ticker in tickers or ticker in related or article.get("source_tier") == "A_primary"
        rows.append({
            "news_id": article_id,
            "record_kind": "event",
            "headline": article.get("title"),
            "summary": article.get("summary"),
            "source": article.get("source"),
            "published_at": article.get("published_at"),
            "url": article.get("url"),
            "ticker_relevance": "direct" if direct else "unclear",
            "eligible_evidence": True,
            "exclusion_reasons": [],
            "event_type": article.get("event_type"),
            "source_tier": article.get("source_tier"),
            "verification_status": verify.get("status"),
            "verification_pit": verify.get("verification_pit"),
        })
    rows.sort(key=lambda row: row.get("published_at") or "", reverse=True)
    return rows


def analysis_time(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
