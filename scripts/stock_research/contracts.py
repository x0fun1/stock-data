"""Versioned contracts passed between the skill's layers.

The dataclasses in this module are deliberately plain: they can be serialised to
JSON, validated at construction boundaries, and stored without pickle.  They do
not contain business logic beyond small invariants.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

from .common.timeutil import ensure_utc, iso, parse_date, parse_dt


class Market(str, Enum):
    US = "US"
    HK = "HK"


class EvidenceScope(str, Enum):
    TITLE = "title"
    ABSTRACT = "abstract"
    OFFICIAL_EXCERPT = "official_excerpt"
    SOURCE_EXCERPT = "source_excerpt"
    FULL_TEXT = "full_text"
    NUMERIC = "numeric"
    NONE = "none"


class Grade(str, Enum):
    VERY_BULLISH = "极大利好"
    BULLISH = "高度利好"
    MODERATELY_BULLISH = "中度利好"
    NEUTRAL = "中性"
    MODERATELY_BEARISH = "中度利空"
    BEARISH = "高度利空"
    VERY_BEARISH = "极大利空"
    INSUFFICIENT = "证据不足"


GRADE_ORDER = [
    Grade.VERY_BEARISH,
    Grade.BEARISH,
    Grade.MODERATELY_BEARISH,
    Grade.NEUTRAL,
    Grade.MODERATELY_BULLISH,
    Grade.BULLISH,
    Grade.VERY_BULLISH,
]


class SourceKind(str, Enum):
    OFFICIAL = "official"
    LICENSED = "licensed"
    VENDOR = "vendor"
    UNKNOWN = "unknown"
    USER_IMPORT = "user_import"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return iso(value) if isinstance(value, datetime) else value.isoformat()
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    return value


def to_dict(obj: Any) -> dict[str, Any]:
    return _jsonable(obj)


def _date(value: str | date | datetime | None) -> date | None:
    if value is None or value == "":
        return None
    return parse_date(value)


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    return ensure_utc(parse_dt(value))


@dataclass(slots=True)
class ResearchRequest:
    market: Market
    tickers: list[str]
    cutoff: datetime | None = None
    news_lookback_days: int = 30
    output_format: str = "markdown"
    notes: str = ""

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.tickers = [str(t).strip().upper() for t in self.tickers if str(t).strip()]
        if not self.tickers:
            raise ValueError("ResearchRequest requires at least one ticker")
        if self.news_lookback_days < 1:
            raise ValueError("news_lookback_days must be positive")
        if self.cutoff is not None:
            self.cutoff = _dt(self.cutoff)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchRequest":
        # Retired quantitative keys in older request files are ignored.
        tickers = data.get("tickers") or data.get("ticker") or []
        return cls(
            market=Market(str(data.get("market", "US")).upper()),
            tickers=[tickers] if isinstance(tickers, str) else list(tickers),
            cutoff=_dt(data.get("cutoff")),
            news_lookback_days=int(data.get("news_lookback_days", 30)),
            output_format=str(data.get("output_format", "markdown")),
            notes=str(data.get("notes", "")),
        )


@dataclass(slots=True)
class SecurityIdentity:
    security_id: str
    ticker: str
    market: Market
    exchange: str
    security_type: str
    currency: str
    valid_from: date
    valid_to: date | None = None
    name: str = ""
    industry: str = ""
    source_id: str = ""

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.valid_from = _date(self.valid_from)  # type: ignore[assignment]
        self.valid_to = _date(self.valid_to)
        if not self.security_id:
            self.security_id = make_security_id(self.market, self.exchange, self.ticker)

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


def make_security_id(market: Market | str, exchange: str, ticker: str) -> str:
    market = Market(market)
    exchange = (exchange or "UNKNOWN").upper().replace(" ", "")
    ticker = ticker.strip().upper()
    return f"{market.value}:{exchange}:{ticker}"


@dataclass(slots=True)
class MacroObservation:
    series_id: str
    observation_date: date
    value: float
    unit: str
    market: Market | None = None
    available_at: datetime | None = None
    vintage_id: str = ""
    source_id: str = ""
    quality: str = "ok"

    def __post_init__(self) -> None:
        self.observation_date = _date(self.observation_date)  # type: ignore[assignment]
        if self.market is not None:
            self.market = Market(self.market)
        self.available_at = _dt(self.available_at)
        self.value = float(self.value)


@dataclass(slots=True)
class AcquisitionReceipt:
    source_id: str
    dataset: str
    subrequest: str
    params_redacted: dict[str, Any] = field(default_factory=dict)
    requested_at: datetime | None = None
    received_at: datetime | None = None
    status: str = "ok"
    http_status: int | None = None
    content_type: str = ""
    byte_count: int = 0
    sha256: str = ""
    coverage_start: date | None = None
    coverage_end: date | None = None
    exhausted: bool = False
    cap_reached: bool = False
    record_count: int = 0
    error: str = ""
    raw_path: str = ""

    def __post_init__(self) -> None:
        self.requested_at = _dt(self.requested_at)
        self.received_at = _dt(self.received_at)
        self.coverage_start = _date(self.coverage_start)
        self.coverage_end = _date(self.coverage_end)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(slots=True)
class ContextEvent:
    event_id: str
    market: Market
    security_ids: list[str]
    title: str
    published_at: datetime | None
    available_at: datetime | None
    received_at: datetime | None
    source_id: str
    source_kind: SourceKind
    evidence_scope: EvidenceScope
    url: str = ""
    body: str = ""
    evidence_locator: str = ""
    event_cluster: str = ""
    grade: Grade = Grade.INSUFFICIENT
    grade_method: str = "insufficient_evidence"
    grade_version: str = ""
    grade_reason: str = ""
    fact_summary: str = ""
    # Retained so news stored by earlier versions keeps loading unchanged.
    price_reaction: dict[str, Any] = field(default_factory=dict)
    in_model: bool = False
    permissions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.published_at = _dt(self.published_at)
        self.available_at = _dt(self.available_at)
        self.received_at = _dt(self.received_at)
        self.source_kind = SourceKind(self.source_kind)
        self.evidence_scope = EvidenceScope(self.evidence_scope)
        self.grade = Grade(self.grade)


@dataclass(slots=True)
class RunManifest:
    run_id: str
    created_at: datetime
    request: dict[str, Any]
    policy_version: str
    code_version: str
    snapshot: dict[str, Any]
    stages: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.created_at = ensure_utc(self.created_at)


@dataclass(slots=True)
class ReportBundle:
    request: ResearchRequest
    securities: list[SecurityIdentity]
    context_events: list[ContextEvent]
    run_manifest: RunManifest
    macro_background: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    news_impact_summary: dict[str, Any] = field(default_factory=dict)
