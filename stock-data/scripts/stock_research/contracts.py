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
from typing import Any, Iterable, Mapping

from .common.timeutil import ensure_utc, iso, now_utc, parse_date, parse_dt


class Market(str, Enum):
    US = "US"
    HK = "HK"


class TaskType(str, Enum):
    RESEARCH = "research"
    MAINTENANCE = "maintenance"
    STUDY = "study"
    VALIDATE = "validate"
    REPRODUCE = "reproduce"


class Target(str, Enum):
    ABSOLUTE = "absolute"
    EXCESS = "excess"
    QUANTILE = "quantile"
    PATH_RISK = "path_risk"
    LOSS_THRESHOLD = "loss_threshold"


class DataLevel(str, Enum):
    UNAVAILABLE = "unavailable"
    CONTEXT = "context"
    DESCRIPTIVE = "descriptive"
    RESEARCH = "research"


class CapabilityState(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    NOT_EXECUTED = "not_executed"
    INSUFFICIENT_DATA = "insufficient_data"
    DESCRIPTIVE = "descriptive"
    NOT_PUBLISHED = "not_published"


class ValidationState(str, Enum):
    PENDING = "pending"
    INVALID = "invalid"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


class ActionType(str, Enum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    SPECIAL_DIVIDEND = "special_dividend"
    RIGHTS = "rights"
    SPIN_OFF = "spin_off"
    MERGER = "merger"
    OTHER = "other"


SUPPORTED_ACTION_TYPES = {
    ActionType.SPLIT,
    ActionType.REVERSE_SPLIT,
    ActionType.CASH_DIVIDEND,
    ActionType.SPECIAL_DIVIDEND,
}


class EvidenceScope(str, Enum):
    TITLE = "title"
    ABSTRACT = "abstract"
    OFFICIAL_EXCERPT = "official_excerpt"
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


class MissingReason(str, Enum):
    NO_DATA = "no_data"
    WINDOW_INSUFFICIENT = "window_insufficient"
    ZERO_DENOMINATOR = "zero_denominator"
    NONPOSITIVE_BASE = "nonpositive_base"
    UNSUPPORTED_ACTION = "unsupported_action"
    INCOMPLETE_PATH = "incomplete_path"
    NO_OPEN = "no_open"
    NO_CLOSE = "no_close"
    SUSPENDED = "suspended"
    STALE = "stale"
    NOT_AVAILABLE = "not_available"
    UNIVERSE_REQUIRED = "universe_required"
    INDUSTRY_NOT_APPLICABLE = "industry_not_applicable"
    NOT_IMPLEMENTED = "not_implemented"
    NO_OVERLAP = "no_overlap"
    PENDING_MATURITY = "pending_maturity"
    SOURCE_NOT_ELIGIBLE = "source_not_eligible"


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
    benchmark: str | None = None
    cutoff: datetime | None = None
    horizons: tuple[int, ...] = (1, 5, 20, 60)
    targets: tuple[Target, ...] = (Target.ABSOLUTE, Target.EXCESS)
    news_lookback_days: int = 30
    update_policy: str = "cache_first"
    task: TaskType = TaskType.RESEARCH
    output_format: str = "markdown"
    universe: list[str] = field(default_factory=list)
    notes: str = ""
    quant_baseline: bool = True
    multifactor: bool = True

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.tickers = [str(t).strip().upper() for t in self.tickers if str(t).strip()]
        if not self.tickers:
            raise ValueError("ResearchRequest requires at least one ticker")
        if self.benchmark:
            self.benchmark = str(self.benchmark).strip().upper()
        self.horizons = tuple(sorted({int(h) for h in self.horizons if int(h) > 0}))
        if not self.horizons:
            raise ValueError("ResearchRequest requires at least one horizon")
        self.targets = tuple(Target(t) for t in self.targets)
        if self.news_lookback_days < 1:
            raise ValueError("news_lookback_days must be positive")
        if self.cutoff is not None:
            self.cutoff = ensure_utc(self.cutoff)
        self.task = TaskType(self.task)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResearchRequest":
        return cls(
            market=Market(str(data.get("market", "US")).upper()),
            tickers=list(data.get("tickers") or data.get("ticker") or []),
            benchmark=data.get("benchmark"),
            cutoff=_dt(data.get("cutoff")),
            horizons=tuple(data.get("horizons") or (1, 5, 20, 60)),
            targets=tuple(Target(t) for t in (data.get("targets") or ("absolute", "excess"))),
            news_lookback_days=int(data.get("news_lookback_days", 30)),
            update_policy=str(data.get("update_policy", "cache_first")),
            task=TaskType(str(data.get("task", "research"))),
            output_format=str(data.get("output_format", "markdown")),
            universe=list(data.get("universe") or []),
            notes=str(data.get("notes", "")),
            quant_baseline=bool(data.get("quant_baseline", True)),
            multifactor=bool(data.get("multifactor", True)),
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
class PriceBar:
    security_id: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    amount: float | None = None
    currency: str = ""
    source_id: str = ""
    available_at: datetime | None = None
    quality: str = "ok"

    def __post_init__(self) -> None:
        self.date = _date(self.date)  # type: ignore[assignment]
        self.available_at = _dt(self.available_at)
        for name in ("open", "high", "low", "close", "volume", "amount"):
            value = getattr(self, name)
            if value is not None and (value != value or value in (float("inf"), float("-inf"))):
                raise ValueError(f"PriceBar.{name} must be finite, got {value!r}")


@dataclass(slots=True)
class CorporateAction:
    security_id: str
    ex_date: date
    action_type: ActionType
    q: float = 1.0
    cash_old: float = 0.0
    currency: str = ""
    announced_at: datetime | None = None
    available_at: datetime | None = None
    accession: str = ""
    source_id: str = ""
    confirmed: bool = True
    supported: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        self.ex_date = _date(self.ex_date)  # type: ignore[assignment]
        self.action_type = ActionType(self.action_type)
        self.announced_at = _dt(self.announced_at)
        self.available_at = _dt(self.available_at)
        self.q = float(self.q)
        self.cash_old = float(self.cash_old)
        if self.q <= 0:
            raise ValueError("CorporateAction.q must be positive")
        if self.action_type not in SUPPORTED_ACTION_TYPES:
            self.supported = False
        if not self.supported:
            self.confirmed = False


@dataclass(slots=True)
class ActionCoverage:
    security_id: str
    start: date
    end: date
    complete: bool
    evidence: str = ""
    unresolved: list[str] = field(default_factory=list)
    source_id: str = ""

    def __post_init__(self) -> None:
        self.start = _date(self.start)  # type: ignore[assignment]
        self.end = _date(self.end)  # type: ignore[assignment]

    def covers(self, start: date, end: date) -> bool:
        return self.complete and self.start <= start and self.end >= end


@dataclass(slots=True)
class FinancialFact:
    security_id: str
    concept: str
    value: float
    unit: str
    period_end: date
    period_start: date | None = None
    fiscal_year: int | None = None
    fiscal_period: str = ""
    form: str = ""
    accession: str = ""
    filed_at: datetime | None = None
    available_at: datetime | None = None
    source_id: str = ""
    quality: str = "ok"

    def __post_init__(self) -> None:
        self.period_end = _date(self.period_end)  # type: ignore[assignment]
        self.period_start = _date(self.period_start)
        self.filed_at = _dt(self.filed_at)
        self.available_at = _dt(self.available_at)
        self.value = float(self.value)

    @property
    def is_instant(self) -> bool:
        return self.period_start is None


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
class FeatureObservation:
    name: str
    value: float | None
    as_of: date
    status: str = "ok"
    reason: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    display_only: bool = False

    def __post_init__(self) -> None:
        self.as_of = _date(self.as_of)  # type: ignore[assignment]
        if self.value is not None:
            self.value = float(self.value)
            if self.value != self.value or self.value in (float("inf"), float("-inf")):
                self.value = None
                self.status = "missing"
                self.reason = MissingReason.ZERO_DENOMINATOR.value

    def missing(self, reason: MissingReason | str) -> "FeatureObservation":
        self.value = None
        self.status = "missing"
        self.reason = reason.value if isinstance(reason, MissingReason) else str(reason)
        return self


@dataclass(slots=True)
class WindowResult:
    status: str
    entry_date: date | None
    end_date: date | None
    return_value: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    missing_reason: str = ""
    path_complete: bool = False
    actions_applied: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LabelRecord:
    security_id: str
    market: Market
    cutoff_date: date
    horizon: int
    target: Target
    entry_date: date | None = None
    end_date: date | None = None
    return_value: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    label: int | None = None
    label_excess: int | None = None
    status: str = "ok"
    missing_reason: str = ""
    matured_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.cutoff_date = _date(self.cutoff_date)  # type: ignore[assignment]
        self.entry_date = _date(self.entry_date)
        self.end_date = _date(self.end_date)
        self.target = Target(self.target)
        self.matured_at = _dt(self.matured_at)

    @property
    def mature(self) -> bool:
        return self.matured_at is not None and self.matured_at <= now_utc()


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
class DatasetManifest:
    dataset_id: str
    market: Market
    version: str
    created_at: datetime
    row_count: int
    security_count: int
    date_start: date | None
    date_end: date | None
    factor_version: str
    label_version: str
    source_manifests: list[str] = field(default_factory=list)
    exclusions: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.created_at = ensure_utc(self.created_at)
        self.date_start = _date(self.date_start)
        self.date_end = _date(self.date_end)


@dataclass(slots=True)
class ExperimentProtocol:
    name: str
    market: Market
    horizons: tuple[int, ...]
    targets: tuple[Target, ...]
    factors: tuple[str, ...]
    train_start: date
    train_end: date
    dev_start: date
    dev_end: date
    cal_start: date
    cal_end: date
    test_start: date
    test_end: date
    candidate_models: tuple[str, ...] = ("logistic_l2",)
    l2_grid: tuple[float, ...] = (0.01, 0.1, 1.0)
    seed: int = 20261106
    registered_at: datetime | None = None
    code_version: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.horizons = tuple(sorted(int(h) for h in self.horizons))
        self.targets = tuple(Target(t) for t in self.targets)
        for name in ("train_start", "train_end", "dev_start", "dev_end", "cal_start", "cal_end", "test_start", "test_end"):
            setattr(self, name, _date(getattr(self, name)))
        self.registered_at = _dt(self.registered_at)
        if not (self.train_start <= self.train_end < self.dev_start <= self.dev_end < self.cal_start <= self.cal_end < self.test_start <= self.test_end):
            raise ValueError("Experiment blocks must be ordered and non-overlapping")


@dataclass(slots=True)
class ModelArtifact:
    model_id: str
    market: Market
    horizon: int
    target: Target
    status: str
    factor_names: list[str]
    transformer: dict[str, Any]
    coefficients: list[float]
    intercept: float
    baseline_rate: float
    dataset_id: str
    dataset_version: str
    evidence_id: str = ""
    calibrator: dict[str, Any] | None = None
    created_at: datetime | None = None
    code_version: str = ""
    seed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.target = Target(self.target)
        self.created_at = _dt(self.created_at)
        if len(self.factor_names) != len(self.coefficients):
            raise ValueError("factor_names and coefficients must have equal length")
        self.baseline_rate = float(self.baseline_rate)


@dataclass(slots=True)
class ValidationEvidence:
    evidence_id: str
    model_id: str
    market: Market
    horizon: int
    target: Target
    status: ValidationState
    sample_count: int
    independent_dates: int
    date_start: date | None
    date_end: date | None
    metrics: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    skill_ci: dict[str, Any] = field(default_factory=dict)
    stability: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.target = Target(self.target)
        self.status = ValidationState(self.status)
        self.date_start = _date(self.date_start)
        self.date_end = _date(self.date_end)
        self.created_at = _dt(self.created_at)


@dataclass(slots=True)
class CapabilityDecision:
    market: Market
    horizon: int
    target: Target
    state: CapabilityState
    data_level: DataLevel
    reason: str
    security_id: str = ""
    model_id: str = ""
    evidence_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.target = Target(self.target)
        self.state = CapabilityState(self.state)
        self.data_level = DataLevel(self.data_level)


@dataclass(slots=True)
class QuantitativeResult:
    market: Market
    security_id: str
    horizon: int
    target: Target
    state: CapabilityState
    data_level: DataLevel
    reason: str = ""
    probability: float | None = None
    baseline_probability: float | None = None
    quantiles: dict[str, float | None] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    evidence_id: str = ""
    validation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.market = Market(self.market)
        self.target = Target(self.target)
        self.state = CapabilityState(self.state)
        self.data_level = DataLevel(self.data_level)


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
    results: list[QuantitativeResult]
    decisions: list[CapabilityDecision]
    context_events: list[ContextEvent]
    features: dict[str, list[FeatureObservation]]
    run_manifest: RunManifest
    limitations: list[str] = field(default_factory=list)
    headline: str = ""
    baseline_forecasts: dict[str, dict[str, Any]] = field(default_factory=dict)
    multifactor_snapshot: dict[str, Any] = field(default_factory=dict)


def as_json_list(records: Iterable[Any]) -> list[dict[str, Any]]:
    return [to_dict(r) for r in records]
