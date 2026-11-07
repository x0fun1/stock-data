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
LOCAL_ONLY_FILES = (
    'docs/api.txt', 'docs/planning/notes.md', 'runtime/private.json', '.env', '.env.local',
    '.gitignore', '.gitattributes', 'vendor/kronos/.gitignore', 'vendor/kronos/.gitattributes',
    'model-comparison/compare.py', 'hourly-model-test/predictions.json',
    'scratch/compare_models.py', 'scripts/model-comparison/compare.py',
    'scripts/stock_research/__pycache__/discard.py',
    'vendor/kronos/examples/new_prediction.json', 'vendor/kronos/new_comparison.py',
    'vendor/chronos/model.py', 'model-assets/kronos-small/local-training.pth',
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
    public_files = (
        "SKILL.md",
        "scripts/install_skill.py",
        "scripts/skill.py",
        "vendor/kronos/LICENSE",
        "model-assets/kronos-small/manifest.json",
        "model-assets/kronos-small/small/model.safetensors",
        "model-assets/kronos-small/tokenizer/model.safetensors",
        "references/kronos-training-plan.md",
        "references/multifactor-workflow.md",
        "scripts/stock_research/multifactor/study.py",
        "scripts/stock_research/multifactor/risk.py",
        "scripts/stock_research/control/multifactor.py",
    )
    for relative in public_files:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic public fixture: {relative}\n".encode())
    manifest = {
        'source_directory': 'vendor/kronos', 'source_files': [{'path': 'LICENSE'}],
        'checkpoints': [{'files': [{'path': f'{name}/model.safetensors'}]}
                        for name in ('small', 'tokenizer')],
    }
    (source / 'model-assets/kronos-small/manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    for relative in LOCAL_ONLY_FILES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic local-only fixture\n", encoding="utf-8")
    return source


def test_package_copy_excludes_docs_and_runtime_but_keeps_model_resources(package_source, tmp_path):
    installer = load_script("install_skill")
    destination = tmp_path / "installed"
    installer.copy_package(package_source, destination)

    assert not (destination / "docs").exists()
    assert not (destination / "runtime").exists()
    assert not (destination / ".env").exists()
    assert not list(destination.rglob(".gitignore"))
    assert not list(destination.rglob(".gitattributes"))
    assert all(not (destination / relative).exists() for relative in LOCAL_ONLY_FILES)
    for relative in (
        "SKILL.md", "vendor/kronos/LICENSE", "model-assets/kronos-small/manifest.json",
        "model-assets/kronos-small/small/model.safetensors",
        "references/multifactor-workflow.md",
    ):
        assert (destination / relative).read_bytes() == (package_source / relative).read_bytes()


def test_portable_zip_excludes_local_docs_and_keeps_relative_resources(package_source, tmp_path, monkeypatch):
    builder = load_script("build_bundle")
    output = tmp_path / "portable.zip"
    monkeypatch.setattr(builder, "ROOT", package_source)
    monkeypatch.setattr(sys, "argv", ["build_bundle.py", "--out", str(output)])
    builder.main()

    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert all(not name.startswith(("stock-research/docs/", "stock-research/runtime/")) for name in names)
        assert "stock-research/.env" not in names
        assert all(Path(name).name not in {".gitignore", ".gitattributes"} for name in names)
        assert all('stock-research/' + relative not in names for relative in LOCAL_ONLY_FILES)
        manifest = json.loads(archive.read("stock-research/bundle-manifest.json"))
        assert all(not entry["path"].startswith(("docs/", "runtime/")) for entry in manifest["files"])
        relative = "model-assets/kronos-small/tokenizer/model.safetensors"
        assert archive.read("stock-research/" + relative) == (package_source / relative).read_bytes()
        assert "stock-research/references/multifactor-workflow.md" in names


def test_package_selection_preserves_exact_pinned_upstream_and_checkpoints():
    manifest = json.loads((ROOT / 'model-assets/kronos-small/manifest.json').read_text(encoding='utf-8'))
    selected = {path.relative_to(ROOT).as_posix() for path in package_files(ROOT)}
    assert {name for name in selected if name.startswith('vendor/')} == {
        'vendor/kronos/' + entry['path'] for entry in manifest['source_files']
    }
    assert {name for name in selected if name.startswith('model-assets/')} == {
        'model-assets/kronos-small/manifest.json',
        *('model-assets/kronos-small/' + entry['path'] for checkpoint in manifest['checkpoints']
          for entry in checkpoint['files'] if (ROOT / 'model-assets/kronos-small' / entry['path']).is_file()),
    }


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


def test_flattened_root_reads_policies_and_pinned_source_manifest():
    installer = load_script("install_skill")
    builder = load_script("build_bundle")
    assert default_skill_root() == installer.PACKAGE_ROOT == builder.ROOT == ROOT
    assert load_policies().skill_root == ROOT
    assets, manifest = installer.read_manifest(ROOT)
    assert assets == ROOT / "model-assets" / "kronos-small"
    installer.verify_source(ROOT, manifest)
