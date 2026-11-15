from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from stock_research.common.jsonio import read_json, write_json
from stock_research.contracts import ResearchRequest
from stock_research.control.orchestration import run_research
from stock_research.storage.catalog import Catalog
from stock_research.storage.curated_store import CuratedStore
from stock_research.storage.retention import cleanup_runtime, runtime_session
from stock_research.storage import retention

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=7, seconds=1)
BOUNDARY = NOW - timedelta(days=7)


def dated(path, text='cached', when=OLD):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def test_strict_seven_day_boundary_and_dry_run_do_not_refresh_files(tmp_path):
    old = dated(tmp_path / 'cache/old.json')
    boundary = dated(tmp_path / 'cache/boundary.json', when=BOUNDARY)
    fresh = dated(tmp_path / 'cache/fresh.json', when=NOW)
    first = cleanup_runtime(tmp_path, NOW, dry_run=True)
    assert first['expired_files'] == 1 and first['deleted_files'] == 0
    assert old.exists() and old.stat().st_mtime == OLD.timestamp()
    result = cleanup_runtime(tmp_path, NOW)
    assert result['deleted_files'] == 1 and result['freed_bytes'] == len('cached')
    assert not old.exists() and boundary.exists() and fresh.exists()
    assert read_json(tmp_path / '.retention-last.json') == result
    assert cleanup_runtime(tmp_path, NOW)['deleted_files'] == 0


def test_report_pairs_raw_pairs_and_recent_workflows_stay_complete(tmp_path):
    dated(tmp_path / 'reports/run-old.json')
    dated(tmp_path / 'reports/run-old.md')
    kept_md = dated(tmp_path / 'reports/run-recent.md')
    kept_json = dated(tmp_path / 'reports/run-recent.json', when=NOW)
    dated(tmp_path / 'raw/finnhub/news/old.json')
    dated(tmp_path / 'raw/finnhub/news/old.json.manifest.json')
    kept_payload = dated(tmp_path / 'raw/finnhub/news/reobserved.json')
    dated(tmp_path / 'raw/finnhub/news/reobserved.json.manifest.json', when=NOW)
    workflow = tmp_path / 'news-workflows/current'
    tasks = dated(workflow / 'tasks.json', '[]')
    dated(workflow / 'collections/old/items/one.json')
    dated(workflow / 'collections/old/completion.json')
    recent = dated(workflow / 'collections/new/items/one.json', when=NOW)
    receipt = cleanup_runtime(tmp_path, NOW)
    assert receipt['deleted_files'] == 6
    assert kept_md.exists() and kept_json.exists() and kept_payload.exists()
    assert tasks.exists() and recent.exists()
    assert not (workflow / 'collections/old').exists()


def test_expired_workflow_and_pointer_are_removed_despite_recent_directory_mtime(tmp_path):
    folder = tmp_path / 'news-workflows/expired'
    dated(folder / 'tasks.json', '[]')
    dated(folder / 'collections/old/completion.json')
    os.utime(folder, (NOW.timestamp(), NOW.timestamp()))
    pointer = tmp_path / 'news-workflows/NVDA-latest.json'
    write_json(pointer, {'folder': str(folder), 'workflow_id': 'expired'})
    result = cleanup_runtime(tmp_path, NOW)
    assert result['deleted_files'] == 3
    assert not folder.exists() and not pointer.exists()


def test_catalog_drops_expired_snapshots_and_reports_without_touching_fresh_versions(tmp_path):
    store = CuratedStore(tmp_path)
    old = store.save('news', [{'title': 'Old'}])
    recent = store.save('news', [{'title': 'New'}])
    expired = Path(store.metadata('news', old)['path'])
    os.utime(expired, (OLD.timestamp(), OLD.timestamp()))
    report = dated(tmp_path / 'reports/run-old.json')
    store.catalog.register_run('run-old', report)
    preview = cleanup_runtime(tmp_path, NOW, dry_run=True)
    assert preview['catalog_entries_removed'] == 2
    assert store.catalog.get_run('run-old') is not None and expired.exists()
    receipt = cleanup_runtime(tmp_path, NOW)
    assert receipt['errors'] == [] and receipt['catalog_entries_removed'] == 2
    assert store.catalog.get_run('run-old') is None
    assert store.catalog.get_dataset('news', old) is None
    assert store.load('news', recent) == [{'title': 'New'}]


