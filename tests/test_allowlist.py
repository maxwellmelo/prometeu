"""Tests for the curated model allowlist (Fase 6 hardening)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

import allowlist as A


def _write_list(tmp_path, models):
    p = tmp_path / "al.json"
    p.write_text(json.dumps({"schema": "prometeu/allowlist/1", "models": models}))
    return str(p)


def test_off_list_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETEU_ALLOWLIST", _write_list(tmp_path, []))
    d = A.check_model("evil/backdoor.gguf", "hf", None)
    assert d["allowed"] is False
    assert "allowlist" in d["reason"]


def test_pinned_matches(tmp_path, monkeypatch):
    sha = "a" * 64
    monkeypatch.setenv("PROMETEU_ALLOWLIST",
                       _write_list(tmp_path, [{"model_id": "ok/m.gguf", "source": "hf", "sha256": sha}]))
    d = A.check_model("ok/m.gguf", "hf", sha)
    assert d["allowed"] is True
    assert d["pinned"] is True
    assert d["sha256"] == sha


def test_pinned_mismatch_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETEU_ALLOWLIST",
                       _write_list(tmp_path, [{"model_id": "ok/m.gguf", "source": "hf", "sha256": "a" * 64}]))
    d = A.check_model("ok/m.gguf", "hf", "b" * 64)
    assert d["allowed"] is False
    assert "mismatch" in d["reason"]


def test_pinned_authoritative_when_no_supplied(tmp_path, monkeypatch):
    sha = "c" * 64
    monkeypatch.setenv("PROMETEU_ALLOWLIST",
                       _write_list(tmp_path, [{"model_id": "ok/m.gguf", "source": "hf", "sha256": sha}]))
    d = A.check_model("ok/m.gguf", "hf", None)
    assert d["allowed"] is True
    assert d["sha256"] == sha  # pinned value wins, peer will verify against it


def test_unpinned_requires_supplied_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETEU_ALLOWLIST",
                       _write_list(tmp_path, [{"model_id": "new/m.gguf", "source": "hf", "sha256": ""}]))
    denied = A.check_model("new/m.gguf", "hf", None)
    assert denied["allowed"] is False
    ok = A.check_model("new/m.gguf", "hf", "d" * 64)
    assert ok["allowed"] is True
    assert ok["pinned"] is False
    assert ok["sha256"] == "d" * 64


def test_source_mismatch_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETEU_ALLOWLIST",
                       _write_list(tmp_path, [{"model_id": "ok/m.gguf", "source": "hf", "sha256": "a" * 64}]))
    d = A.check_model("ok/m.gguf", "url", "a" * 64)
    assert d["allowed"] is False


def test_real_shipped_allowlist_loads():
    # The shipped allowlist.json must be valid and non-empty.
    import importlib
    importlib.reload(A)
    data = A.load_allowlist()
    assert data.get("models"), "shipped allowlist should list models"
