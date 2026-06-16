"""Tests for gateway.sizer — no real GGUF downloads, all HTTP mocked."""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the repo root importable when pytest is launched from anywhere.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import sizer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — build a minimal fake GGUF header in memory
# ---------------------------------------------------------------------------


def _gguf_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _kv_u32(key: str, value: int) -> bytes:
    return _gguf_string(key) + struct.pack("<I", sizer._GGUF_TYPE_UINT32) + struct.pack("<I", value)


def _kv_str(key: str, value: str) -> bytes:
    return _gguf_string(key) + struct.pack("<I", sizer._GGUF_TYPE_STRING) + _gguf_string(value)


def _build_fake_gguf(
    arch: str = "qwen2",
    n_layers: int = 28,
    n_heads: int = 12,
    n_kv_heads: int = 2,
    n_embd: int = 1536,
    ctx_train: int = 32768,
    vocab_size: int = 151936,
    head_dim: int = 128,
) -> bytes:
    kvs = [
        _kv_str("general.architecture", arch),
        _kv_str("general.file_type", "Q4_K_M"),
        _kv_u32(f"{arch}.block_count", n_layers),
        _kv_u32(f"{arch}.attention.head_count", n_heads),
        _kv_u32(f"{arch}.attention.head_count_kv", n_kv_heads),
        _kv_u32(f"{arch}.embedding_length", n_embd),
        _kv_u32(f"{arch}.context_length", ctx_train),
        _kv_u32(f"{arch}.vocab_size", vocab_size),
        _kv_u32(f"{arch}.attention.key_length", head_dim),
    ]
    header = b"GGUF"
    header += struct.pack("<I", 3)        # version
    header += struct.pack("<Q", 0)        # tensor_count
    header += struct.pack("<Q", len(kvs)) # kv_count
    for kv in kvs:
        header += kv
    # Pad to 16KB so the range-read can fill its buffer naturally.
    return header + b"\x00" * (16 * 1024 - len(header))


class _FakeResponse:
    def __init__(self, data: bytes, total: int):
        self._data = data
        self.headers = {
            "Content-Range": f"bytes 0-{len(data) - 1}/{total}",
            "Content-Length": str(len(data)),
        }

    def read(self, n: int = -1) -> bytes:
        if n < 0 or n >= len(self._data):
            return self._data
        return self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# read_gguf_metadata
# ---------------------------------------------------------------------------


def test_read_gguf_metadata_parses_header():
    fake = _build_fake_gguf()
    total_bytes = 1_200_000_000  # pretend the real file is ~1.2 GB

    def fake_urlopen(req, timeout=15.0):  # noqa: ARG001
        return _FakeResponse(fake, total_bytes)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        meta = sizer.read_gguf_metadata("https://example/qwen.gguf")

    assert meta["architecture"] == "qwen2"
    assert meta["n_layers"] == 28
    assert meta["n_heads"] == 12
    assert meta["n_kv_heads"] == 2
    assert meta["head_dim"] == 128
    assert meta["n_embd"] == 1536
    assert meta["ctx_train"] == 32768
    assert meta["vocab_size"] == 151936
    assert meta["file_size_bytes"] == total_bytes
    assert "kv" in meta


def test_read_gguf_metadata_rejects_bad_magic():
    bad = b"NOPE" + b"\x00" * 1024

    def fake_urlopen(req, timeout=15.0):  # noqa: ARG001
        return _FakeResponse(bad, len(bad))

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ValueError, match="Not a GGUF file"):
            sizer.read_gguf_metadata("https://example/bad.gguf")


# ---------------------------------------------------------------------------
# estimate_ram
# ---------------------------------------------------------------------------


def test_estimate_ram_qwen_15b_kv_cache_in_expected_band():
    """Qwen2 1.5B: 28 layers, 2 KV heads, head_dim 128, ctx 2048.

    Expected KV cache: 2 * 28 * 2 * 128 * 2048 * 2 bytes = 56 MiB.
    """
    meta = {
        "file_size_bytes": 1_000 * 1024 * 1024,  # 1000 MB weights
        "n_layers": 28,
        "n_heads": 12,
        "n_kv_heads": 2,
        "head_dim": 128,
        "quantization": "Q4_K_M",
    }
    ram = sizer.estimate_ram(meta, context=2048)

    # KV cache: 2*28*2*128*2048*2 = 58_720_256 bytes  →  56 MB
    assert ram["kv_cache_mb"] == 56
    assert ram["weights_mb"] == 1000
    # Overhead is 15% of (weights + kv) → ceil((1000+56)*0.15) = 159 MB
    assert 158 <= ram["overhead_mb"] <= 160
    assert ram["total_ram_mb"] == ram["weights_mb"] + ram["kv_cache_mb"] + ram["overhead_mb"]
    assert ram["context"] == 2048


