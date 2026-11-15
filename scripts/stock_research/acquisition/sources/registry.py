"""Source registry and policy enforcement."""

from __future__ import annotations

from dataclasses import dataclass

from ...common.errors import SourceNotAllowed
from ...config import Policies


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    markets: tuple[str, ...]
    datasets: tuple[str, ...]
    kind: str
    notes: str = ""


SOURCE_DEFINITIONS: dict[str, SourceDefinition] = {
    "sec": SourceDefinition("sec", ("US",), ("filings",), "official"),
    "fred": SourceDefinition("fred", ("US",), ("macro",), "official"),
    "finnhub": SourceDefinition("finnhub", ("US",), ("news", "calendar"), "vendor"),
    "gdelt": SourceDefinition("gdelt", ("US", "HK"), ("news",), "metadata"),
    "hkma": SourceDefinition("hkma", ("HK",), ("news", "macro"), "official"),
    "census": SourceDefinition("census", ("US",), ("macro",), "official"),
    "company_ir": SourceDefinition("company_ir", ("US", "HK"), ("filings", "news"), "authorized_import"),
    "user_import": SourceDefinition("user_import", ("US", "HK"), ("macro", "news", "securities"), "user_import"),
}


def assert_source_allowed(policies: Policies, source_id: str, market: str, dataset: str) -> None:
    if not policies.source_allowed(source_id, market, dataset):
        raise SourceNotAllowed(f"source {source_id} is not allowed for {market}/{dataset}")
    definition = SOURCE_DEFINITIONS.get(source_id)
    if definition is not None and dataset not in definition.datasets:
        raise SourceNotAllowed(f"source {source_id} does not provide {dataset}")


def source_definition(source_id: str) -> SourceDefinition | None:
    return SOURCE_DEFINITIONS.get(source_id)
