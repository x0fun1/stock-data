"""Verify the actual Git index cannot publish local data through forced staging."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "repository_boundary_check", ROOT / "scripts/check_repository.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
    )


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "README.md", b"Core research source\n")
    write(root, "scripts/research.py", b"print('core')\n")
    write(root, ".gitignore", b"runtime/\n*.safetensors\n")
    manifest = {
        "model_id": "kronos-small", "source_directory": "vendor/kronos",
        "source_files": [], "checkpoints": [],
    }
    write(root, "model-assets/kronos-small/manifest.json", json.dumps(manifest).encode())
    git(root, "add", "README.md", "scripts/research.py", "model-assets")
    return root


def test_core_source_passes_without_requiring_gitignore_tracking(repository, monkeypatch, capsys):
    checker = load_checker()
    monkeypatch.setattr(checker, "ROOT", repository)
    assert checker.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "passed"
    assert receipt["files"] == 3
    assert receipt["forbidden"] == []


@pytest.mark.parametrize("relative", [
    "runtime/model-comparison/predictions.json",
    "model-assets/kronos-small/small/model.safetensors",
    "vendor/kronos/unregistered_prediction.py",
    "model-comparison/compare_models.py",
    ".env.local",
])
def test_forced_local_data_in_index_fails(repository, relative, monkeypatch, capsys):
    write(repository, relative, b"local experiment payload\n")
    git(repository, "add", "-f", "--", relative)
    checker = load_checker()
    monkeypatch.setattr(checker, "ROOT", repository)
    assert checker.main() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "failed"
    assert receipt["forbidden"] == [relative]


def test_size_summary_uses_indexed_bytes_not_working_tree(repository):
    checker = load_checker()
    expected = checker.check_repository(repository)["bytes"]
    write(repository, "README.md", b"unstaged local edit\n" * 100)
    assert checker.check_repository(repository)["bytes"] == expected


def test_unstaged_manifest_cannot_allow_unregistered_indexed_output(repository):
    relative = 'vendor/kronos/new_prediction.json'
    write(repository, relative, b'local generated output\n')
    git(repository, 'add', '-f', '--', relative)
    manifest_path = repository / 'model-assets/kronos-small/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['source_files'].append({'path': 'new_prediction.json'})
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    receipt = load_checker().check_repository(repository)
    assert receipt['status'] == 'failed'
    assert receipt['forbidden'] == [relative]


def test_indexed_symlink_cannot_be_hidden_by_regular_working_tree_file(repository):
    oid = subprocess.run(
        ['git', '-C', str(repository), 'hash-object', '-w', '--stdin'],
        input=b'local-generated-data', check=True, capture_output=True,
    ).stdout.decode().strip()
    git(repository, 'update-index', '--cacheinfo', f'120000,{oid},README.md')
    assert (repository / 'README.md').is_file()
    receipt = load_checker().check_repository(repository)
    assert receipt['status'] == 'failed'
    assert receipt['forbidden'] == ['README.md']
