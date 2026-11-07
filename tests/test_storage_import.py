from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from stock_research.common.errors import DataError, SourceNotAllowed
from stock_research.common.timeutil import now_utc
from stock_research.contracts import AcquisitionReceipt
from stock_research.control.maintenance import import_file
from stock_research.storage import Catalog, CuratedStore, RawStore


def test_raw_store_is_content_addressed_and_idempotent(tmp_path):
    store = RawStore(tmp_path)
    receipt = AcquisitionReceipt(source_id="user_import", dataset="prices", subrequest="one", received_at=now_utc())
    payload = b'{"ok": true}'
    manifest = store.save_payload("user_import", "prices", payload, receipt, ".json")
    assert manifest.exists()
    assert receipt.sha256
    assert store.verify(manifest)
    second = AcquisitionReceipt(source_id="user_import", dataset="prices", subrequest="one", received_at=now_utc())
    manifest2 = store.save_payload("user_import", "prices", payload, second, ".json")
    assert manifest2 == manifest
    receipt.raw_path = str(Path(receipt.raw_path))
    Path(receipt.raw_path).write_bytes(b"tampered")
    assert not store.verify(manifest)


def test_curated_store_versions_and_snapshot(tmp_path):
    store = CuratedStore(tmp_path)
    frame = pd.DataFrame({"security_id": ["X"], "date": ["2024-01-02"], "close": [10.0]})
    v1 = store.save("prices", frame, {"market": "US"})
    v2 = store.save("prices", frame, {"market": "US"})
    assert v1 != v2
    assert len(store.versions("prices")) == 2
    assert store.load("prices", v1).equals(frame)
    snapshot = store.snapshot(["prices", "news"])
    assert snapshot["datasets"]["prices"]["version"] in {v1, v2}
    assert snapshot["missing"] == ["news"]


def test_import_prices_and_actions(tmp_path):
    prices = pd.DataFrame(
        [
            {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
            {"date": "2024-01-03", "open": 10.5, "high": 11, "low": 10, "close": 10.8, "volume": 1200},
        ]
    )
    path = tmp_path / "prices.csv"
    prices.to_csv(path, index=False)
    result = import_file("prices", path, "US", tmp_path / "runtime", ticker="X")
    assert result["dataset"] == "prices"
    assert result["rows"] == 2
    store = CuratedStore(tmp_path / "runtime")
    loaded = store.load("prices")
    assert loaded["security_id"].iloc[0] == "US:XNAS:X"
    assert loaded["market"].iloc[0] == "US"

    actions = tmp_path / "actions.json"
    actions.write_text(json.dumps([{"security_id": "US:XNAS:X", "ex_date": "2024-06-05", "action_type": "split", "q": 2}]), encoding="utf-8")
    result2 = import_file("actions", actions, "US", tmp_path / "runtime")
    assert result2["rows"] == 1


def test_import_requires_available_at_for_financial_facts(tmp_path):
    frame = pd.DataFrame([{"security_id": "US:XNAS:X", "concept": "Revenues", "value": 100, "period_end": "2024-12-31"}])
    path = tmp_path / "facts.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(DataError):
        import_file("financial_facts", path, "US", tmp_path / "runtime")


def test_prohibited_source_is_rejected(tmp_path):
    frame = pd.DataFrame([{"date": "2024-01-02", "close": 10}])
    path = tmp_path / "p.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(SourceNotAllowed):
        import_file("prices", path, "HK", tmp_path / "runtime", ticker="0700", source_id="hkexnews")
