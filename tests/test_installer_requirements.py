"""Installer compatibility fails before touching a destination or network."""
import importlib.util
from pathlib import Path

import pytest


def test_unsupported_python_fails_before_copy_or_network(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/install_skill.py"
    spec = importlib.util.spec_from_file_location("installer_requirements_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "must_remain_absent"
    monkeypatch.setattr(module.sys, "version_info", (3, 11, 9))
    with pytest.raises(SystemExit, match="Python 3.12 or newer"):
        module.main(["--destination", str(destination)])
    assert not destination.exists()
