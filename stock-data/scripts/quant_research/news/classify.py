"""Conservative deterministic event and opinion screening for the News layer."""

from __future__ import annotations

import re
from typing import Any


EVENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EARNINGS", ("earnings", "quarterly results", "quarter results", "eps", "revenue beat", "revenue miss")),
    ("GUIDANCE", ("guidance", "outlook", "forecast raised", "forecast cut")),
    ("ANALYST_REVISION", ("analyst upgrade", "analyst downgrade", "price target", "eps estimate", "rating raised", "rating cut")),
    ("PRODUCT", ("product launch", "launches", "unveils", "new product", "product release")),
    ("MERGER_ACQUISITION", ("acquisition", "acquires", "merger", "to acquire", "buyout")),
    ("BUYBACK", ("buyback", "share repurchase", "repurchase program")),
    ("DIVIDEND", ("dividend", "special distribution")),
    ("CAPITAL_RAISE", ("share offering", "capital raise", "secondary offering", "debt offering", "convertible notes")),
    ("REGULATORY", ("regulator", "regulatory", "antitrust", "ftc", "doj probe", "sec investigation", "license revoked")),
    ("LEGAL", ("lawsuit", "sued", "court ruling", "settlement", "class action", "indictment")),
    ("MANAGEMENT", ("ceo resign", "ceo retires", "appoints ceo", "chief executive", "management change")),
    ("INSIDER", ("insider buying", "insider selling", "director buys", "director sells", "filed form 4")),
    ("SUPPLY_CHAIN", ("supply chain", "component shortage", "production disruption", "factory shutdown", "supplier")),
    ("COMPETITION", ("market share", "competitor", "competitive pressure", "price war")),
    ("MACRO", ("federal reserve", "fed rate", "interest rate", "inflation", "cpi", "pce", "unemployment", "tariff", "geopolitical", "central bank")),
)

POSITIVE_TERMS = (
    "raises guidance", "raised guidance", "guidance raised", "beats estimates", "beat estimates",
    "beats revenue", "revenue beats", "record revenue", "record profit", "approval granted", "wins contract", "contract win",
    "strong demand", "revenue growth", "profit rises", "upgrade", "dividend increase",
    "positive", "bullish", "增长", "上调", "获批", "超预期", "利好", "增长强劲",
)
NEGATIVE_TERMS = (
    "cuts guidance", "cut guidance", "guidance cut", "misses estimates", "missed estimates",
    "revenue decline", "profit falls", "demand weak", "downgrade", "investigation", "lawsuit",
    "recall", "layoff", "default", "dilution", "negative", "bearish", "下调", "不及预期",
    "调查", "诉讼", "裁员", "违约", "利空", "需求疲软",
)

TOPIC_TERMS = ("AI", "data center", "demand", "guidance", "earnings", "rates", "regulation", "China", "supply", "product")


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.casefold()
    if re.fullmatch(r"[a-z0-9 ]+", phrase):
        return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text.casefold()) is not None
    return phrase in text.casefold()


def event_type_for(text: str, hint: str | None = None) -> str:
    allowed = {kind for kind, _ in EVENT_PATTERNS}
    if hint:
        value = hint.strip().upper()
        if value in allowed:
            return value
        for kind in allowed:
            if value.startswith(kind + "_"):
                return kind
    normalized = text.casefold()
    for kind, phrases in EVENT_PATTERNS:
        if any(_contains(normalized, phrase) for phrase in phrases):
            return kind
    return "UNCLASSIFIED"


def direction_for(text: str) -> str:
    normalized = text.casefold()
    positive = any(_contains(normalized, phrase) for phrase in POSITIVE_TERMS)
    negative = any(_contains(normalized, phrase) for phrase in NEGATIVE_TERMS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "unknown"


def opinion_score(text: str) -> float | None:
    normalized = text.casefold()
    positive_hits = sum(1 for phrase in POSITIVE_TERMS if _contains(normalized, phrase))
    negative_hits = sum(1 for phrase in NEGATIVE_TERMS if _contains(normalized, phrase))
    total = positive_hits + negative_hits
    if not total:
        return None
    return (positive_hits - negative_hits) / total


def matched_topics(text: str) -> list[str]:
    return [term for term in TOPIC_TERMS if _contains(text, term)]


def event_text(cluster: dict[str, Any]) -> str:
    return " ".join(
        str(value) for article in cluster["articles"]
        for value in (article.get("title"), article.get("summary")) if value
    )
