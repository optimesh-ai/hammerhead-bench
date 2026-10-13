"""Preflight's licence check: presence only, never the token."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "preflight_under_test", REPO_ROOT / "scripts" / "preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve the defining module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preflight(monkeypatch, tmp_path):
    monkeypatch.delenv("HAMMERHEAD_LICENSE", raising=False)
    monkeypatch.delenv("HAMMERHEAD_LICENSE_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return _load_preflight()


def test_no_licence_warns(preflight):
    result = preflight.check_hammerhead_license()
    assert result.status == "warn"
    assert "optimesh.ai/#trial" in result.detail


def test_env_token_passes_without_echoing_it(preflight, monkeypatch):
    monkeypatch.setenv("HAMMERHEAD_LICENSE", "HH1.secret-token-value")
    result = preflight.check_hammerhead_license()
    assert result.status == "pass"
    assert "secret-token-value" not in result.detail


def test_licence_file_env(preflight, monkeypatch, tmp_path):
    lic = tmp_path / "lic"
    lic.write_text("x")
    monkeypatch.setenv("HAMMERHEAD_LICENSE_FILE", str(lic))
    assert preflight.check_hammerhead_license().status == "pass"
    monkeypatch.setenv("HAMMERHEAD_LICENSE_FILE", str(tmp_path / "missing"))
    assert preflight.check_hammerhead_license().status == "warn"


def test_default_location(preflight, tmp_path):
    default = tmp_path / ".config" / "hammerhead" / "license"
    default.parent.mkdir(parents=True)
    default.write_text("x")
    assert preflight.check_hammerhead_license().status == "pass"
