"""Policy tests for the public node installer.

Prometeu's no-fallback rule means the installer must not claim an inference-capable
node when mandatory serving prerequisites are missing. These tests are static on
purpose: they catch accidental reintroduction of shell patterns that mask failures.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "node" / "install.sh"


def _script() -> str:
    return INSTALL.read_text()


def test_installer_does_not_continue_without_llama_binaries():
    script = _script()
    assert "continuing without llama binaries" not in script
    assert "install_llama_binaries ||" not in script


def test_installer_does_not_mask_resource_limit_failures():
    script = _script()
    assert "prometeu-node-apply-limits || true" not in script


def test_installer_documents_hard_preflight_checks():
    script = _script()
    assert "/api/node/preflight" in script
    assert "resource_limits.applied" in script
