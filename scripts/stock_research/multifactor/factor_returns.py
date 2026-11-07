"""Local, explicit-unit import of public Ken French daily factor archives.

These are realised common-factor returns for attribution.  They are neither
stock characteristics nor known-in-advance forecasts.  Unknown publication
times remain unknown; the imported latest vintage is descriptive only.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
import re
import zipfile

import numpy as np
import pandas as pd

from ..common.hashing import sha256_file
from ..common.timeutil import now_utc
from ..config import load_policies
from ..storage.curated_store import CuratedStore


def parse_daily_archive(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError("daily factor archive must contain exactly one CSV")
            content = archive.read(members[0]).decode("utf-8-sig")
    else:
        content = path.read_text(encoding="utf-8-sig")
    aliases = {"mkt-rf": "MKT", "mkt_rf": "MKT", "smb": "SMB", "hml": "HML", "rf": "RF", "mom": "MOM", "umd": "MOM"}
    header = None
    rows = []
    for cells in csv.reader(io.StringIO(content)):
        if not cells:
            continue
        first = cells[0].strip()
        if not re.fullmatch(r"\d{8}", first):
            normal = [aliases.get(x.strip().lower()) for x in cells[1:]]
            if normal and all(normal):
                header = normal
            continue
        if header is None:
            raise ValueError("daily numeric rows have no supported factor header")
        if len(cells) != len(header) + 1:
            raise ValueError("daily factor row has an inconsistent column count")
        values = [float(x.strip()) for x in cells[1:]]
        # French missing codes are -99.99/-999 percent; do not interpret as losses.
        values = [None if x in (-99.99, -999.0, -999.99) else x / 100.0 for x in values]
        if any(x is not None and (not np.isfinite(x) or abs(x) > 1.0) for x in values):
            raise ValueError("daily factor value is nonfinite or outside plausible percent units")
        rows.append({"date": pd.to_datetime(first, format="%Y%m%d").strftime("%Y-%m-%d"), **dict(zip(header, values))})
    frame = pd.DataFrame(rows)
    if frame.empty or frame.duplicated("date").any():
        raise ValueError("daily factor archive is empty or repeats dates")
    return frame.sort_values("date").reset_index(drop=True)


def import_factor_archives(ff3_path: str | Path, momentum_path: str | Path,
                           runtime_root: str | Path) -> dict:
    if not load_policies().source_allowed("ken_french", "US", "factor_returns"):
        raise ValueError("Ken French local risk-factor import is not allowed by this package's source policy")
    ff3, momentum = parse_daily_archive(ff3_path), parse_daily_archive(momentum_path)
    if not {"MKT", "SMB", "HML", "RF"} <= set(ff3) or "MOM" not in momentum:
        raise ValueError("requires FF3+RF daily archive and daily Momentum archive")
    frame = ff3.merge(momentum[["date", "MOM"]], on="date", how="inner", validate="one_to_one")
    frame["unit"] = "decimal_return"
    frame["market"] = "US"
    frame["source_id"] = "ken_french"
    frame["available_at"] = None
    frame["quality"] = "latest_vintage_context_only"
    frame["received_at"] = now_utc().isoformat()
    store = CuratedStore(runtime_root)
    metadata = {"use": "realised_factor_risk_attribution", "not_prediction_input": True,
                "historical_availability_verified": False,
                "source_units": "percent", "stored_units": "decimal_return",
                "archives": [{"path": str(Path(p).resolve()), "sha256": sha256_file(p)} for p in (ff3_path, momentum_path)]}
    version = store.save("factor_returns", frame, metadata)
    return {"dataset": "factor_returns", "version": version, "rows": len(frame),
            "date_start": frame["date"].min(), "date_end": frame["date"].max(), **metadata}

