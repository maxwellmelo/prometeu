"""Static policy tests for isolated public node install validator."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate-public-node-install.sh"


def _script() -> str:
    return SCRIPT.read_text()


def test_validator_uses_pct_exec_not_host_install():
    script = _script()
    assert "pct exec" in script
    assert "bash -s --" in script
    assert "raw.githubusercontent.com/maxwellmelo/prometeu/main/node/install.sh" in script


def test_validator_removes_old_node_state_inside_ct_only():
    script = _script()
    assert "/opt/prometeu-node" in script
    assert "/etc/prometeu-node" in script
    assert "/var/lib/prometeu-node" in script
    assert "pct exec \"$CTID\" -- bash -lc" in script


def test_validator_checks_hard_readiness_contracts():
    script = _script()
    assert "systemctl is-active --quiet prometeu-node" in script
    assert "/api/node/preflight" in script
    assert "can_serve" in script
    assert "resource_limits" in script
    assert "applied" in script
