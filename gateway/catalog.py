"""Prometeu catalog — top LLMs from HuggingFace + curated Ollama list.

Two sources merged into one endpoint:

1. **HuggingFace** — official API `/api/models` with filter=gguf, sort=downloads.
   Stable, free, no auth required. Cached in Redis (TTL 6h) to be polite.

2. **Ollama** — no official catalog API. The site is JS-rendered (Next.js)
   and a registry-only manifest lookup requires knowing the model name first.
   We ship a CURATED list of popular Ollama models (refreshed periodically by
   maintainers) and enrich each entry with a registry manifest probe to fetch
   total size. Curated list is the source of truth — no scraping.

Cache strategy:
- Each fetcher writes to Redis key `prometeu:catalog:<source>` with TTL 6h.
- `/api/catalog/llms` is read-through: if cache hit, return; else refetch.
- Background refresh planned for Fase 3+ (today: lazy on first request).

Endpoints:
- `GET /api/catalog/llms?source=all|hf|ollama&limit=50` — top N from each.
- `GET /api/catalog/active` — LLMs currently being hosted by registered nodes,
  ranked by peer count and aggregate capacity (CPU cores + RAM MB).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import redis.asyncio as redis

CATALOG_TTL_SEC = int(os.getenv("PROMETEU_CATALOG_TTL_SEC", str(6 * 3600)))
HF_API = "https://huggingface.co/api/models"
HF_TIMEOUT = 10.0
OLLAMA_REGISTRY = "https://registry.ollama.ai/v2/library"
OLLAMA_TIMEOUT = 8.0

# Curated Ollama popular models. Order = manually ranked by community
# popularity (downloads on ollama.com/library). Refresh: open PR to update.
# Source: ollama.com/search?o=popular (manual inspection, last refreshed 2026-06).
OLLAMA_CURATED: list[dict[str, Any]] = [
    {"name": "llama3.2", "params": "3B", "family": "llama", "tags": ["chat", "general"]},
    {"name": "llama3.1", "params": "8B", "family": "llama", "tags": ["chat", "general"]},
    {"name": "llama3", "params": "8B", "family": "llama", "tags": ["chat", "general"]},
    {"name": "qwen3", "params": "varied", "family": "qwen", "tags": ["chat", "reasoning"]},
    {"name": "qwen2.5", "params": "0.5B-72B", "family": "qwen", "tags": ["chat", "code"]},
    {"name": "qwen2.5-coder", "params": "0.5B-32B", "family": "qwen", "tags": ["code"]},
    {"name": "gemma3", "params": "1B-27B", "family": "gemma", "tags": ["chat", "multimodal"]},
    {"name": "gemma2", "params": "2B-27B", "family": "gemma", "tags": ["chat"]},
    {"name": "phi4", "params": "14B", "family": "phi", "tags": ["chat", "reasoning"]},
    {"name": "phi3", "params": "3.8B-14B", "family": "phi", "tags": ["chat"]},
    {"name": "mistral", "params": "7B", "family": "mistral", "tags": ["chat"]},
    {"name": "mistral-nemo", "params": "12B", "family": "mistral", "tags": ["chat"]},
    {"name": "mixtral", "params": "8x7B", "family": "mistral", "tags": ["chat", "moe"]},
    {"name": "deepseek-r1", "params": "1.5B-671B", "family": "deepseek", "tags": ["reasoning"]},
    {"name": "deepseek-v3", "params": "671B", "family": "deepseek", "tags": ["chat", "moe"]},
    {"name": "deepseek-coder-v2", "params": "16B-236B", "family": "deepseek", "tags": ["code"]},
    {"name": "deepseek-coder", "params": "1.3B-33B", "family": "deepseek", "tags": ["code"]},
    {"name": "codellama", "params": "7B-70B", "family": "llama", "tags": ["code"]},
    {"name": "nomic-embed-text", "params": "137M", "family": "nomic", "tags": ["embed"]},
    {"name": "mxbai-embed-large", "params": "335M", "family": "mxbai", "tags": ["embed"]},
    {"name": "snowflake-arctic-embed", "params": "335M", "family": "snowflake", "tags": ["embed"]},
    {"name": "all-minilm", "params": "22M-33M", "family": "minilm", "tags": ["embed"]},
    {"name": "llava", "params": "7B-34B", "family": "llava", "tags": ["multimodal"]},
    {"name": "moondream", "params": "1.8B", "family": "moondream", "tags": ["multimodal"]},
    {"name": "llama3.2-vision", "params": "11B-90B", "family": "llama", "tags": ["multimodal"]},
    {"name": "minicpm-v", "params": "8B", "family": "minicpm", "tags": ["multimodal"]},
    {"name": "command-r", "params": "35B", "family": "cohere", "tags": ["chat", "rag"]},
    {"name": "command-r-plus", "params": "104B", "family": "cohere", "tags": ["chat", "rag"]},
    {"name": "yi", "params": "6B-34B", "family": "yi", "tags": ["chat"]},
    {"name": "vicuna", "params": "7B-33B", "family": "vicuna", "tags": ["chat"]},
    {"name": "starcoder2", "params": "3B-15B", "family": "starcoder", "tags": ["code"]},
    {"name": "wizardlm2", "params": "7B-8x22B", "family": "wizard", "tags": ["chat"]},
    {"name": "tinyllama", "params": "1.1B", "family": "llama", "tags": ["chat", "small"]},
    {"name": "neural-chat", "params": "7B", "family": "intel", "tags": ["chat"]},
    {"name": "openhermes", "params": "7B", "family": "hermes", "tags": ["chat"]},
    {"name": "dolphin-llama3", "params": "8B-70B", "family": "dolphin", "tags": ["chat", "uncensored"]},
    {"name": "dolphin-mistral", "params": "7B", "family": "dolphin", "tags": ["chat", "uncensored"]},
    {"name": "dolphin-mixtral", "params": "8x7B-8x22B", "family": "dolphin", "tags": ["chat", "uncensored"]},
    {"name": "zephyr", "params": "7B-141B", "family": "zephyr", "tags": ["chat"]},
    {"name": "stablelm2", "params": "1.6B-12B", "family": "stable", "tags": ["chat"]},
    {"name": "smollm2", "params": "135M-1.7B", "family": "smol", "tags": ["chat", "small"]},
    {"name": "granite3.1-dense", "params": "2B-8B", "family": "ibm", "tags": ["chat"]},
    {"name": "granite-code", "params": "3B-34B", "family": "ibm", "tags": ["code"]},
    {"name": "aya", "params": "8B-35B", "family": "cohere", "tags": ["chat", "multilingual"]},
    {"name": "sailor2", "params": "1B-20B", "family": "sailor", "tags": ["chat", "multilingual"]},
    {"name": "exaone3.5", "params": "2.4B-32B", "family": "exaone", "tags": ["chat"]},
    {"name": "marco-o1", "params": "7B", "family": "marco", "tags": ["reasoning"]},
    {"name": "qwq", "params": "32B", "family": "qwen", "tags": ["reasoning"]},
    {"name": "olmo2", "params": "7B-13B", "family": "olmo", "tags": ["chat", "open-data"]},
    {"name": "falcon3", "params": "1B-10B", "family": "falcon", "tags": ["chat"]},
]


async def _hf_fetch(client: httpx.AsyncClient, limit: int, sort: str = "downloads") -> list[dict[str, Any]]:
    """GGUF text-generation models from HF, sorted by downloads or lastModified."""
    hf_sort = "lastModified" if sort == "updated" else "downloads"
    params = {
        "sort": hf_sort,
        "direction": "-1",
        "limit": str(limit * 2),  # over-fetch; we filter
        "filter": "gguf",
        "pipeline_tag": "text-generation",
    }
    r = await client.get(HF_API, params=params, timeout=HF_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    out: list[dict[str, Any]] = []
    for m in data:
        mid = m.get("modelId") or m.get("id")
        if not mid:
            continue
        out.append({
            "source": "huggingface",
            "id": mid,
            "url": f"https://huggingface.co/{mid}",
            "downloads": int(m.get("downloads") or 0),
            "likes": int(m.get("likes") or 0),
            "last_modified": m.get("lastModified") or m.get("last_modified"),
            "pipeline_tag": m.get("pipeline_tag"),
            "tags": [t for t in (m.get("tags") or []) if isinstance(t, str)][:12],
        })
        if len(out) >= limit:
            break
    return out


async def _ollama_size_probe(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    """Best-effort probe of `latest` manifest for total GGUF layer size."""
    try:
        r = await client.get(
            f"{OLLAMA_REGISTRY}/{name}/manifests/latest",
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            timeout=OLLAMA_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        manifest = r.json()
        layers = manifest.get("layers", [])
        total = sum(int(l.get("size") or 0) for l in layers
                    if str(l.get("mediaType", "")).endswith("image.model"))
        return {"size_bytes_latest": total} if total else {}
    except Exception:
        return {}


async def _ollama_fetch(client: httpx.AsyncClient, limit: int) -> list[dict[str, Any]]:
    """Curated Ollama list + best-effort size probe of `latest` tag."""
    out: list[dict[str, Any]] = []
    items = OLLAMA_CURATED[:limit]
    probes = await asyncio.gather(
        *[_ollama_size_probe(client, item["name"]) for item in items],
        return_exceptions=True,
    )
    for item, probe in zip(items, probes):
        extra = probe if isinstance(probe, dict) else {}
        out.append({
            "source": "ollama",
            "id": item["name"],
            "url": f"https://ollama.com/library/{item['name']}",
            "params": item["params"],
            "family": item["family"],
            "tags": item["tags"],
            **extra,
        })
    return out


async def fetch_catalog(
    r: redis.Redis,
    source: str = "all",
    limit: int = 50,
    force_refresh: bool = False,
    sort: str = "downloads",
) -> dict[str, Any]:
    """Read-through Redis cache wrapper around HF + Ollama fetchers.

    Returns dict with keys: huggingface[], ollama[], generated_at,
    cache_hits[], errors[].
    """
    limit = max(1, min(limit, 100))
    sort = sort if sort in {"downloads", "updated"} else "downloads"
    sources = {"all": {"hf", "ollama"}, "hf": {"hf"}, "ollama": {"ollama"}}.get(source, {"hf", "ollama"})

    out: dict[str, Any] = {
        "generated_at": time.time(),
        "limit": limit,
        "huggingface": [],
        "ollama": [],
        "cache_hits": [],
        "errors": [],
        "sort": sort,
    }

    async with httpx.AsyncClient(headers={"User-Agent": "Prometeu/catalog (+https://github.com/maxwellmelo/prometeu)"}) as client:
        tasks = {}
        if "hf" in sources:
            tasks["huggingface"] = _maybe_fetch(r, client, "huggingface", limit, force_refresh, _hf_fetch, sort=sort)
        if "ollama" in sources:
            tasks["ollama"] = _maybe_fetch(r, client, "ollama", limit, force_refresh, _ollama_fetch)
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                out["errors"].append({"source": key, "error": str(result)})
                continue
            items, cache_hit = result
            out[key] = items
            if cache_hit:
                out["cache_hits"].append(key)
    return out


async def _maybe_fetch(
    r: redis.Redis,
    client: httpx.AsyncClient,
    source: str,
    limit: int,
    force_refresh: bool,
    fetcher,
    sort: str = "downloads",
) -> tuple[list[dict[str, Any]], bool]:
    key = f"prometeu:catalog:{source}:sort{sort}:limit{limit}"
    if not force_refresh:
        raw = await r.get(key)
        if raw:
            try:
                return json.loads(raw), True
            except Exception:
                pass  # corrupt cache; refetch
    if source == "huggingface":
        items = await fetcher(client, limit, sort)
    else:
        items = await fetcher(client, limit)
    try:
        await r.set(key, json.dumps(items), ex=CATALOG_TTL_SEC)
    except Exception:
        pass  # cache write failure is non-fatal
    return items, False


# ─── Active LLMs ranked by peer + capacity ────────────────────────────────

def aggregate_active_llms(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate registered node list by `active_model` (or first of `models`).

    Returns dict with `models[]` sorted by:
        1. peers (count of online nodes hosting that model) DESC
        2. capacity_score (sum of cpu_count * 100 + ram_total_mb / 1024) DESC
        3. id ASC (stable)
    """
    buckets: dict[str, dict[str, Any]] = {}
    for n in nodes:
        if not n.get("online"):
            continue
        model_id = n.get("active_model") or _first_model(n.get("models"))
        if not model_id:
            continue
        b = buckets.setdefault(model_id, {
            "model_id": model_id,
            "source": _guess_source(model_id),
            "peers": 0,
            "cpu_cores_total": 0,
            "ram_mb_total": 0,
            "capacity_score": 0.0,
            "node_ids": [],
        })
        hw = n.get("hardware") or {}
        cpu = int(hw.get("cpu_count") or hw.get("cpus") or 0)
        ram = int(hw.get("ram_total_mb") or hw.get("ram_mb") or 0)
        b["peers"] += 1
        b["cpu_cores_total"] += cpu
        b["ram_mb_total"] += ram
        b["capacity_score"] += cpu * 100 + ram / 1024.0
        b["node_ids"].append(n.get("node_id"))

    ranked = sorted(
        buckets.values(),
        key=lambda b: (-b["peers"], -b["capacity_score"], b["model_id"]),
    )
    return {
        "generated_at": time.time(),
        "models": ranked,
        "total_models": len(ranked),
        "total_peers": sum(b["peers"] for b in ranked),
    }


def _first_model(v: Any) -> str | None:
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _guess_source(model_id: str) -> str:
    if "/" in model_id:
        return "huggingface"
    return "ollama"
