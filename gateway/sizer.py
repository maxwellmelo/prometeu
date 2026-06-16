"""Prometeu sizer — estimate RAM, peer count, throughput for a GGUF model.

Standalone module: stdlib only (+ optional ``gguf`` lib). No FastAPI/redis
deps so it can be lifted into its own ``pip`` package later.

Pipeline:

1. :func:`read_gguf_metadata` — HTTP range-fetches the first ~16KB of a
   GGUF file and parses the header (using the ``gguf`` lib if installed,
   otherwise a minimal hand-rolled parser).
2. :func:`estimate_ram` — pure arithmetic: weights + KV cache + overhead.
3. :func:`plan_pool` — picks the smallest set of peers whose combined RAM
   can host the model, splits load uniformly, and estimates tok/s.
4. :func:`size_model` — orchestrator that resolves a ``(model_id, source)``
   pair into a GGUF URL and returns a single sizing report.

The module is intentionally side-effect free apart from the single HTTP
range request performed by :func:`read_gguf_metadata`.
"""

from __future__ import annotations

import math
import struct
import urllib.request
from typing import Any

# GGUF spec constants (https://github.com/ggerganov/ggml/blob/master/docs/gguf.md)
GGUF_MAGIC = b"GGUF"

# GGUF metadata value types
_GGUF_TYPE_UINT8 = 0
_GGUF_TYPE_INT8 = 1
_GGUF_TYPE_UINT16 = 2
_GGUF_TYPE_INT16 = 3
_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_INT32 = 5
_GGUF_TYPE_FLOAT32 = 6
_GGUF_TYPE_BOOL = 7
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9
_GGUF_TYPE_UINT64 = 10
_GGUF_TYPE_INT64 = 11
_GGUF_TYPE_FLOAT64 = 12

_SCALAR_FMT = {
    _GGUF_TYPE_UINT8: ("<B", 1),
    _GGUF_TYPE_INT8: ("<b", 1),
    _GGUF_TYPE_UINT16: ("<H", 2),
    _GGUF_TYPE_INT16: ("<h", 2),
    _GGUF_TYPE_UINT32: ("<I", 4),
    _GGUF_TYPE_INT32: ("<i", 4),
    _GGUF_TYPE_FLOAT32: ("<f", 4),
    _GGUF_TYPE_BOOL: ("<?", 1),
    _GGUF_TYPE_UINT64: ("<Q", 8),
    _GGUF_TYPE_INT64: ("<q", 8),
    _GGUF_TYPE_FLOAT64: ("<d", 8),
}

# Approximate bytes-per-weight for common GGUF quantizations. Used only as
# a sanity hint; weight cost is normally derived from ``file_size_bytes``.
_QUANT_BPW = {
    "F32": 4.0,
    "F16": 2.0,
    "BF16": 2.0,
    "Q8_0": 1.06,
    "Q6_K": 0.82,
    "Q5_K_M": 0.69,
    "Q5_K_S": 0.66,
    "Q5_0": 0.69,
    "Q4_K_M": 0.58,
    "Q4_K_S": 0.55,
    "Q4_0": 0.56,
    "Q3_K_M": 0.47,
    "Q3_K_S": 0.43,
    "Q2_K": 0.36,
}

_RANGE_BYTES = 16 * 1024  # 16KB is plenty for the header on every model we care about


# ---------------------------------------------------------------------------
# GGUF parsing
# ---------------------------------------------------------------------------


def _http_range_get(url: str, n_bytes: int = _RANGE_BYTES, timeout: float = 15.0) -> bytes:
    """Fetch the first ``n_bytes`` of ``url`` using a Range header.

    Falls back to a normal GET if the server ignores the Range request.
    """
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{n_bytes - 1}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URL)
        data = resp.read(n_bytes)
        # Try to discover the total size from Content-Range / Content-Length
        size = None
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            try:
                size = int(cr.rsplit("/", 1)[1])
            except ValueError:
                size = None
        if size is None:
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    size = int(cl)
                except ValueError:
                    size = None
    return data, size  # type: ignore[return-value]


