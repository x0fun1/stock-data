from __future__ import annotations


class ResearchError(Exception):
    """Base error for a bounded research capability."""


class DataError(ResearchError):
    """Input data is missing, malformed, or semantically invalid."""


class PolicyError(ResearchError):
    """The requested source, use, or output violates policy."""


class CoverageError(ResearchError):
    """A calendar, action, or source range has no validated coverage."""


class MissingData(DataError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class SourceNotAllowed(PolicyError):
    pass
