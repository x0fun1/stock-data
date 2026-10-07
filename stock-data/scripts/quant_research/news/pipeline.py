"""Build a descriptive News result from a restricted, frozen-snapshot input."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from ..contracts import HORIZONS, parse_timestamp
from ..snapshot import load_snapshot
from ..security import provider_failed
from .classify import direction_for, event_text, event_type_for, matched_topics, opinion_score
from .contracts import NewsInput
from .decay import time_decay
from .deduplicate import deduplicate_articles
from .normalize import normalize_articles, normalize_source_sentiment
from .reaction import validate_reaction


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def _mean_field(articles: list[dict[str, Any]], name: str) -> float | None:
    values = [float(row[name]) for row in articles if row.get(name) is not None]
    return _rounded(mean(values)) if values else None


def _label(score: float | None) -> str:
    if score is None:
        return "unknown"
    return "moderately_bullish" if score >= 0.35 else "slightly_bullish" if score > 0.15 else "moderately_bearish" if score <= -0.35 else "slightly_bearish" if score < -0.15 else "neutral"


def _direction_aggregate(values: list[str]) -> str:
    if "mixed" in values:
        return "mixed"
    positive = any(value == "positive" for value in values)
    negative = any(value == "negative" for value in values)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral" if values and all(value == "neutral" for value in values) else "unknown"


def _source_agreement(opinions: list[dict[str, Any]]) -> tuple[str, str]:
    by_source: dict[str, list[float]] = {}
    for opinion in opinions:
        score = opinion.get("sentiment_score")
        if score is not None:
            for source in opinion.get("sources", []):
                by_source.setdefault(source, []).append(float(score))
    labels = {"positive" if mean(scores) > 0.15 else "negative" if mean(scores) < -0.15 else "neutral" for scores in by_source.values() if scores}
    directional = labels - {"neutral", "unknown"}
    if len(directional) > 1:
        divergence = "high"
    elif len(by_source) >= 3:
        divergence = "low"
    elif len(by_source) >= 2:
        divergence = "medium"
    else:
        divergence = "unknown"
    if not labels:
        agreement = "unknown"
    elif len(directional) > 1:
        agreement = "low"
    elif len(by_source) >= 2:
        agreement = "high" if divergence == "low" else "medium"
    else:
        agreement = "single_source"
    return agreement, divergence


def _relevance(rows: list[dict[str, Any]], ticker: str) -> str:
    tickers = {symbol for row in rows for symbol in row.get("tickers", [])}
    if ticker in tickers:
        return "tagged_target"
    if any(ticker in row.get("exposure_tickers", []) and row.get("exposure_basis") for row in rows):
        return "documented_exposure"
    if tickers:
        return "other_instrument"
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", re.I)
    return "text_match" if any(pattern.search(" ".join((row.get("title") or "", row.get("summary") or ""))) for row in rows) else "not_explicitly_matched"


def _eligibility(cluster: dict[str, Any], news_input: NewsInput, decay: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str]]:
    rows = cluster["articles"]
    reasons = []
    if _relevance(rows, news_input.ticker) not in {"tagged_target", "text_match", "documented_exposure"}:
        reasons.append("target relevance is unverified or belongs to another instrument")
    if not any(row.get("source", "unknown").lower() not in {"unknown", "", "none"} for row in rows):
        reasons.append("news source is untraceable")
    if any(row.get("source_quality") == 0 or row.get("relevance") == 0 for row in rows):
        reasons.append("source supplied zero quality/relevance")
    representative = cluster["representative"]
    available = representative.get("occurred_at") or representative.get("announcement_at") or cluster["first_published_at"]
    age = (parse_timestamp(news_input.asof_timestamp) - parse_timestamp(available)).total_seconds() / 86400
    if age > policy.get("max_age_calendar_days", max(7, HORIZONS[news_input.horizon] * 4)):
        reasons.append("event/opinion is outside the current evidence window")
    if float(decay["time_decay"]) < policy.get("minimum_time_decay", 0.0625):
        reasons.append("event/opinion decay is below the evidence threshold")
    if news_input.news_status in {"failed", "invalid", "unavailable", "empty"}:
        reasons.append("news domain is unavailable or failed")
    return not reasons, reasons


def prepare_news_input(snapshot_dir: Path) -> NewsInput:
    """Read only normalized news, snapshot identity and frozen market bars.

    This function never opens researcher outputs. Its returned contract has no
    Quant fields or Quant result path.
    """
    manifest, market = load_snapshot(snapshot_dir)
    news_path = snapshot_dir / "news.json"
    news = json.loads(news_path.read_text(encoding="utf-8"))
    data = news.get("data") if isinstance(news, dict) and not provider_failed(news) and news.get("status") not in {"unavailable", "failed", "invalid", "empty"} else None
    normalized, excluded, news_warnings = normalize_articles(
        data,
        ticker=manifest["ticker"],
        asof_timestamp=manifest["asof_timestamp"],
    )
    warnings = list(news_warnings)
    source_sentiment, source_sentiment_warnings = normalize_source_sentiment(
        data,
        available_at=news.get("source_timestamp") or news.get("fetched_at_utc") or news.get("fetched_at"),
        asof_timestamp=manifest["asof_timestamp"],
    )
    warnings.extend(source_sentiment_warnings)
    if news.get("status") in {"unavailable", "failed", "invalid"}:
        warnings.append(f"news domain status is {news.get('status')}")
    if excluded["missing_or_invalid_published_at"] or excluded["after_asof"] or excluded["updated_after_asof"]:
        warnings.append("news records without a reliable point-in-time publication/update timestamp were excluded")
    return NewsInput(
        ticker=manifest["ticker"],
        market=manifest["market"],
        horizon=manifest["horizon"],
        asof_timestamp=manifest["asof_timestamp"],
        snapshot_id=manifest["snapshot_id"],
        news_status="failed" if provider_failed(news) else str(news.get("status", "unavailable")),
        source_sentiment=source_sentiment,
        articles=tuple(dict(row) for row in normalized),
        bars=tuple(dict(row) for row in market.get("data", {}).get("bars", [])),
        warnings=tuple(warnings),
        coverage={"actual_source": news.get("actual_source"), "source_timestamp": news.get("source_timestamp"),
                  "retrieved_window": data.get("history_window") if isinstance(data, dict) else None,
                  "excluded_at_normalization": excluded,
                  "calendar_verification": manifest.get("data_validation", {}).get("calendar_verification", "unverified"),
                  "session_closes": {row["date"]: row["close_at"] for row in (market.get("session_calendar") or {}).get("sessions", [])}},
    )


def analyze_news(news_input: NewsInput, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze news records without Quant results, probabilities, or provider access."""
    policy = policy or {}
    if not isinstance(policy, dict):
        raise ValueError("news policy must be an object")
    if set(policy) - {"half_life_sessions_by_event_type", "max_age_calendar_days", "minimum_time_decay"}:
        raise ValueError("unsupported news policy fields")
    half_lives = policy.get("half_life_sessions_by_event_type", {})
    if not isinstance(half_lives, dict) or any(not isinstance(key, str) or len(key) > 40 or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 366 for key, value in half_lives.items()):
        raise ValueError("news half-life policy requires bounded names and numeric session counts in (0,366]")
    age_limit = policy.get("max_age_calendar_days", max(7, HORIZONS[news_input.horizon] * 4))
    if isinstance(age_limit, bool) or not isinstance(age_limit, int) or not 1 <= age_limit <= 366:
        raise ValueError("news age window must be an integer in 1..366 days")
    decay_limit = policy.get("minimum_time_decay", 0.0625)
    if isinstance(decay_limit, bool) or not isinstance(decay_limit, (int, float)) or not math.isfinite(decay_limit) or not 0 <= decay_limit <= 1:
        raise ValueError("news minimum_time_decay must be in [0,1]")
    cutoff = parse_timestamp(news_input.asof_timestamp)
    # Recheck the boundary at the analyst contract as defense in depth.
    articles, _, input_warnings = normalize_articles({"articles": list(news_input.articles)}, ticker=news_input.ticker, asof_timestamp=news_input.asof_timestamp)
    failed_domain = news_input.news_status in {"failed", "invalid", "unavailable", "empty"}
    if failed_domain:
        articles = []
    clusters, duplicates = deduplicate_articles(articles)
    opinion_clusters = [item for item in clusters if item["role"] == "opinion"]
    event_clusters: list[dict[str, Any]] = []
    unclassified_clusters: list[dict[str, Any]] = []
    for cluster in clusters:
        if cluster["role"] == "opinion":
            continue
        representative = cluster["representative"]
        category = event_type_for(event_text(cluster), representative.get("event_type_hint"))
        if cluster["role"] == "event" or category != "UNCLASSIFIED":
            event_clusters.append(cluster)
        else:
            unclassified_clusters.append(cluster)
    warnings = list(news_input.warnings) + input_warnings
    if failed_domain:
        warnings.append("failed/unavailable news domain payload excluded from evidence")
    if len(articles) != len(news_input.articles):
        warnings.append("NewsInput contained post-as-of records; they were excluded again before analysis")
    if not articles:
        if news_input.source_sentiment:
            warnings.append("no point-in-time article records are available; event analysis is not assessed, provider aggregate sentiment is reported separately")
        else:
            warnings.append("no point-in-time news records are available; News/Sentiment is not assessed")
    if unclassified_clusters:
        warnings.append(f"{len(unclassified_clusters)} records could not be classified as a factual event or explicit opinion and were left unclassified")

    bars = [dict(row) for row in news_input.bars]
    events: list[dict[str, Any]] = []
    for cluster in event_clusters:
        representative = cluster["representative"]
        text = event_text(cluster)
        event_type = event_type_for(text, representative.get("event_type_hint"))
        event_direction = direction_for(text)
        decay_time = representative.get("occurred_at") or representative.get("announcement_at") or cluster["first_published_at"]
        decay = time_decay(decay_time, bars, event_type, news_input.horizon, policy, market=news_input.market)
        eligible, exclusion_reasons = _eligibility(cluster, news_input, decay, policy)
        reaction = validate_reaction(
            published_at=cluster["first_published_at"],
            event_direction=event_direction,
            market=news_input.market,
            bars=bars,
            session_closes=news_input.coverage.get("session_closes"),
        )
        scores = {
            field: _mean_field(cluster["articles"], field)
            for field in ("event_strength", "relevance", "magnitude", "novelty", "source_quality")
        }
        score_inputs = [scores[name] for name in ("event_strength", "relevance", "novelty", "source_quality")]
        effective_score = math.prod(score_inputs) * float(decay["time_decay"]) if all(value is not None for value in score_inputs) else None
        topics = sorted({term for row in cluster["articles"] for term in matched_topics(" ".join((row.get("title") or "", row.get("summary") or "")))})
        source_directions = [direction_for(" ".join((row.get("title") or "", row.get("summary") or ""))) for row in cluster["articles"]]
        positive_count = sum(value == "positive" for value in source_directions)
        negative_count = sum(value == "negative" for value in source_directions)
        if positive_count and negative_count:
            event_source_agreement = "low"
        elif len(cluster["sources"]) > 1 and positive_count + negative_count:
            event_source_agreement = "high"
        elif positive_count + negative_count:
            event_source_agreement = "single_source"
        else:
            event_source_agreement = "unknown"
        tickers = sorted({ticker for row in cluster["articles"] for ticker in row.get("tickers", [])})
        ticker_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(news_input.ticker)}(?![A-Za-z0-9])", re.IGNORECASE)
        explicit_ticker_mention = any(
            ticker_pattern.search(" ".join((row.get("title") or "", row.get("summary") or "")))
            for row in cluster["articles"]
        )
        ticker_relevance = _relevance(cluster["articles"], news_input.ticker)
        events.append({
            "event_id": cluster["cluster_id"].replace("cluster_", "event_"),
            "ticker": news_input.ticker,
            "event_type": event_type,
            "event_group": representative.get("event_group") or ("macro" if event_type == "MACRO" else "company"),
            "eligible_evidence": eligible,
            "exclusion_reasons": exclusion_reasons,
            "fact_status": "reported_event_not_independently_verified",
            "url": representative.get("url"),
            "occurred_at": representative.get("occurred_at"),
            "announcement_at": representative.get("announcement_at"),
            "scheduled_at": representative.get("scheduled_at"),
            "source_independence": "reported_lineage" if cluster["independent_reporting_ids"] else "unverified",
            "independent_reporting_ids": cluster["independent_reporting_ids"],
            "first_published_at": cluster["first_published_at"],
            "article_count": cluster["article_count"],
            "sources": cluster["sources"],
            "source_agreement": event_source_agreement,
            "direction": event_direction,
            "relevance": scores["relevance"],
            "magnitude": scores["magnitude"],
            "novelty": scores["novelty"],
            "source_quality": scores["source_quality"],
            "event_strength": scores["event_strength"],
            "effective_event_score": _rounded(effective_score),
            "effective_event_score_status": "computed_from_gateway_scores" if effective_score is not None else "not_computed_missing_gateway_score_inputs",
            "expected_horizon": None,
            "age_sessions": decay["age_sessions"],
            "half_life_sessions": decay["half_life_sessions"],
            "time_decay": _rounded(float(decay["time_decay"])),
            "ticker_tags": tickers,
            "ticker_relevance": ticker_relevance,
            "topic_terms": topics,
            "headline": representative.get("title"),
            "summary": representative.get("summary"),
            "market_reaction": reaction,
        })

    opinions: list[dict[str, Any]] = []
    for cluster in opinion_clusters:
        texts = [" ".join((row.get("title") or "", row.get("summary") or "")) for row in cluster["articles"]]
        scores = [opinion_score(text) for text in texts]
        scores = [score for score in scores if score is not None]
        score = _rounded(mean(scores)) if scores else None
        source_scores = [float(row["sentiment_score"]) for row in cluster["articles"] if row.get("sentiment_score") is not None]
        representative = cluster["representative"]
        decay = time_decay(cluster["first_published_at"], bars, "OPINION", news_input.horizon, policy, market=news_input.market)
        eligible, exclusion_reasons = _eligibility(cluster, news_input, decay, policy)
        opinions.append({
            "opinion_id": cluster["cluster_id"].replace("cluster_", "opinion_"),
            "published_at": cluster["first_published_at"],
            "article_count": cluster["article_count"],
            "sources": cluster["sources"],
            "eligible_evidence": eligible, "exclusion_reasons": exclusion_reasons,
            "url": representative.get("url"), "time_decay": decay["time_decay"],
            "record_type": representative.get("record_type"),
            "sentiment": _label(score),
            "sentiment_score": score,
            "source_sentiment_score": _rounded(mean(source_scores)) if source_scores else None,
            "source_sentiment_score_scale": "source supplied [-1,+1]; not combined with opinion lexicon",
            "headline": representative.get("title"),
            "summary": representative.get("summary"),
        })

    unclassified_records = [{
        "published_at": cluster["first_published_at"],
        "article_count": cluster["article_count"],
        "sources": cluster["sources"],
        "headline": cluster["representative"].get("title"),
        "reason": "no supported event category and no explicit opinion/social record type",
    } for cluster in unclassified_clusters]

    all_events, all_opinions = events, opinions
    events = [item for item in all_events if item["eligible_evidence"]]
    opinions = [item for item in all_opinions if item["eligible_evidence"]]
    excluded_evidence = [item for item in all_events + all_opinions if not item["eligible_evidence"]]
    if excluded_evidence:
        warnings.append(f"{len(excluded_evidence)} canonical records excluded from current bias due to relevance/time/source gates")
    overall_direction = _direction_aggregate([event["direction"] for event in events])
    opinion_scores = [float(item["sentiment_score"]) for item in opinions if item["sentiment_score"] is not None]
    sentiment_score = _rounded(mean(opinion_scores)) if opinion_scores else None
    provider_sentiment = news_input.source_sentiment or {}
    if provider_sentiment:
        available = provider_sentiment.get("available_at")
        if failed_domain or provider_sentiment.get("ticker") != news_input.ticker or not available or parse_timestamp(available) > cutoff or (cutoff - parse_timestamp(available)).total_seconds() / 86400 > age_limit:
            warnings.append("provider sentiment aggregate is background-only: target/time/domain eligibility unverified")
            provider_for_bias = {}
        else:
            provider_for_bias = provider_sentiment
    else:
        provider_for_bias = {}
    bullish_percent = provider_for_bias.get("bullish_percent")
    bearish_percent = provider_for_bias.get("bearish_percent")
    provider_direction = (
        "positive" if isinstance(bullish_percent, (int, float)) and isinstance(bearish_percent, (int, float)) and bullish_percent > bearish_percent
        else "negative" if isinstance(bullish_percent, (int, float)) and isinstance(bearish_percent, (int, float)) and bearish_percent > bullish_percent
        else "neutral" if isinstance(bullish_percent, (int, float)) and isinstance(bearish_percent, (int, float))
        else "unknown"
    )
    sentiment_label = _label(sentiment_score) if sentiment_score is not None else (
        f"provider_{provider_direction}" if provider_direction in {"positive", "negative"} else provider_direction
    )
    source_agreement, cross_source_divergence = _source_agreement(opinions)
    opinion_direction = "positive" if sentiment_score is not None and sentiment_score > 0.15 else "negative" if sentiment_score is not None and sentiment_score < -0.15 else "neutral" if sentiment_score is not None else "unknown"
    sentiment_direction = opinion_direction if opinion_direction != "unknown" else provider_direction
    if cross_source_divergence == "high" and opinions:
        sentiment_direction = "mixed"
    if opinion_direction != "unknown" and provider_direction != "unknown":
        if opinion_direction != provider_direction:
            cross_source_divergence = "high" if {opinion_direction, provider_direction} == {"positive", "negative"} else "medium"
            sentiment_direction = "mixed"
        elif cross_source_divergence != "high" and (len(opinions) or provider_direction != "unknown"):
            cross_source_divergence = "low"
    reaction_statuses = [event["market_reaction"].get("status") for event in events
                         if event["market_reaction"].get("status") in {
                             "confirmed_positive", "confirmed_negative",
                             "event_price_divergence_negative", "event_price_divergence_positive",
                         }]
    if not reaction_statuses:
        market_confirmation = "not_assessed"
    elif all(status == "confirmed_positive" for status in reaction_statuses):
        market_confirmation = "positive"
    elif all(status == "confirmed_negative" for status in reaction_statuses):
        market_confirmation = "negative"
    elif len(set(reaction_statuses)) == 1 and reaction_statuses[0] in {
        "event_price_divergence_negative", "event_price_divergence_positive",
    }:
        market_confirmation = "divergent"
    else:
        market_confirmation = "mixed"

    events.sort(key=lambda item: (item["effective_event_score"] is not None, item["effective_event_score"] or 0, item["first_published_at"]), reverse=True)
    positive = [event for event in events if event["direction"] == "positive"]
    negative = [event for event in events if event["direction"] in {"negative", "mixed"}]
    independent_count = len({identity for event in events for identity in event["independent_reporting_ids"]})
    confidence = "medium" if len(events) >= 3 and independent_count >= 2 and all(event["source_quality"] is not None and event["source_quality"] >= 0.5 for event in events) and market_confirmation in {"positive", "negative"} and news_input.coverage.get("calendar_verification") == "supplied_source_evidence" else "low"
    limitations = [
        "Event classification and opinion tone use deterministic keyword screening; they are not a calibrated semantic model.",
        "News direction and sentiment are descriptive and do not generate or adjust Quant P(up).",
        "Daily-bar market reactions are temporal associations, not proof that a headline caused the price move.",
    ]
    if not events and not opinion_scores and provider_direction == "unknown":
        confidence = "unavailable" if not articles else "low"
    status = "not_assessed" if not events and not opinions and not provider_for_bias else "partial" if warnings else "complete"
    return {
        "schema_version": "1.1",
        "status": status,
        "ticker": news_input.ticker,
        "market": news_input.market,
        "horizon": news_input.horizon,
        "asof_timestamp": news_input.asof_timestamp,
        "snapshot_id": news_input.snapshot_id,
        "method": {
            "event_analyst": "deterministic_keyword_screen_v1",
            "sentiment_analyst": "separate_opinion_lexicon_v1",
            "event_opinion_separation": True,
            "news_probability": "not_emitted",
            "market_data": "snapshot_frozen_OHLCV_only",
            "calibrated": False,
        },
        "coverage": {
            "news_domain_status": news_input.news_status,
            "asof_article_count": len(articles),
            "canonical_event_count": len(events),
            "canonical_opinion_count": len(opinions),
            "unclassified_record_count": len(unclassified_records),
            "duplicate_records_merged": duplicates,
            "analyzed_market_bar_count": len(bars),
            "eligible_event_count": len(events), "eligible_opinion_count": len(opinions),
            "excluded_evidence_count": len(excluded_evidence),
            "retrieved_window": news_input.coverage.get("retrieved_window"),
            "observed_publication_start": min((item["published_at"] for item in articles), default=None),
            "observed_publication_end": max((item["published_at"] for item in articles), default=None),
            "evidence_max_age_calendar_days": age_limit,
        },
        "overall_direction": overall_direction,
        "event_strength": _rounded(mean([float(event["event_strength"]) for event in events if event["event_strength"] is not None])) if any(event["event_strength"] is not None for event in events) else None,
        "sentiment": {
            "label": sentiment_label,
            "score": sentiment_score,
            "score_scale": "heuristic -1 to +1; uncalibrated",
            "evidence_basis": "opinion_text_lexicon" if sentiment_score is not None else "provider_aggregate" if provider_direction != "unknown" else "unavailable",
            "provider_direction": provider_direction,
            "consensus_direction": sentiment_direction,
            "provider_aggregate": provider_sentiment or None,
            "confidence": "low" if opinion_scores or provider_direction != "unknown" else "unavailable",
            "dominant_narrative": None,
            "topic_terms": sorted({term for opinion in opinions for term in matched_topics(" ".join((opinion.get("headline") or "", opinion.get("summary") or "")))}),
            "cross_source_divergence": cross_source_divergence,
        },
        "source_agreement": source_agreement,
        "market_confirmation": market_confirmation,
        "major_events": events[:10],
        "background_records": [{key: item.get(key) for key in ("event_id", "opinion_id", "headline", "url", "first_published_at", "published_at", "ticker_relevance", "exclusion_reasons")} for item in excluded_evidence[:10]],
        "catalysts": [{"event_id": event["event_id"], "event_type": event["event_type"], "headline": event["headline"]} for event in positive[:5]],
        "risks": [{"event_id": event["event_id"], "event_type": event["event_type"], "headline": event["headline"]} for event in negative[:5]],
        "opinion_records": opinions[:20],
        "unclassified_records": unclassified_records[:20],
        "confidence": confidence,
        "warnings": warnings,
        "limitations": limitations,
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def freeze_result(path: Path, result: dict[str, Any], *, result_name: str) -> dict[str, Any]:
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    digest = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    freeze = {
        "result_file": result_name,
        "sha256": digest,
        "frozen_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.with_name(path.stem + ".freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return freeze


def load_frozen_result(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a stage result only when its adjacent SHA-256 freeze receipt matches."""
    freeze_path = path.with_name(path.stem + ".freeze.json")
    if not path.is_file() or not freeze_path.is_file():
        raise ValueError(f"frozen result and receipt are required: {path.name}")
    result = json.loads(path.read_text(encoding="utf-8"))
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    actual = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    if freeze.get("result_file") != path.name or freeze.get("sha256") != actual:
        raise ValueError(f"frozen result digest mismatch: {path.name}")
    return result, freeze


def run_news(news_input: NewsInput, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run News on the restricted in-memory contract; this entrypoint has no filesystem path."""
    return analyze_news(news_input, policy)