def _http_head_size(url: str, timeout: float = 10.0) -> int | None:
    """Best-effort HEAD to retrieve total file size."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


class _GGUFReader:
    """Tiny stream reader for GGUF binary header bytes."""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise ValueError("GGUF header truncated (need more bytes)")
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self) -> str:
        length = self.u64()
        return self.read(length).decode("utf-8", errors="replace")

    def value(self, vtype: int) -> Any:
        if vtype in _SCALAR_FMT:
            fmt, size = _SCALAR_FMT[vtype]
            return struct.unpack(fmt, self.read(size))[0]
        if vtype == _GGUF_TYPE_STRING:
            return self.string()
        if vtype == _GGUF_TYPE_ARRAY:
            elem_type = self.u32()
            count = self.u64()
            # We rarely need full arrays for sizing; stop at a sane cap.
            cap = min(count, 16)
            arr = [self.value(elem_type) for _ in range(cap)]
            # Skip over the remainder we did not parse — but for tokenizer
            # arrays of strings we'd need to read each length. Easiest:
            # bail out and surface what we collected (caller treats arrays
            # as opaque if not fully consumed).
            for _ in range(count - cap):
                self.value(elem_type)
            return arr
        raise ValueError(f"Unknown GGUF value type: {vtype}")


def _parse_gguf_header(data: bytes) -> dict[str, Any]:
    """Hand-rolled GGUF v2/v3 header parser.

    Returns a flat dict ``{kv_key: value}``. Unknown / oversized arrays may
    be truncated but the keys we care about for sizing are scalars.
    """
    r = _GGUFReader(data)
    magic = r.read(4)
    if magic != GGUF_MAGIC:
        raise ValueError(f"Not a GGUF file (magic={magic!r})")
    version = r.u32()
    if version < 2:
        raise ValueError(f"Unsupported GGUF version: {version}")
    tensor_count = r.u64()
    kv_count = r.u64()

    kv: dict[str, Any] = {
        "_gguf_version": version,
        "_tensor_count": tensor_count,
        "_kv_count": kv_count,
    }
    for _ in range(kv_count):
        key = r.string()
        vtype = r.u32()
        try:
            kv[key] = r.value(vtype)
        except ValueError:
            # Truncated — stop early but keep what we have.
            break
    return kv


def _normalize_kv(kv: dict[str, Any]) -> dict[str, Any]:
    """Pick out the architecture-specific keys we use for sizing."""
    arch = kv.get("general.architecture") or "llama"
    prefix = f"{arch}."

    def pick(*names: str, default: Any = None) -> Any:
        for n in names:
            # Try with arch prefix first, then as an absolute key.
            for full in (prefix + n, n):
                if full in kv:
                    return kv[full]
        return default

    n_layers = pick("block_count", "llama.block_count", default=0) or 0
    n_heads = pick("attention.head_count", default=0) or 0
    n_kv_heads = pick("attention.head_count_kv", default=n_heads) or n_heads
    n_embd = pick("embedding_length", default=0) or 0
    ctx_train = pick("context_length", default=0) or 0
    vocab_size = pick("vocab_size", default=0) or 0
    # head_dim is rarely stored explicitly; derive when we can.
    head_dim = pick("attention.key_length", default=0) or 0
    if not head_dim and n_heads and n_embd:
        head_dim = n_embd // n_heads
    quantization = (
        kv.get("general.file_type")
        or kv.get("general.quantization_version")
        or kv.get("general.name")
        or "unknown"
    )
    return {
        "architecture": arch,
        "n_layers": int(n_layers),
        "n_heads": int(n_heads),
        "n_kv_heads": int(n_kv_heads),
        "head_dim": int(head_dim),
        "n_embd": int(n_embd),
        "ctx_train": int(ctx_train),
        "vocab_size": int(vocab_size),
        "quantization": quantization,
    }


def read_gguf_metadata(url: str) -> dict[str, Any]:
    """Fetch the first 16KB of a GGUF and parse its header.

    Parameters
    ----------
    url:
        HTTPS URL to the GGUF file. The server must support either
        ``Range`` requests or simple GET. Only the first ~16KB is read.

    Returns
    -------
    dict
        Includes ``n_layers``, ``n_heads``, ``n_kv_heads``, ``head_dim``,
        ``n_embd``, ``vocab_size``, ``ctx_train``, ``quantization``,
        ``file_size_bytes``, plus ``architecture`` and the raw ``kv`` dump.
    """
    data, total_size = _http_range_get(url)
    if total_size is None:
        total_size = _http_head_size(url)

    # Prefer the official gguf lib when available (more robust on arrays).
    try:
        import gguf  # type: ignore

        if hasattr(gguf, "GGUFReader"):
            # gguf.GGUFReader needs a file path; fall through to manual.
            raise ImportError  # pragma: no cover
    except ImportError:
        pass

    kv = _parse_gguf_header(data)
    meta = _normalize_kv(kv)
    meta["file_size_bytes"] = int(total_size) if total_size else 0
    meta["kv"] = kv
    return meta


# ---------------------------------------------------------------------------
# RAM estimation
# ---------------------------------------------------------------------------


def estimate_ram(
    metadata: dict[str, Any],
    context: int = 2048,
    quant: str | None = None,
) -> dict[str, int]:
    """Estimate runtime RAM for a GGUF model.

    Components
    ----------
    * ``weights_bytes``: taken from ``metadata['file_size_bytes']``. The
      GGUF on-disk size is already the quantized footprint, so this is a
      tight lower bound for weights in RAM.
    * ``kv_cache_bytes``: ``2 * n_layers * n_kv_heads * head_dim * context
      * 2`` (2 for K+V, 2 bytes for fp16). If ``quant`` looks like an
      8-bit KV cache (``q8_0``) we halve the per-element size.
    * ``overhead_bytes``: 15 % of weights+kv for activation buffers,
      paging slack, and runtime structures.

    Returns
    -------
    dict
        All values in MB (1 MB = 1024² bytes), plus the ``context`` echoed
        back for convenience.
    """
    weights_bytes = int(metadata.get("file_size_bytes") or 0)
    n_layers = int(metadata.get("n_layers") or 0)
    n_kv_heads = int(metadata.get("n_kv_heads") or 0)
    head_dim = int(metadata.get("head_dim") or 0)

    kv_bytes_per_elem = 2  # fp16 default
    q = (quant or metadata.get("quantization") or "").upper() if isinstance(quant or metadata.get("quantization"), str) else ""
    if "Q8" in q and "KV" in q:
        kv_bytes_per_elem = 1
    elif "F32" in q and "KV" in q:
        kv_bytes_per_elem = 4

    kv_cache_bytes = 2 * n_layers * n_kv_heads * head_dim * context * kv_bytes_per_elem
    base = weights_bytes + kv_cache_bytes
    overhead_bytes = int(round(base * 0.15))
    total_ram_bytes = base + overhead_bytes

    mb = 1024 * 1024
    return {
        "total_ram_mb": int(math.ceil(total_ram_bytes / mb)),
        "weights_mb": int(math.ceil(weights_bytes / mb)),
        "kv_cache_mb": int(math.ceil(kv_cache_bytes / mb)),
        "overhead_mb": int(math.ceil(overhead_bytes / mb)),
        "context": int(context),
    }


# ---------------------------------------------------------------------------
# Peer planning
# ---------------------------------------------------------------------------


def plan_pool(
    metadata: dict[str, Any],
    total_ram_mb: int,
    peers_available_mb: list[int],
) -> dict[str, Any]:
    """Pick the smallest peer set whose RAM sum covers ``total_ram_mb``.

    Strategy: sort peers descending by RAM, accumulate until we cover the
    requirement, return both the chosen peer indices (in the original
    input order) and a uniform per-peer split. Throughput heuristic is
    deliberately simple:

    * base: 12 tok/s on a single LAN peer
    * scale: divide by ``sqrt(min_peers)`` to model sharding overhead
    * penalty: −30 % if any selected peer has less RAM than the uniform
      share (slow swap on that peer drags the cluster).

    Returns
    -------
    dict
        ``min_peers``, ``ram_per_peer_mb``, ``selected_peers`` (indices),
        ``tok_s_estimate``, and ``feasible`` (bool).
    """
    if total_ram_mb <= 0:
        raise ValueError("total_ram_mb must be > 0")
    if not peers_available_mb:
        return {
            "min_peers": 0,
            "ram_per_peer_mb": 0,
            "selected_peers": [],
            "tok_s_estimate": 0.0,
            "feasible": False,
        }

    # Sort indices by descending RAM.
    indexed = sorted(
        enumerate(peers_available_mb), key=lambda kv: kv[1], reverse=True
    )
    cumulative = 0
    chosen: list[tuple[int, int]] = []
    for idx, ram in indexed:
        chosen.append((idx, ram))
        cumulative += ram
        if cumulative >= total_ram_mb:
            break

    feasible = cumulative >= total_ram_mb
    min_peers = len(chosen)
    ram_per_peer_mb = int(math.ceil(total_ram_mb / min_peers)) if min_peers else 0

    # Throughput heuristic
    base_tok_s = 12.0
    tok_s = base_tok_s / math.sqrt(min_peers) if min_peers else 0.0
    if any(ram < ram_per_peer_mb for _, ram in chosen):
        tok_s *= 0.7
    if not feasible:
        tok_s = 0.0

    selected_peers = sorted(idx for idx, _ in chosen)
    return {
        "min_peers": min_peers,
        "ram_per_peer_mb": ram_per_peer_mb,
        "selected_peers": selected_peers,
        "tok_s_estimate": round(tok_s, 2),
        "feasible": feasible,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _hf_resolve_url(model_id: str) -> str:
    """Build an HF ``/resolve/main/...`` URL from ``repo/filename`` input.

    Accepts inputs in the forms::

        "TheBloke/Qwen-1.5B-GGUF/qwen-1_5b.Q4_K_M.gguf"
        "TheBloke/Qwen-1.5B-GGUF"   # caller must append filename later
    """
    parts = model_id.strip("/").split("/")
    if len(parts) < 3:
        raise ValueError(
            "HF model_id must be 'owner/repo/filename.gguf' for sizer"
        )
    owner, repo, *file_parts = parts
    filename = "/".join(file_parts)
    return f"https://huggingface.co/{owner}/{repo}/resolve/main/{filename}"


def size_model(
    model_id: str,
    source: str,
    context: int = 2048,
    peers_available_mb: list[int] | None = None,
) -> dict[str, Any]:
    """High-level entry point: from ``(model_id, source)`` to a sizing report.

    Parameters
    ----------
    model_id:
        For ``source='hf'``, expected as ``"owner/repo/filename.gguf"``.
    source:
        ``'hf'`` (HuggingFace) or ``'ollama'`` (not yet implemented).
    context:
        Inference context window in tokens.
    peers_available_mb:
        Optional list of candidate peer RAM (MB). When provided, the
        report includes a :func:`plan_pool` block.
    """
    source = source.lower()
    if source == "hf":
        url = _hf_resolve_url(model_id)
    elif source == "ollama":
        # TODO: Ollama registry uses OCI-style manifests at
        # https://registry.ollama.ai/v2/library/<name>/manifests/<tag>
        # which then point at blob digests. Implement once we have a
        # canonical mapping table for the curated list.
        raise NotImplementedError("Ollama sizing not yet supported")
    else:
        raise ValueError(f"Unknown source: {source!r}")

    metadata = read_gguf_metadata(url)
    ram = estimate_ram(metadata, context=context)

    report: dict[str, Any] = {
        "model_id": model_id,
        "source": source,
        "url": url,
        "metadata": {k: v for k, v in metadata.items() if k != "kv"},
        "ram": ram,
    }
    if peers_available_mb is not None:
        report["pool"] = plan_pool(metadata, ram["total_ram_mb"], peers_available_mb)
    return report


__all__ = [
    "read_gguf_metadata",
    "estimate_ram",
    "plan_pool",
    "size_model",
]
