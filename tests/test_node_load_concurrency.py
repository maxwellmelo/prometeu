"""Regression test: model download must not freeze the heartbeat.

Root cause of a real outage: load_model held the module-level _lock for the
entire GGUF download + cold start. The heartbeat loop builds its payload via
list_models(), which also takes _lock — so during a multi-minute download the
heartbeat blocked, the node's registry TTL expired, and it silently dropped out
of the pool. The fix: network/IO (download, spawn, health-wait) must run WITHOUT
holding _lock; the lock only guards in-memory state mutations.
"""
import threading
import time

from node.prometeu_node import inference


def test_list_models_not_blocked_during_download(monkeypatch, tmp_path):
    download_started = threading.Event()
    release_download = threading.Event()

    def slow_download(url, sha256, dest_dir=None):
        download_started.set()
        # Hold here as if downloading a large GGUF on a slow link.
        assert release_download.wait(timeout=5), "download not released"
        p = tmp_path / f"{sha256}.gguf"
        p.write_bytes(b"x")
        return p

    def fake_spawn(model, cpu_quota=50, mem_mb=1024):
        model.ready = True
        return model

    monkeypatch.setattr(inference, "download_gguf", slow_download)
    monkeypatch.setattr(inference, "_spawn_llama_server", fake_spawn)
    monkeypatch.setattr(inference, "health_check", lambda port: True)
    monkeypatch.setattr(inference, "_pick_port", lambda state: 18080)
    monkeypatch.setattr(inference, "_load_state", lambda: {})
    monkeypatch.setattr(inference, "_save_state", lambda state: None)

    result = {}

    def do_load():
        result["load"] = inference.load_model(
            model_id="m", gguf_url="http://x/g.gguf", sha256="abc", ctx_size=2048
        )

    t = threading.Thread(target=do_load, daemon=True)
    t.start()

    assert download_started.wait(timeout=5), "load never started downloading"

    # While the download is in-flight, list_models() MUST return promptly.
    # If load_model holds _lock across the download, this call hangs.
    got = {}

    def do_list():
        got["models"] = inference.list_models()

    lt = threading.Thread(target=do_list, daemon=True)
    lt.start()
    lt.join(timeout=2)

    assert not lt.is_alive(), "list_models blocked while a download was in flight"

    release_download.set()
    t.join(timeout=5)
    assert result.get("load", {}).get("ok") is True