def test_estimate_ram_scales_with_context():
    meta = {
        "file_size_bytes": 500 * 1024 * 1024,
        "n_layers": 28,
        "n_heads": 12,
        "n_kv_heads": 2,
        "head_dim": 128,
        "quantization": "Q4_K_M",
    }
    r_short = sizer.estimate_ram(meta, context=2048)
    r_long = sizer.estimate_ram(meta, context=8192)
    # 4x context → 4x KV cache
    assert r_long["kv_cache_mb"] == pytest.approx(r_short["kv_cache_mb"] * 4, rel=0.02)


# ---------------------------------------------------------------------------
# plan_pool
# ---------------------------------------------------------------------------


def test_plan_pool_picks_minimum_peers_2gb_each():
    # Need 5 GB total, peers have 2 GB each → need 3 peers.
    meta = {"n_layers": 28}
    pool = sizer.plan_pool(meta, total_ram_mb=5000, peers_available_mb=[2048, 2048, 2048, 2048])
    assert pool["min_peers"] == 3
    assert pool["feasible"] is True
    assert pool["ram_per_peer_mb"] == 1667  # ceil(5000/3)
    assert len(pool["selected_peers"]) == 3
    # All peers tied at 2048; first three chosen indices sorted ascending.
    assert pool["selected_peers"] == [0, 1, 2]
    # base 12 / sqrt(3) ≈ 6.93, no penalty (2048 ≥ 1667)
    assert 6.5 < pool["tok_s_estimate"] < 7.5


def test_plan_pool_applies_slow_peer_penalty():
    meta = {"n_layers": 28}
    # 4 GB needed; peers 2 GB + 2 GB + 1.5 GB. Top-2 sum = 4 GB, ram_per_peer = 2 GB.
    # The third peer would not be selected, so no penalty here. Construct
    # a case where a chosen peer is below the uniform share:
    pool = sizer.plan_pool(meta, total_ram_mb=5000, peers_available_mb=[3000, 2500])
    # 3000+2500 = 5500 ≥ 5000 → both peers selected, share = 2500.
    # Peer with 2500 == share (not strictly less). No penalty expected.
    assert pool["feasible"] is True
    assert pool["min_peers"] == 2

    # Now force one peer below the uniform share:
    pool2 = sizer.plan_pool(meta, total_ram_mb=5000, peers_available_mb=[4000, 1500])
    # 4000+1500=5500, share=ceil(5000/2)=2500. The 1500 peer < 2500 → penalty.
    assert pool2["feasible"] is True
    # base 12 / sqrt(2) ≈ 8.485 → ×0.7 ≈ 5.94
    assert 5.5 < pool2["tok_s_estimate"] < 6.3


def test_plan_pool_infeasible_when_total_short():
    meta = {"n_layers": 28}
    pool = sizer.plan_pool(meta, total_ram_mb=10_000, peers_available_mb=[1024, 1024])
    assert pool["feasible"] is False
    assert pool["tok_s_estimate"] == 0.0


# ---------------------------------------------------------------------------
# size_model end-to-end (with mocked HTTP)
# ---------------------------------------------------------------------------


def test_size_model_hf_orchestrates_full_report():
    fake = _build_fake_gguf()
    total_bytes = 900 * 1024 * 1024  # 900 MB file

    def fake_urlopen(req, timeout=15.0):  # noqa: ARG001
        return _FakeResponse(fake, total_bytes)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        report = sizer.size_model(
            "Qwen/Qwen2-1_5B-Instruct-GGUF/qwen2-1_5b-instruct-q4_k_m.gguf",
            source="hf",
            context=2048,
            peers_available_mb=[2048, 2048, 2048, 2048],
        )

    assert report["source"] == "hf"
    assert report["url"].startswith("https://huggingface.co/Qwen/Qwen2-1_5B-Instruct-GGUF/resolve/main/")
    assert report["metadata"]["n_layers"] == 28
    assert report["ram"]["weights_mb"] == 900
    assert report["pool"]["min_peers"] >= 1
    assert report["pool"]["feasible"] is True


def test_size_model_ollama_not_implemented():
    with pytest.raises(NotImplementedError):
        sizer.size_model("llama3.2:1b", source="ollama")


def test_size_model_rejects_bad_hf_id():
    with pytest.raises(ValueError, match="HF model_id"):
        sizer.size_model("just-a-repo", source="hf")
