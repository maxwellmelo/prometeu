"""Contract tests for the node preflight readiness signal.

The public install validator and the gateway treat ``can_serve`` as the single
canonical readiness flag. ``sandbox_preflight`` must compute it from the
mandatory prerequisites (no-fallback rule): if any required check is false the
node must report ``can_serve=False`` and refuse to serve inference.
"""
from node.prometeu_node import inference


def test_sandbox_preflight_exposes_can_serve(monkeypatch):
    monkeypatch.setattr(inference, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(inference.Path, "exists", lambda self: True)
    monkeypatch.setattr(inference, "_systemd_available", lambda: True)
    monkeypatch.setattr(inference, "_sandbox_user_exists", lambda: True)
    monkeypatch.setattr(inference.os, "access", lambda *a, **k: True)

    result = inference.sandbox_preflight()

    assert result["can_serve"] is True


def test_sandbox_preflight_can_serve_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(inference, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(inference.Path, "exists", lambda self: False)
    monkeypatch.setattr(inference, "_systemd_available", lambda: True)
    monkeypatch.setattr(inference, "_sandbox_user_exists", lambda: True)
    monkeypatch.setattr(inference.os, "access", lambda *a, **k: True)

    result = inference.sandbox_preflight()

    assert result["can_serve"] is False


def test_sandbox_preflight_can_serve_false_when_no_systemd(monkeypatch):
    monkeypatch.setattr(inference, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(inference.Path, "exists", lambda self: True)
    monkeypatch.setattr(inference, "_systemd_available", lambda: False)
    monkeypatch.setattr(inference, "_sandbox_user_exists", lambda: True)
    monkeypatch.setattr(inference.os, "access", lambda *a, **k: True)

    result = inference.sandbox_preflight()

    assert result["can_serve"] is False


def test_sandbox_preflight_can_serve_false_when_models_dir_readonly(monkeypatch):
    monkeypatch.setattr(inference, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(inference.Path, "exists", lambda self: True)
    monkeypatch.setattr(inference, "_systemd_available", lambda: True)
    monkeypatch.setattr(inference, "_sandbox_user_exists", lambda: True)
    monkeypatch.setattr(inference.os, "access", lambda *a, **k: False)

    result = inference.sandbox_preflight()

    assert result["can_serve"] is False
