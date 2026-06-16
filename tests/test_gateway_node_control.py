"""Gateway -> node-agent control contract tests."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))

import app as G  # noqa: E402


def test_node_inference_endpoint_prefers_dashboard_url(monkeypatch):
    async def fake_nodes():
        return [{
            "node_id": "node-a",
            "dashboard_url": "http://10.10.10.50:8787/",
            "hardware": {"host": "10.10.10.99"},
        }]

    monkeypatch.setattr(G, "_list_registry_nodes", fake_nodes)

    endpoint = asyncio.run(G._node_inference_endpoint("node-a"))

    assert endpoint == "http://10.10.10.50:8787"


def test_node_inference_endpoint_falls_back_to_host(monkeypatch):
    async def fake_nodes():
        return [{"node_id": "node-a", "hardware": {"host": "10.10.10.51"}}]

    monkeypatch.setattr(G, "_list_registry_nodes", fake_nodes)

    endpoint = asyncio.run(G._node_inference_endpoint("node-a"))

    assert endpoint == "http://10.10.10.51:8787"


def test_node_inference_endpoint_returns_none_for_unknown_node(monkeypatch):
    async def fake_nodes():
        return [{"node_id": "node-a", "dashboard_url": "http://10.10.10.50:8787"}]

    monkeypatch.setattr(G, "_list_registry_nodes", fake_nodes)

    endpoint = asyncio.run(G._node_inference_endpoint("missing"))

    assert endpoint is None


def test_instruct_peer_load_accepts_async_202(monkeypatch):
    calls = []

    async def fake_endpoint(node_id: str):
        assert node_id == "node-a"
        return "http://10.10.10.50:8787"

    class FakeResponse:
        status_code = 202

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 10.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(G, "_node_inference_endpoint", fake_endpoint)
    monkeypatch.setattr(G.httpx, "AsyncClient", FakeClient)

    pool = G.poolmod.Pool(
        pool_id="hf-qwen",
        model_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        source="hf",
        context=2048,
        min_peers=1,
        gguf_url="https://example.invalid/model.gguf",
        sha256="a" * 64,
    )

    accepted = asyncio.run(G._instruct_peer_load("node-a", pool))

    assert accepted is True
    assert calls == [(
        "http://10.10.10.50:8787/api/node/load",
        {
            "model_id": pool.model_id,
            "gguf_url": pool.gguf_url,
            "sha256": pool.sha256,
            "ctx_size": 2048,
        },
    )]


def test_instruct_peer_load_refuses_without_sha256(monkeypatch):
    async def fail_endpoint(node_id: str):  # pragma: no cover - must not be called
        raise AssertionError("endpoint lookup should not happen without sha256")

    monkeypatch.setattr(G, "_node_inference_endpoint", fail_endpoint)

    pool = G.poolmod.Pool(
        pool_id="hf-qwen",
        model_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        source="hf",
        context=2048,
        min_peers=1,
        gguf_url="https://example.invalid/model.gguf",
        sha256=None,
    )

    accepted = asyncio.run(G._instruct_peer_load("node-a", pool))

    assert accepted is False
