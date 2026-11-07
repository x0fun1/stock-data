"""Historically effective, publicly available industry/size-control metadata."""
from __future__ import annotations

import pandas as pd

from ..multifactor.preprocessing import confirmed


def metadata_asof(frame: pd.DataFrame | None, security_id: str, cutoff, known_by) -> dict:
    if frame is None or frame.empty:
        return {}
    required = {"security_id", "effective_from", "available_at"}
    if not required <= set(frame):
        return {}
    selected = frame[frame["security_id"].astype(str) == security_id].copy()
    start = pd.to_datetime(selected["effective_from"], utc=True, errors="coerce").dt.normalize()
    end = pd.to_datetime(selected.get("effective_to", pd.Series(None, index=selected.index)), utc=True, errors="coerce").dt.normalize()
    available = pd.to_datetime(selected["available_at"], utc=True, errors="coerce")
    cutoff_day = pd.Timestamp(cutoff, tz="UTC")
    selected = selected[available.notna() & (available <= known_by) & (start <= cutoff_day) & (end.isna() | (end >= cutoff_day))]
    if selected.empty:
        return {}
    selected["_available"] = pd.to_datetime(selected["available_at"], utc=True)
    row = selected.sort_values(["effective_from", "_available"], kind="stable").iloc[-1]
    return {"industry": str(row.get("industry", "")),
            "industry_pit_confirmed": confirmed(row.get("pit_verified", False)),
            "size_pit_confirmed": confirmed(row.get("size_pit_confirmed", False)),
            "available_at": row["_available"].isoformat(),
            "source_id": str(row.get("source_id", ""))}
