from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from stock_research.common.errors import DataError, SourceNotAllowed
from stock_research.common.timeutil import now_utc
from stock_research.contracts import AcquisitionReceipt
from stock_research.control.maintenance import import_file
from stock_research.storage import Catalog, CuratedStore, RawStore


def test_raw_store_is_content_addressed_and_idempotent(tmp_path):
    store = RawStore(tmp_path)
    receipt = AcquisitionReceipt(source_id="user_import", dataset="news", subrequest="one", received_at=now_utc())
    payload = b'{"ok": true}'
    manifest = store.save_payload("user_import", "news", payload, receipt, ".json")
    assert manifest.exists() and receipt.sha256 and store.verify(manifest)
    second = AcquisitionReceipt(source_id="user_import", dataset="news", subrequest="one", received_at=now_utc())
    assert store.save_payload("user_import", "news", payload, second, ".json") == manifest
    Path(receipt.raw_path).write_bytes(b"tampered")
    assert not store.verify(manifest)


def test_curated_store_versions_and_snapshot(tmp_path):
    store = CuratedStore(tmp_path)
    records = [{"series_id": "DGS10", "observation_date": "2024-01-02", "value": 4.0}]
    v1 = store.save("macro", records, {"market": "US"})
    v2 = store.save("macro", records, {"market": "US"})
    assert v1 != v2 and len(store.versions("macro")) == 2
    assert store.load("macro", v1) == records
    assert store.metadata("macro", v1)["path"].endswith(".json.gz")
    snapshot = store.snapshot(["macro", "news"])
    assert snapshot["datasets"]["macro"]["version"] in {v1, v2}
    assert snapshot["missing"] == ["news"]


def test_import_macro_and_securities(tmp_path):
    path = tmp_path / "macro.csv"
    path.write_text("series_id,observation_date,value,available_at\nDGS10,2026/11/05,4.1,2026-11-06T21:15:00+00:00\n", encoding="utf-8")
    result = import_file("macro", path, "US", tmp_path / "runtime")
    assert result["dataset"] == "macro" and result["rows"] == 1
    row = CuratedStore(tmp_path / "runtime").load("macro")[0]
    assert row["observation_date"] == "2026-11-05" and row["value"] == 4.1
    securities = tmp_path / "securities.json"
    securities.write_text(json.dumps([{"ticker": "X", "market": "US", "valid_from": "2000-01-01"}]), encoding="utf-8")
    assert import_file("securities", securities, "US", tmp_path / "runtime")["rows"] == 1


def test_import_rejects_missing_columns_retired_kinds_and_unlisted_sources(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text("date,close\n2024-01-02,10\n", encoding="utf-8")
    with pytest.raises(DataError, match="missing columns"):
        import_file("macro", path, "US", tmp_path / "runtime")
    with pytest.raises(DataError, match="unknown import kind"):
        import_file("prices", path, "US", tmp_path / "runtime")
    with pytest.raises(SourceNotAllowed):
        import_file("news", path, "HK", tmp_path / "runtime", source_id="hkexnews")


def test_news_json_import_preserves_security_ids_and_empty_objects(tmp_path):
    from stock_research.control.ingest import news_from_frame
    path = tmp_path / 'news.json'
    path.write_text(json.dumps([{'title': 'Shared company event',
        'published_at': '2026-11-05T18:00:00Z', 'source_id': 'user_import',
        'security_ids': ['US:XNAS:NVDA', 'US:XNAS:MSFT'], 'permissions': {}, 'price_reaction': {}}]), encoding='utf-8')
    import_file('news', path, 'US', tmp_path / 'runtime')
    events = news_from_frame(CuratedStore(tmp_path / 'runtime').load('news'))
    assert len(events) == 1 and events[0].security_ids == ['US:XNAS:NVDA', 'US:XNAS:MSFT']
    assert events[0].permissions == {}


def test_native_record_store_preserves_nested_unicode_and_nulls(tmp_path):
    store = CuratedStore(tmp_path)
    records = [{"title": "宏观政策", "security_ids": ["HK:XHKG:0700"],
                "permissions": {"sources": [{"name": "公告"}]}, "unavailable": None}]
    version = store.save("news", records)
    assert store.load("news", version) == records
    with gzip.open(store.metadata("news", version)["path"], "rt", encoding="utf-8") as handle:
        assert json.load(handle) == records


@pytest.mark.parametrize("records", [None, {"title": "one"}, [None], ["not a record"]])
def test_store_rejects_non_record_datasets(tmp_path, records):
    with pytest.raises(DataError):
        CuratedStore(tmp_path).save("news", records)


def test_store_rejects_path_traversal_names(tmp_path):
    with pytest.raises(DataError, match="invalid dataset"):
        CuratedStore(tmp_path).save("../outside", [])


def test_catalog_storage_migration_keeps_version_and_registration_time(tmp_path):
    store = CuratedStore(tmp_path)
    version = store.save("news", [{"title": "a"}])
    before = store.metadata("news", version)
    store.catalog.replace_dataset_path("news", version, Path(before["path"]))
    assert store.metadata("news", version) == before


def test_new_catalog_has_no_model_or_experiment_tables(tmp_path):
    import sqlite3
    catalog = Catalog(tmp_path)
    with sqlite3.connect(catalog.path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"datasets", "runs"}
