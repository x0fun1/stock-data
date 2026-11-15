"""The one-off converter preserves version identity without runtime pandas dependencies."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from stock_research.storage import Catalog, CuratedStore


def test_legacy_migration_preserves_all_versions_and_originals(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/migrate_runtime.py'
    spec = importlib.util.spec_from_file_location('legacy_converter', script)
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    root = tmp_path / 'runtime'
    catalog = Catalog(root)
    rows = [{'title': '消息', 'security_ids': '["HK:XHKG:0700"]', 'permissions': '{}'}]
    for version in ('v1', 'v2'):
        path = root / 'curated/news' / f'{version}.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows), encoding='utf-8')
        catalog.register_dataset('news', version, path, len(rows))
    dates = {entry['version']: entry['created_at'] for entry in catalog.list_datasets('news')}
    # Simulate the legacy reader; the ordinary test suite needs no pandas/Arrow.
    monkeypatch.setitem(sys.modules, 'pandas', SimpleNamespace(read_parquet=lambda path:
        SimpleNamespace(to_json=lambda **kwargs: path.read_text(encoding='utf-8'))))
    result = converter.migrate_runtime(root)
    assert result['versions'] == 2 and not result['originals_removed']
    store = CuratedStore(root)
    for entry in catalog.list_datasets('news'):
        assert entry['created_at'] == dates[entry['version']]
        assert store.load('news', entry['version']) == [{'title': '消息', 'security_ids': ['HK:XHKG:0700'], 'permissions': {}}]
        assert (root / 'curated/news' / (entry['version'] + '.parquet')).is_file()
    assert converter.migrate_runtime(root)['versions'] == 0