def test_symbols_and_original_input_files_outside_cache_are_untouched(tmp_path):
    outside = tmp_path / 'outside'
    original = dated(outside / 'original.json')
    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'raw').symlink_to(outside, target_is_directory=True)
    folder = root / 'news-workflows/linked'
    old = dated(folder / 'tasks.json', '[]')
    (folder / 'external.json').symlink_to(original)
    credentials = dated(root / '.env', 'synthetic secret')
    supplied = dated(root / 'input.json')
    assert cleanup_runtime(root, NOW)['deleted_files'] == 0
    assert original.exists() and old.exists() and credentials.exists() and supplied.exists()
    with pytest.raises(ValueError, match='symlink root'):
        cleanup_runtime(root / 'raw', NOW)


def test_nested_analysis_directories_are_checked_on_the_same_runtime_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, 'default_skill_root', lambda: tmp_path)
    root = tmp_path / 'runtime'
    dated(root / 'previous/reports/run.json')
    dated(root / 'previous/reports/run.md')
    dated(root / 'previous/acceptance/old-summary.json')
    keep = dated(root / 'current/benchmark-result.json', when=NOW)
    receipt = cleanup_runtime(root / 'current', NOW)
    assert receipt['scope'] == str(root)
    assert receipt['deleted_files'] == 3 and keep.exists()


def test_research_cleanup_uses_request_time_and_not_historical_report_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, 'now_utc', lambda: NOW)
    expired = dated(tmp_path / 'reports/run-expired.json')
    store = CuratedStore(tmp_path)
    version = store.save('news', [{'title': 'Older news freshly fetched', 'source_id': 'finnhub',
                                 'security_ids': ['US:XNAS:NVDA'], 'market': 'US',
                                 'published_at': '2026-11-01T12:00:00Z', 'evidence_scope': 'title'}])
    path = Path(store.metadata('news', version)['path'])
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    bundle, _ = run_research(ResearchRequest(market='US', tickers=['NVDA'], cutoff='2026-11-02T00:00:00Z'),
                             runtime_root=tmp_path)
    assert not expired.exists() and path.exists()
    assert bundle.run_manifest.artifacts['retention']['deleted_files'] == 1
    assert len(bundle.context_events) == 1


def test_nested_operations_clean_once_and_do_not_remove_files_created_during_request(tmp_path):
    with runtime_session(tmp_path, NOW) as first:
        created = dated(tmp_path / 'cache/imported-old-timestamp.json')
        with runtime_session(tmp_path, NOW + timedelta(days=8)) as second:
            assert second is first and created.exists()
    assert cleanup_runtime(tmp_path, NOW)['deleted_files'] == 1


@pytest.mark.skipif(os.name == 'nt', reason='fcntl test for the POSIX request lock')
def test_active_request_holds_cross_process_cleanup_lock(tmp_path):
    code = '''
import fcntl,sys
with open(sys.argv[1], 'a+b') as handle:
    try:fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:sys.exit(3)
'''
    with runtime_session(tmp_path, NOW):
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path / '.retention.lock')])
        assert result.returncode == 3
    assert subprocess.run([sys.executable, '-c', code, str(tmp_path / '.retention.lock')]).returncode == 0


def test_cli_preview_does_not_run_automatic_cleanup_first(tmp_path):
    expired = dated(tmp_path / 'cache/old.json', when=datetime.now(timezone.utc) - timedelta(days=8))
    script = Path(__file__).resolve().parents[1] / 'scripts/research.py'
    command = [sys.executable, str(script), 'cleanup', '--root', str(tmp_path)]
    preview = subprocess.run([*command, '--dry-run'], capture_output=True, text=True, check=True)
    assert json.loads(preview.stdout)['expired_files'] == 1 and expired.exists()
    deleted = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(deleted.stdout)['deleted_files'] == 1 and not expired.exists()


def test_source_directory_cannot_be_used_as_a_cleanup_root(tmp_path):
    (tmp_path / 'SKILL.md').write_text('skill source')
    with pytest.raises(ValueError, match='dedicated data directory'):
        cleanup_runtime(tmp_path, NOW)
