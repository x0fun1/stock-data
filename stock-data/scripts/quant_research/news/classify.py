"""Conservative deterministic event and opinion screening for the News layer."""

from __future__ import annotations

import re
from typing import Any

#: Version of the deterministic keyword vocabulary below.  The lists are still
#: compiled in (so an offline install cannot fail), but every News artifact
#: records this version and the supported languages, and an unknown language is
#: disclosed instead of being treated as "no event".
LEXICON_VERSION = "news_keyword_en_zh_v1"
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh")
COVERAGE_NOTE = (
    "deterministic English/Chinese keyword screening; other languages and paraphrases are "
    "under-classified and surface as unclassified records"
)

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
    "no. 1", "number one", "market leader", "top-ranked",
    "positive", "bullish", "增长", "上调", "获批", "超预期", "利好", "增长强劲",
)
NEGATIVE_TERMS = (
    "cuts guidance", "cut guidance", "guidance cut", "misses estimates", "missed estimates",
    "revenue decline", "profit falls", "demand weak", "downgrade", "investigation", "lawsuit",
    "recall", "layoff", "default", "dilution", "negative", "bearish", "下调", "不及预期",
    "调查", "诉讼", "裁员", "违约", "利空", "需求疲软",
)

TOPIC_TERMS = ("AI", "data center", "demand", "guidance", "earnings", "rates", "regulation", "China", "supply", "product")

#: Genuine uncertainty.  These must stay separate from negation: a hedge says the
#: claim is unverified, while a denial is a verified statement *about* a claim.
_HEDGE = re.compile(
    r"\b(?:reportedly|allegedly|may|might|could|possibly|speculat(?:e|es|ed|ion)|if)\b"
    r"|据报道|可能|或将|如果"
)
#: Unverified-claim nouns.  A denial of a rumour is not itself a rumour, so these
#: are only treated as uncertainty when no denial verb is present.
_UNVERIFIED_CLAIM = re.compile(r"\b(?:rumou?rs?|rumou?red|unconfirmed)\b|传闻|未经证实")

#: Explicit negation of a claim.  ``no`` alone is deliberately absent: "No. 1
#: vendor" and "no comment" are not denials, and treating every bare ``no`` as
#: uncertainty turned confirmed negative headlines into "unknown" — a systematic
#: bias that reads bad news as neutral.  A denial with no directional content is
#: reported as ``neutral`` (a factual statement), not as unknown.
_NEGATION = re.compile(
    r"\b(?:denies|denied|deny|refutes|refuted|rebuts|rebutted|dismisses|dismissed|"
    r"no longer|does not|doesn't|did not|didn't|will not|won't|is not|isn't|are not|aren't|"
    r"fails? to|failed to|without|rejects|rejected)\b|否认|驳斥|拒绝|没有|未能|不再"
)


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
    negated = _NEGATION.search(normalized) is not None
    if _HEDGE.search(normalized):
        return "unknown"
    if _UNVERIFIED_CLAIM.search(normalized) and not negated:
        return "unknown"
    positive = any(_contains(normalized, phrase) for phrase in POSITIVE_TERMS)
    negative = any(_contains(normalized, phrase) for phrase in NEGATIVE_TERMS)
    if negated and (positive or negative):
        # The scope of a denial is not parsed: "denies the lawsuit" and "denies
        # the upgrade" have opposite polarity.  Guessing here is how negative
        # news gets quietly read as neutral, so the honest answer is unknown.
        return "unknown"
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    if negated:
        # An explicit denial/rejection with no directional phrase is a factual
        # statement, not missing evidence.  Calling it "unknown" would hide it.
        return "neutral"
    return "unknown"


def opinion_score(text: str) -> float | None:
    if direction_for(text) == "unknown":
        return None
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
