"""The repository root is an installable skill, without local planning/data files."""
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

from stock_research.config import default_runtime_root, default_skill_root, load_policies
from stock_research.common.package_files import package_files


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    'SKILL.md', 'README.md', 'requirements.txt', 'scripts/install_skill.py', 'scripts/skill.py',
    'scripts/research.py', 'references/news-evidence-and-grades.md', 'policies/defaults.json',
    'examples/news-template.json',
    'references/install-prompt.md', 'references/local-search-fetch.md',
)
LOCAL_ONLY_FILES = (
    'docs/api.txt', 'docs/planning/notes.md', 'runtime/private.json', '.env', '.env.local',
    '.gitignore', '.gitattributes', 'scratch/compare_models.py',
    'scripts/stock_research/__pycache__/discard.py', 'examples/local-notes.json',
    'vendor/kronos/model/kronos.py', 'model-assets/kronos-small/small/model.safetensors',
    'requirements-kronos.txt',
)


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        f"package_layout_{name}", ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def package_source(tmp_path):
    source = tmp_path / "source"
    for relative in PUBLIC_FILES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic public fixture: {relative}\n".encode())
    for relative in LOCAL_ONLY_FILES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic local-only fixture\n", encoding="utf-8")
    return source


def test_package_copy_keeps_only_declared_skill_files(package_source, tmp_path):
    installer = load_script("install_skill")
    destination = tmp_path / "installed"
    assert installer.copy_package(package_source, destination) == len(PUBLIC_FILES)
    assert all(not (destination / relative).exists() for relative in LOCAL_ONLY_FILES)
    assert not (destination / 'vendor').exists() and not (destination / 'model-assets').exists()
    for relative in PUBLIC_FILES:
        assert (destination / relative).read_bytes() == (package_source / relative).read_bytes()


def test_portable_zip_excludes_local_files(package_source, tmp_path, monkeypatch):
    builder = load_script("build_bundle")
    output = tmp_path / "portable.zip"
    monkeypatch.setattr(builder, "ROOT", package_source)
    monkeypatch.setattr(sys, "argv", ["build_bundle.py", "--out", str(output)])
    builder.main()

    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        manifest = json.loads(archive.read("stock-research/bundle-manifest.json"))
    assert names == {'stock-research/' + relative for relative in PUBLIC_FILES} | {'stock-research/bundle-manifest.json'}
    assert {entry['path'] for entry in manifest['files']} == set(PUBLIC_FILES)


def test_repository_package_contains_no_price_model_or_quantitative_code():
    selected = {path.relative_to(ROOT).as_posix() for path in package_files(ROOT)}
    assert {'SKILL.md', 'requirements.txt', 'scripts/research.py', 'policies/source-policy.json',
            'references/install-prompt.md', 'references/local-search-fetch.md',
            'scripts/stock_research/acquisition/local_service.py',
            'scripts/stock_research/acquisition/local_search_fetch.py'} <= selected
    assert not [name for name in selected if name.startswith(('vendor/', 'model-assets/'))]
    retired = ('kronos', 'multifactor', '/models/', '/features/', '/datasets/', '/validation/', '/corporate_actions/')
    assert not [name for name in selected if any(term in name for term in retired)]
    requirements = (ROOT / 'requirements.txt').read_text(encoding='utf-8').lower()
    assert not any(name in requirements for name in ('torch', 'lightgbm', 'scipy', 'safetensors'))


def test_bundle_rejects_output_inside_source(package_source, monkeypatch):
    builder = load_script("build_bundle")
    output = package_source / "runtime" / "portable.zip"
    monkeypatch.setattr(builder, "ROOT", package_source)
    monkeypatch.setattr(sys, "argv", ["build_bundle.py", "--out", str(output)])
    with pytest.raises(SystemExit, match="outside the source skill directory"):
        builder.main()
    assert not output.exists()


def test_installer_rejects_nested_destination_before_copy(package_source, monkeypatch):
    installer = load_script("install_skill")
    destination = package_source / "runtime" / "installed"
    monkeypatch.setattr(installer, "PACKAGE_ROOT", package_source)
    with pytest.raises(SystemExit, match="destination must be outside the source skill"):
        installer.main(["--destination", str(destination), "--skip-deps", "--skip-smoke"])
    assert not destination.exists()


def test_unsupported_python_fails_before_copy(tmp_path, monkeypatch):
    installer = load_script("install_skill")
    destination = tmp_path / "must_remain_absent"
    monkeypatch.setattr(installer.sys, "version_info", (3, 11, 9))
    with pytest.raises(SystemExit, match="Python 3.12 or newer"):
        installer.main(["--destination", str(destination)])
    assert not destination.exists()


def test_install_copies_the_real_skill_and_passes_the_news_self_test(tmp_path):
    installer = load_script("install_skill")
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        destination = Path(folder) / "installed"
        assert installer.main(["--destination", str(destination), "--skip-deps"]) == 0
        receipt = json.loads((destination / "install-receipt.json").read_text(encoding="utf-8"))
        assert receipt["smoke"]["status"] == "passed"
        assert receipt["smoke"]["result"]["classification"] == "利多"
        assert receipt['smoke']['result']['local_search_fetch']['html_extraction'] == 'passed'
        assert receipt['smoke']['result']['local_search_fetch']['default_search_workers'] == 32
        assert receipt['local_service']['fetch_workers'] == 32
        assert receipt['local_service']['started_by_installer'] is False
        assert not (destination / "runtime").exists() and not (destination / ".pip-cache").exists()


def test_default_runtime_stays_with_skill_when_sibling_runtime_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("STOCK_RESEARCH_RUNTIME", raising=False)
    skill = tmp_path / "stock-research"
    skill.mkdir()
    (tmp_path / "runtime").mkdir()
    assert default_runtime_root(skill) == skill / "runtime"
    assert not (skill / "runtime").exists()


def test_runtime_environment_override_is_preserved(tmp_path, monkeypatch):
    override = tmp_path / "selected-data"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME", str(override))
    assert default_runtime_root(tmp_path / "stock-research") == override


def test_flattened_root_reads_policies():
    installer = load_script("install_skill")
    builder = load_script("build_bundle")
    assert default_skill_root() == installer.PACKAGE_ROOT == builder.ROOT == ROOT
    assert load_policies().skill_root == ROOT
