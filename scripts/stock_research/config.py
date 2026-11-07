"""Skill paths and policy loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common.hashing import canonical_hash
from .common.jsonio import read_json


def default_skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_runtime_root(skill_root: Path | None = None) -> Path:
    override = os.environ.get("STOCK_RESEARCH_RUNTIME")
    if override:
        return Path(override)
    root = Path(skill_root or default_skill_root())
    return root / "runtime"


@dataclass
class Policies:
    defaults: dict[str, Any]
    sources: dict[str, Any]
    calendars: dict[str, Any]
    skill_root: Path

    @property
    def version(self) -> str:
        return str(self.defaults.get("version", "unknown"))

    @property
    def hash(self) -> str:
        return canonical_hash(
            {
                "defaults": self.defaults,
                "sources": self.sources,
                "calendars": self.calendars,
            }
        )

    def source(self, source_id: str) -> dict[str, Any]:
        return dict(self.sources.get("sources", {}).get(source_id, {}))

    def source_allowed(self, source_id: str, market: str, dataset: str) -> bool:
        policy = self.sources.get("sources", {}).get(source_id)
        if policy is None:
            return False
        return market.upper() in {m.upper() for m in policy.get("markets", [])} and dataset in set(policy.get("datasets", []))

    def is_prohibited(self, key: str, market: str) -> bool:
        item = self.sources.get("prohibited", {}).get(key)
        if not item:
            return False
        return market.upper() in {m.upper() for m in item.get("markets", [])}


def load_policies(skill_root: str | Path | None = None) -> Policies:
    root = Path(skill_root) if skill_root else default_skill_root()
    policies_dir = root / "policies"
    return Policies(
        defaults=read_json(policies_dir / "defaults.json"),
        sources=read_json(policies_dir / "source-policy.json"),
        calendars=read_json(policies_dir / "calendars.json"),
        skill_root=root,
    )
