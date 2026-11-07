"""Pinned text survives Git checkout conversions without accepting changed code."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from stock_research.common.hashing import sha256_bytes, verify_pinned_file
from stock_research.models import kronos


ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    spec = importlib.util.spec_from_file_location("text_portability_installer", ROOT / "scripts/install_skill.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer


@pytest.mark.parametrize("expected,incoming", [
    (b"value = 1\n\n", b"value = 1\r\n\r\n"),
    (b"value = 1\r\n\r\n", b"value = 1\n\n"),
])
def test_checkout_line_endings_restore_exact_pinned_bytes(tmp_path, expected, incoming):
    path = tmp_path / "model.py"
    path.write_bytes(incoming)
    assert not verify_pinned_file(path, sha256_bytes(expected), len(expected))
    assert path.read_bytes() == incoming
    assert verify_pinned_file(path, sha256_bytes(expected), len(expected), restore_text_newlines=True)
    assert path.read_bytes() == expected
    assert not list(tmp_path.glob(".pinned-text-*"))


def test_matching_mixed_line_endings_and_declared_size_are_preserved(tmp_path):
    path = tmp_path / "model.py"
    expected = b"first\r\nsecond\n\n"
    path.write_bytes(expected)
    modified_at = path.stat().st_mtime_ns
    assert verify_pinned_file(path, sha256_bytes(expected), len(expected), restore_text_newlines=True)
    assert path.read_bytes() == expected
    assert path.stat().st_mtime_ns == modified_at
    assert not verify_pinned_file(path, sha256_bytes(expected), len(expected) + 1, restore_text_newlines=True)
    assert path.read_bytes() == expected


def test_content_changes_and_final_blank_line_changes_are_not_repaired(tmp_path):
    path = tmp_path / "model.py"
    expected = b"value = 1\n\n"
    for incoming in (b"value = 2\r\n\r\n", b"value = 1\r\n", b"value = 1\r\n\r\n\r\n"):
        path.write_bytes(incoming)
        assert not verify_pinned_file(path, sha256_bytes(expected), len(expected), restore_text_newlines=True)
        assert path.read_bytes() == incoming


@pytest.mark.parametrize("filename", ["model.safetensors", "figure.png", "model.py"])
def test_binary_assets_are_never_normalized(tmp_path, filename):
    path = tmp_path / filename
    expected = b"binary\0\nasset" if filename.endswith(".py") else b"binary\nasset"
    incoming = expected.replace(b"\n", b"\r\n")
    path.write_bytes(incoming)
    assert not verify_pinned_file(path, sha256_bytes(expected), len(expected), restore_text_newlines=True)
    assert path.read_bytes() == incoming


def source_fixture(tmp_path):
    source = tmp_path / "source"
    relative_files = (
        "LICENSE", "model/kronos.py", "model/module.py", "finetune_csv/finetune_base_model.py",
        "finetune_csv/finetune_tokenizer.py", "finetune/train_predictor.py",
    )
    entries = []
    for relative in relative_files:
        expected = f"# synthetic pinned source: {relative}\n\n".encode()
        path = source / "vendor/kronos" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected.replace(b"\n", b"\r\n"))
        entries.append({"path": relative, "sha256": sha256_bytes(expected), "bytes": len(expected)})
    tree = "".join(entry["path"] + "|" + entry["sha256"] + "\n" for entry in entries).encode()
    manifest = {"source_directory": "vendor/kronos", "source_files": entries,
                "source_tree_sha256": hashlib.sha256(tree).hexdigest()}
    return source, manifest


def test_installer_source_verification_repairs_checkout_bytes_and_rejects_code_changes(tmp_path):
    installer = load_installer()
    source, manifest = source_fixture(tmp_path)
    installer.verify_source(source, manifest)
    for entry in manifest["source_files"]:
        path = source / "vendor/kronos" / entry["path"]
        assert sha256_bytes(path.read_bytes()) == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]
    altered = source / "vendor/kronos/model/kronos.py"
    changed = altered.read_bytes() + b"# changed implementation\n"
    altered.write_bytes(changed)
    with pytest.raises(ValueError, match="official source hash mismatch"):
        installer.verify_source(source, manifest)
    assert altered.read_bytes() == changed


def test_installer_repairs_json_configs_but_not_binary_checkpoints(tmp_path):
    installer = load_installer()
    assets = tmp_path / "assets"
    (assets / "small").mkdir(parents=True)
    expected_json = b'{\n  "width": 8\n}\n'
    config = assets / "small/config.json"
    config.write_bytes(expected_json.replace(b"\n", b"\r\n"))
    binary = assets / "small/model.safetensors"
    expected_binary = b"binary\nfixture"
    binary.write_bytes(expected_binary)
    entries = [{"path": "small/config.json", "sha256": sha256_bytes(expected_json), "bytes": len(expected_json)},
               {"path": "small/model.safetensors", "sha256": sha256_bytes(expected_binary), "bytes": len(expected_binary)}]
    manifest = {"checkpoints": [{"files": entries}]}
    assert installer.ensure_checkpoints(assets, manifest, offline=True) == {
        "small/config.json": "bundled_verified", "small/model.safetensors": "bundled_verified"}
    assert config.read_bytes() == expected_json
    changed_binary = expected_binary.replace(b"\n", b"\r\n")
    binary.write_bytes(changed_binary)
    with pytest.raises(ValueError, match="existing checkpoint is corrupt"):
        installer.ensure_checkpoints(assets, manifest, offline=True)
    assert binary.read_bytes() == changed_binary


@pytest.fixture
def runtime_assets(tmp_path, monkeypatch):
    package = tmp_path / "package"
    models = package / "model-assets/kronos-small"
    source = package / "vendor/kronos/model"
    source.mkdir(parents=True)
    manifest = json.loads((ROOT / "model-assets/kronos-small/manifest.json").read_text(encoding="utf-8"))
    pinned = dict(kronos._PINNED)
    for checkpoint in manifest["checkpoints"]:
        for entry in checkpoint["files"]:
            relative = entry["path"]
            path = models / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith(".json"):
                payload = (ROOT / "model-assets/kronos-small" / relative).read_bytes().replace(b"\r\n", b"\n")
                path.write_bytes(payload.replace(b"\n", b"\r\n"))
            else:
                payload = b"synthetic untrained binary weights: " + relative.encode()
                path.write_bytes(payload)
                pinned[relative] = sha256_bytes(payload)
                entry["sha256"] = pinned[relative]
                entry["bytes"] = len(payload)
    for filename in kronos._PINNED_SOURCE:
        payload = (ROOT / "vendor/kronos/model" / filename).read_bytes().replace(b"\r\n", b"\n")
        (source / filename).write_bytes(payload.replace(b"\n", b"\r\n"))
    (models / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(kronos, "_PINNED", pinned)
    return package


def test_direct_runtime_audit_restores_pinned_source_and_json_before_loading(runtime_assets):
    models, source, audit = kronos._audit_assets(runtime_assets)
    assert audit["checkpoint_sha256"] == kronos._PINNED
    assert audit["vendor_source_sha256"] == kronos._PINNED_SOURCE
    for key, expected in kronos._PINNED.items():
        assert sha256_bytes((models / key).read_bytes()) == expected
    for filename, expected in kronos._PINNED_SOURCE.items():
        assert sha256_bytes((source / filename).read_bytes()) == expected


def test_direct_runtime_audit_rejects_tampered_model_source(runtime_assets):
    source = runtime_assets / "vendor/kronos/model/kronos.py"
    changed = source.read_bytes() + b"# changed implementation\n"
    source.write_bytes(changed)
    with pytest.raises(ValueError, match="vendor_source_hash_mismatch:kronos.py"):
        kronos._audit_assets(runtime_assets)
    assert source.read_bytes() == changed
