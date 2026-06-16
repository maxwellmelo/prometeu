"""
Prometeu Gateway — proxy fino llama-server + telemetria de nós distribuídos.

Lê configuração de nós e modelo de variáveis de ambiente / arquivo JSON,
faz proxy /v1/* OpenAI-compatible pro llama-server (master) e expõe /api/nodes
com a topologia humanizada do cluster.

Env vars:
  PROMETEU_LLAMA_URL     URL do llama-server master       (default: http://127.0.0.1:8080)
  PROMETEU_CONFIG        Path pra JSON de config          (default: ./config.json ou /etc/prometeu/config.json)
  PROMETEU_WEB_DIR       Diretório do frontend            (default: ./web ou /opt/prometeu/web)

Config JSON formato:
{
  "model_name": "Qwen 2.5 1.5B Instruct Q4_K_M",
  "nodes": [
    {"id": "master",  "host": "10.0.0.100", "port": 8080,  "role": "master + camadas 0-8"},
    {"id": "worker1", "host": "10.0.0.101", "port": 50052, "role": "RPC worker camadas 9-18"},
    {"id": "worker2", "host": "10.0.0.102", "port": 50052, "role": "RPC worker camadas 19-27"}
  ]
}
"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# `catalog` is loaded as a flat sibling module in prod (/opt/prometeu/catalog.py)
# but as `gateway.catalog` package member in tests/repo. Support both layouts.
try:
    from catalog import aggregate_active_llms, fetch_catalog  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway.catalog import aggregate_active_llms, fetch_catalog  # type: ignore[no-redef]

try:
    from router import select_peer_for_model, list_served_models, _model_matches  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway.router import select_peer_for_model, list_served_models, _model_matches  # type: ignore[no-redef]

try:
    from sizer import size_model  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway.sizer import size_model  # type: ignore[no-redef]

try:
    import pools as poolmod  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway import pools as poolmod  # type: ignore[no-redef]

try:
    import reciprocity as recip  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway import reciprocity as recip  # type: ignore[no-redef]

try:
    import allowlist as allowmod  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    from gateway import allowlist as allowmod  # type: ignore[no-redef]


LLAMA_URL = os.getenv("PROMETEU_LLAMA_URL", "http://127.0.0.1:8080")
# The built-in master peer serves this legacy model directly via LLAMA_URL.
# Requests for it (or with no model specified) route to the master; everything
# else routes to a registry peer that advertises it. No silent fallback: if a
# named model has no serving peer, proxy_v1 returns 503.
MASTER_MODEL = os.getenv("PROMETEU_MASTER_MODEL", "qwen2.5-1.5b-q4")


def _find_config() -> Path:
    if cfg := os.getenv("PROMETEU_CONFIG"):
        return Path(cfg)
    for p in (Path("./config.json"), Path("/etc/prometeu/config.json"), Path("/opt/prometeu/config.json")):
        if p.is_file():
            return p
    # default fallback embutido
    return Path(__file__).parent / "config.example.json"


def _find_web_dir() -> Path:
    if d := os.getenv("PROMETEU_WEB_DIR"):
        return Path(d)
    for p in (Path("./web"), Path("/opt/prometeu/web")):
        if p.is_dir():
            return p
    return Path(__file__).parent.parent / "web"


_cfg_path = _find_config()
_cfg = json.loads(_cfg_path.read_text())
MODEL_NAME: str = _cfg["model_name"]
NODES: list[dict] = _cfg["nodes"]
REGISTRY_TTL_SEC = int(os.getenv("PROMETEU_REGISTRY_TTL_SEC", "120"))
REDIS_URL = os.getenv("PROMETEU_REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_NODE_PREFIX = "prometeu:nodes:"
REDIS_NODE_INDEX = "prometeu:nodes:index"

node_status_cache: dict[str, Any] = {"checked_at": 0, "nodes": []}
redis_client: redis.Redis | None = None


class NodeJoin(BaseModel):
    node_id: str = Field(min_length=3, max_length=128)
    display_name: str | None = None
    version: str = "unknown"
    public_key: str | None = None
    mode: str = "public"
    models: list[str] = []
    # active_model is the LLM the node is *currently* hosting/contributing to
    # (more granular than `models` which can be a list of supported LLMs).
    # Used by /api/catalog/active to rank LLMs by peer count + capacity.
    active_model: str | None = None
    limits: dict[str, Any] = {}
    hardware: dict[str, Any] = {}
    status: str = "available"
    dashboard_url: str | None = None
    rpc_endpoint: str | None = None
    # inference summary advertised by the node-agent (Fase 1+): which models the
    # peer is currently serving, their local endpoints, and ready state. Used by
    # the router (Fase 2) and pool orchestrator (Fase 4).
    inference: dict[str, Any] = {}


class NodeHeartbeat(NodeJoin):
    pass


def _redis() -> redis.Redis:
    if redis_client is None:
        raise RuntimeError("Redis not initialized")
    return redis_client


async def init_registry() -> None:
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    await redis_client.ping()


def _node_key(node_id: str) -> str:
    return f"{REDIS_NODE_PREFIX}{node_id}"


async def _upsert_node(payload: NodeJoin) -> dict[str, Any]:
    r = _redis()
    now = time.time()
    key = _node_key(payload.node_id)
    old_raw = await r.get(key)
    first_seen = now
    if old_raw:
        try:
            first_seen = float(json.loads(old_raw).get("first_seen", now))
        except Exception:
            first_seen = now
    data = payload.model_dump()
    data.update({
        "first_seen": first_seen,
        "last_seen": now,
    })
    await r.set(key, json.dumps(data), ex=REGISTRY_TTL_SEC)
    await r.sadd(REDIS_NODE_INDEX, payload.node_id)
    return {"ok": True, "node_id": payload.node_id, "backend": "redis", "first_seen": first_seen, "last_seen": now}


async def _list_registry_nodes() -> list[dict[str, Any]]:
    r = _redis()
    now = time.time()
    node_ids = sorted(await r.smembers(REDIS_NODE_INDEX))
    nodes: list[dict[str, Any]] = []
    stale: list[str] = []
    if not node_ids:
        return nodes
    values = await r.mget([_node_key(nid) for nid in node_ids])
    for node_id, raw in zip(node_ids, values):
        if raw is None:
            stale.append(node_id)
            continue
        try:
            n = json.loads(raw)
        except Exception:
            stale.append(node_id)
            continue
        last_seen = float(n.get("last_seen") or 0)
        ttl = await r.ttl(_node_key(node_id))
        n["age_sec"] = round(now - last_seen, 1)
        n["ttl_sec"] = ttl
        n["online"] = ttl > 0 and n.get("status") != "offline"
        nodes.append(n)
    if stale:
        await r.srem(REDIS_NODE_INDEX, *stale)
    nodes.sort(key=lambda n: n.get("last_seen", 0), reverse=True)
    return nodes


async def _fetch_agent(host: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"http://{host}:9100/status")
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


async def refresh_nodes() -> list[dict]:
    """
    Health has two layers:
    - cluster_ok: llama-server says model+RPC graph are alive.
    - agent: per-node telemetry (CPU/RAM/network/process), used to prove workers
      are doing real work during inference.
    """
    master_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{LLAMA_URL}/health")
            master_ok = r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        master_ok = False

    agents = await asyncio.gather(*[_fetch_agent(n["host"]) for n in NODES])

    out = []
    for n, agent in zip(NODES, agents):
        process_alive = bool(agent.get("process_alive")) if agent else None
        alive = bool(master_ok and (process_alive is not False))
        out.append({
            "id": n["id"],
            "host": n["host"],
            "port": n["port"],
            "role": n["role"],
            "layers": n.get("layers"),
            "alive": alive,
            "cluster_ok": master_ok,
            "agent_ok": agent is not None,
            "telemetry": agent,
        })
    node_status_cache["nodes"] = out
    node_status_cache["checked_at"] = time.time()
    # Gauges Prometheus (best-effort)
    try:
        alive = sum(1 for n in out if n.get("alive"))
        METRIC_NODES_ALIVE.set(alive)
        METRIC_NODES_TOTAL.set(len(out))
        # Mesh peers: conta entries no índice Redis (TTL-based)
        try:
            r = _redis()
            mesh_count = await r.scard("prometeu:mesh:index")
            METRIC_MESH_PEERS.set(int(mesh_count or 0))
        except Exception:
            pass
    except Exception:
        pass
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_registry()
    await refresh_nodes()

    async def loop():
        while True:
            try:
                await refresh_nodes()
            except Exception:
                pass
            await asyncio.sleep(5)

    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="Prometeu Gateway", lifespan=lifespan)

# ─── Rate limiting (slowapi) ─────────────────────────────────────────
RATE_LIMIT_V1 = os.getenv("PROMETEU_RATE_LIMIT_V1", "30/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    METRIC_RATE_LIMITED.labels(path=request.url.path).inc()
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limit_exceeded", "detail": str(exc.detail)},
        headers={"Retry-After": "60"},
    )


# ─── Prometheus metrics ──────────────────────────────────────────────
METRIC_REQUESTS = Counter(
    "prometeu_requests_total",
    "HTTP requests processed",
    ["path", "method", "status"],
)
METRIC_LATENCY = Histogram(
    "prometeu_request_latency_seconds",
    "Request latency in seconds",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)
METRIC_TOKENS = Counter(
    "prometeu_tokens_total",
    "Tokens served (reported by llama-server)",
    ["kind"],  # prompt | completion | total
)
METRIC_INFERENCE_REQUESTS = Counter(
    "prometeu_inference_requests_total",
    "Inference requests credited by usage",
)
METRIC_RATE_LIMITED = Counter(
    "prometeu_rate_limited_total",
    "Requests rejected by rate limiter",
    ["path"],
)
METRIC_NODES_ALIVE = Gauge(
    "prometeu_nodes_alive",
    "Cluster nodes alive (master + workers)",
)
METRIC_NODES_TOTAL = Gauge(
    "prometeu_nodes_total",
    "Cluster nodes total (configured)",
)
METRIC_MESH_PEERS = Gauge(
    "prometeu_mesh_peers_online",
    "Mesh peers online in registry (Redis TTL)",
)
METRIC_ACTIVE_LLMS = Gauge(
    "prometeu_active_llms_total",
    "Distinct LLMs being hosted by at least one online registered node",
)
METRIC_POOLS = Gauge(
    "prometeu_pools_total",
    "Pools by state",
    ["state"],
)
METRIC_RECIPROCITY_SOFT_QUOTA = Counter(
    "prometeu_reciprocity_soft_quota_total",
    "Requests over reciprocity soft quota",
    ["authenticated"],
)
METRIC_CONSUMED_TOKENS = Counter(
    "prometeu_reciprocity_consumed_tokens_total",
    "Tokens consumed through gateway /v1 by auth class",
    ["authenticated"],
)


def _update_pool_metrics(pools: list[Any]) -> None:
    try:
        states = ["REQUESTED", "WARMING", "READY", "DEGRADED", "FAILED", "STOPPED"]
        counts = {s: 0 for s in states}
        for p in pools:
            st = p.state if hasattr(p, "state") else str((p or {}).get("state"))
            counts[st] = counts.get(st, 0) + 1
        for st, n in counts.items():
            METRIC_POOLS.labels(st).set(n)
    except Exception:
        pass


def _metric_auth_label(authenticated: bool) -> str:
    return "true" if authenticated else "false"


async def _bump_consumption_metrics(authenticated: bool, tokens: int) -> None:
    try:
        if tokens > 0:
            METRIC_CONSUMED_TOKENS.labels(_metric_auth_label(authenticated)).inc(tokens)
    except Exception:
        pass



@app.middleware("http")
async def _metrics_mw(request: Request, call_next):
    # Normaliza path pra evitar high-cardinality (ex: /v1/chat/completions vs /v1/foo)
    raw = request.url.path
    if raw.startswith("/v1/"):
        path_label = "/v1/" + raw.split("/", 2)[2].split("?")[0]
    elif raw.startswith("/api/"):
        path_label = raw.split("?")[0]
    elif raw == "/metrics":
        path_label = "/metrics"
    else:
        path_label = "static"
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        METRIC_REQUESTS.labels(path=path_label, method=request.method, status="500").inc()
        METRIC_LATENCY.labels(path=path_label).observe(time.perf_counter() - start)
        raise
    METRIC_REQUESTS.labels(path=path_label, method=request.method, status=str(status)).inc()
    METRIC_LATENCY.labels(path=path_label).observe(time.perf_counter() - start)
    # Attribution header per NOTICE (Apache 2.0 §4 attribution requirement).
    # DO NOT remove. Removal of this header in derivative deployments is a
    # license breach. See NOTICE in repo root.
    response.headers["X-Powered-By"] = "Prometeu (https://github.com/maxwellmelo/prometeu)"
    return response


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/nodes")
async def api_nodes():
    return {
        "model": MODEL_NAME,
        "nodes": node_status_cache["nodes"],
        "checked_at": node_status_cache["checked_at"],
        "alive_count": sum(1 for n in node_status_cache["nodes"] if n["alive"]),
        "total": len(node_status_cache["nodes"]),
    }


@app.get("/api/health")
async def api_health():
    async with httpx.AsyncClient(timeout=2.0) as c:
        try:
            r = await c.get(f"{LLAMA_URL}/health")
            return {"gateway": "ok", "llama_server": r.json()}
        except Exception as e:
            return JSONResponse(
                {"gateway": "ok", "llama_server": "down", "err": str(e)},
                status_code=503,
            )


@app.post("/api/registry/join")
async def registry_join(payload: NodeJoin):
    return await _upsert_node(payload)


@app.post("/api/registry/heartbeat")
async def registry_heartbeat(payload: NodeHeartbeat):
    return await _upsert_node(payload)


@app.post("/api/registry/leave")
async def registry_leave(payload: dict[str, Any]):
    node_id = str(payload.get("node_id", ""))
    if not node_id:
        return JSONResponse({"ok": False, "error": "node_id required"}, status_code=400)
    r = _redis()
    await r.delete(_node_key(node_id))
    await r.srem(REDIS_NODE_INDEX, node_id)
    return {"ok": True, "node_id": node_id, "backend": "redis"}


@app.get("/api/registry/nodes")
async def registry_nodes():
    await init_registry()
    nodes = await _list_registry_nodes()
    return {
        "backend": "redis",
        "nodes": nodes,
        "total": len(nodes),
        "online": sum(1 for n in nodes if n["online"] and n["status"] != "offline"),
        "ttl_sec": REGISTRY_TTL_SEC,
    }


# --- catalog (HuggingFace + Ollama) + active LLM stats ---


@app.get("/api/catalog/llms")
async def catalog_llms(source: str = "all", limit: int = 50, refresh: int = 0):
    """Top LLMs from HuggingFace + curated Ollama list.

    Query params:
        source: all | hf | ollama  (default: all)
        limit:  1..100             (default: 50)
        refresh: 1 to force cache miss (cooldown: respect 6h cache otherwise)
    """
    await init_registry()
    r = _redis()
    try:
        return await fetch_catalog(r, source=source, limit=limit, force_refresh=bool(refresh))
    except Exception as e:
        return JSONResponse({"error": "catalog_fetch_failed", "detail": str(e)}, status_code=502)


@app.get("/api/catalog/active")
async def catalog_active():
    """LLMs currently being hosted by registered nodes, ranked by peers + capacity."""
    await init_registry()
    nodes = await _list_registry_nodes()
    result = aggregate_active_llms(nodes)
    METRIC_ACTIVE_LLMS.set(result["total_models"])
    return result


# --- mesh discovery (Iroh peer registry) ---

REDIS_MESH_PREFIX = "prometeu:mesh:"
REDIS_MESH_INDEX = "prometeu:mesh:index"
MESH_TTL_SEC = int(os.getenv("PROMETEU_MESH_TTL_SEC", "120"))


def _mesh_key(node_id: str) -> str:
    return f"{REDIS_MESH_PREFIX}{node_id}"


async def _list_mesh_peers(capability: str | None = None) -> list[dict[str, Any]]:
    r = _redis()
    ids = await r.smembers(REDIS_MESH_INDEX)
    out: list[dict[str, Any]] = []
    stale: list[str] = []
    for nid in ids:
        raw = await r.get(_mesh_key(nid))
        if raw is None:
            stale.append(nid)
            continue
        try:
            data = json.loads(raw)
        except Exception:
            stale.append(nid)
            continue
        if capability and data.get("capability") != capability:
            continue
        out.append(data)
    if stale:
        for nid in stale:
            await r.srem(REDIS_MESH_INDEX, nid)
    return out


@app.post("/api/mesh/announce")
async def mesh_announce(payload: dict[str, Any]):
    node_id = str(payload.get("node_id", "")).strip()
    if not node_id or len(node_id) != 64:
        return JSONResponse({"ok": False, "error": "node_id must be 64-char hex"}, status_code=400)
    capability = str(payload.get("capability", "rpc-worker")).strip() or "rpc-worker"
    data = {
        "node_id": node_id,
        "capability": capability,
        "model": str(payload.get("model", "")),
        "layers": str(payload.get("layers", "")),
        "region_hint": str(payload.get("region_hint", "")),
        "display_name": str(payload.get("display_name", "")),
        "advertised_at": int(time.time()),
        "schema": "prometeu/advertisement/1",
    }
    r = _redis()
    await r.set(_mesh_key(node_id), json.dumps(data), ex=MESH_TTL_SEC)
    await r.sadd(REDIS_MESH_INDEX, node_id)
    return {"ok": True, "node_id": node_id, "ttl_sec": MESH_TTL_SEC}


@app.get("/api/mesh/peers")
async def mesh_peers(capability: str | None = None):
    peers = await _list_mesh_peers(capability)
    return {
        "schema": "prometeu/mesh/peers/1",
        "peers": peers,
        "total": len(peers),
        "ttl_sec": MESH_TTL_SEC,
    }


# --- mesh receipts (signed work ledger aggregator) ---

REDIS_RECEIPTS_PREFIX = "prometeu:mesh:receipts:"
REDIS_RECEIPTS_INDEX = "prometeu:mesh:receipts:index"
REDIS_RECEIPTS_RECENT = "prometeu:mesh:receipts:recent"
RECEIPTS_RECENT_MAX = int(os.getenv("PROMETEU_RECEIPTS_RECENT_MAX", "200"))


def _receipts_key(node_id: str) -> str:
    return f"{REDIS_RECEIPTS_PREFIX}{node_id}"


@app.post("/api/mesh/receipts")
async def mesh_receipts_ingest(payload: dict[str, Any]):
    """Ingest a signed receipt from a mesh node (dialer or server side).

    Body shape: ReceiptSigned JSON from prometeu-mesh:
      { receipt: {session_id, server_node_id, dialer_node_id, capability,
                  bytes_in, bytes_out, tokens_served, opened_at, closed_at, ...},
        server_sig, dialer_sig }
    """
    receipt = payload.get("receipt") or {}
    session_id = str(receipt.get("session_id", "")).strip()
    server_node_id = str(receipt.get("server_node_id", "")).strip()
    dialer_node_id = str(receipt.get("dialer_node_id", "")).strip()
    if not session_id or not (server_node_id or dialer_node_id):
        return JSONResponse({"ok": False, "error": "receipt missing session_id or node ids"}, status_code=400)
    try:
        bytes_in = int(receipt.get("bytes_in", 0))
        bytes_out = int(receipt.get("bytes_out", 0))
        tokens_served = int(receipt.get("tokens_served", 0))
        closed_at = int(receipt.get("closed_at", int(time.time())))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid numeric fields"}, status_code=400)

    server_sig = str(payload.get("server_sig", ""))
    dialer_sig = str(payload.get("dialer_sig", ""))
    # Side: which party signed this submission (dialer-side or server-side).
    side = "server" if server_sig and not dialer_sig else ("dialer" if dialer_sig and not server_sig else "both")

    r = _redis()
    # Aggregate per-node (only credit the signing side to avoid double count when both sides POST)
    targets: list[str] = []
    if side in ("server", "both"):
        if server_node_id:
            targets.append(server_node_id)
    if side == "dialer" and dialer_node_id:
        targets.append(dialer_node_id)

    for nid in targets:
        key = _receipts_key(nid)
        await r.hincrby(key, "bytes_in", bytes_in)
        await r.hincrby(key, "bytes_out", bytes_out)
        await r.hincrby(key, "tokens_served", tokens_served)
        await r.hincrby(key, "sessions", 1)
        await r.hset(key, "last_seen", closed_at)
        await r.sadd(REDIS_RECEIPTS_INDEX, nid)

    summary = {
        "session_id": session_id,
        "server_node_id": server_node_id,
        "dialer_node_id": dialer_node_id,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "tokens_served": tokens_served,
        "closed_at": closed_at,
        "side": side,
        "received_at": int(time.time()),
    }
    await r.lpush(REDIS_RECEIPTS_RECENT, json.dumps(summary))
    await r.ltrim(REDIS_RECEIPTS_RECENT, 0, RECEIPTS_RECENT_MAX - 1)
    return {"ok": True, "credited": targets, "side": side}


@app.get("/api/mesh/receipts")
async def mesh_receipts_summary(node_id: str | None = None, recent: int = 20):
    r = _redis()
    if node_id:
        h = await r.hgetall(_receipts_key(node_id))
        return {
            "schema": "prometeu/mesh/receipts/1",
            "node_id": node_id,
            "totals": {k: (int(v) if k != "last_seen" else int(v)) for k, v in h.items()} if h else {},
        }
    ids = await r.smembers(REDIS_RECEIPTS_INDEX)
    nodes: list[dict[str, Any]] = []
    grand = {"bytes_in": 0, "bytes_out": 0, "tokens_served": 0, "sessions": 0}
    for nid in ids:
        h = await r.hgetall(_receipts_key(nid))
        if not h:
            continue
        entry = {"node_id": nid}
        for k, v in h.items():
            try:
                entry[k] = int(v)
            except (TypeError, ValueError):
                entry[k] = v
        for k in ("bytes_in", "bytes_out", "tokens_served", "sessions"):
            if isinstance(entry.get(k), int):
                grand[k] += entry[k]
        nodes.append(entry)
    nodes.sort(key=lambda e: e.get("bytes_out", 0) + e.get("bytes_in", 0), reverse=True)

    recent_n = max(0, min(int(recent), RECEIPTS_RECENT_MAX))
    recent_list: list[dict[str, Any]] = []
    if recent_n:
        raws = await r.lrange(REDIS_RECEIPTS_RECENT, 0, recent_n - 1)
        for raw in raws:
            try:
                recent_list.append(json.loads(raw))
            except Exception:
                continue
    # Tokens reais reportados pelo llama-server via gateway proxy hook
    try:
        tk = await r.mget(REDIS_TOKENS_TOTAL, REDIS_TOKENS_PROMPT, REDIS_TOKENS_COMPLETION, REDIS_TOKENS_REQUESTS)
        tokens_block = {
            "total": int(tk[0] or 0),
            "prompt": int(tk[1] or 0),
            "completion": int(tk[2] or 0),
            "requests": int(tk[3] or 0),
        }
    except Exception:
        tokens_block = {"total": 0, "prompt": 0, "completion": 0, "requests": 0}
    grand["tokens_served"] = tokens_block["completion"] or tokens_block["total"]

    return {
        "schema": "prometeu/mesh/receipts/1",
        "totals": grand,
        "tokens": tokens_block,
        "nodes": nodes,
        "recent": recent_list,
    }


@app.post("/api/mesh/leave")
async def mesh_leave(payload: dict[str, Any]):
    node_id = str(payload.get("node_id", "")).strip()
    if not node_id:
        return JSONResponse({"ok": False, "error": "node_id required"}, status_code=400)
    r = _redis()
    await r.delete(_mesh_key(node_id))
    await r.srem(REDIS_MESH_INDEX, node_id)
    return {"ok": True, "node_id": node_id}


# Proxy genérico /v1/* pra llama-server (OpenAI-compatible)
async def _proxy_stream(method: str, path: str, body: bytes, headers: dict, base_url: str):
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(method, f"{base_url}{path}", content=body, headers=headers) as r:
            yield ("HEADERS", r.status_code, dict(r.headers))
            async for chunk in r.aiter_bytes():
                yield ("DATA", chunk)


def _extract_model_from_body(body: bytes) -> str | None:
    if not body:
        return None
    try:
        obj = json.loads(body)
        m = obj.get("model")
        return m if isinstance(m, str) and m else None
    except Exception:
        return None


async def _resolve_target(requested_model: str | None) -> dict[str, Any]:
    """Decide where a /v1 request goes.

    Returns {"base_url", "served_by", "node_id"} or raises a 503-worthy
    LookupError if a named model has no serving peer.
    """
    # No model or the master's own model -> built-in master peer.
    if not requested_model or requested_model in (MASTER_MODEL, "qwen", "default"):
        return {"base_url": LLAMA_URL, "served_by": "master", "node_id": "master"}

    nodes = await _list_registry_nodes()
    peer = select_peer_for_model(requested_model, nodes)
    if peer:
        return {"base_url": peer["endpoint"], "served_by": peer["display_name"], "node_id": peer["node_id"]}

    # The master also serves MASTER_MODEL under loose matching.
    if _model_matches(requested_model, MASTER_MODEL):
        return {"base_url": LLAMA_URL, "served_by": "master", "node_id": "master"}

    raise LookupError(requested_model)


REDIS_TOKENS_TOTAL = "prometeu:mesh:tokens:total"
REDIS_TOKENS_PROMPT = "prometeu:mesh:tokens:prompt"
REDIS_TOKENS_COMPLETION = "prometeu:mesh:tokens:completion"
REDIS_TOKENS_REQUESTS = "prometeu:mesh:tokens:requests"


async def _credit_usage(usage: dict):
    """Best-effort: incrementa contadores Redis com tokens reportados pelo llama-server."""
    if not isinstance(usage, dict):
        return
    try:
        total = int(usage.get("total_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return
    if total <= 0 and prompt <= 0 and completion <= 0:
        return
    # Prometheus counters (process-local, sobrevive reload do gateway)
    try:
        if prompt > 0:
            METRIC_TOKENS.labels(kind="prompt").inc(prompt)
        if completion > 0:
            METRIC_TOKENS.labels(kind="completion").inc(completion)
        if total > 0:
            METRIC_TOKENS.labels(kind="total").inc(total)
        METRIC_INFERENCE_REQUESTS.inc()
    except Exception:
        pass
    try:
        r = _redis()
        pipe = r.pipeline()
        if total > 0:
            pipe.incrby(REDIS_TOKENS_TOTAL, total)
        if prompt > 0:
            pipe.incrby(REDIS_TOKENS_PROMPT, prompt)
        if completion > 0:
            pipe.incrby(REDIS_TOKENS_COMPLETION, completion)
        pipe.incr(REDIS_TOKENS_REQUESTS)
        await pipe.execute()
    except Exception as e:
        # Telemetria não pode quebrar o request
        import logging
        logging.getLogger("uvicorn.error").warning("credit_usage redis failed: %s", e)


def _extract_usage_from_sse_chunk(buf: bytes) -> dict | None:
    """Procura por 'usage' em data: {...} SSE. Retorna o último encontrado ou None."""
    last = None
    for line in buf.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        u = obj.get("usage")
        if isinstance(u, dict):
            last = u
    return last


@app.api_route("/v1/{rest:path}", methods=["GET", "POST", "OPTIONS"])
@limiter.limit(RATE_LIMIT_V1)
async def proxy_v1(rest: str, request: Request):
    body = await request.body()
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    method = request.method
    path = f"/v1/{rest}"

    # Soft reciprocity quota. Authenticated contributors get larger RPM based
    # on signed-receipt contribution; anonymous callers get the floor. This is
    # "soft" in policy terms: 429 asks caller to slow down, never swaps model or
    # hides the reason.
    identity, authenticated, pubkey = await _resolve_identity(request)
    contributed = await _contributed_tokens(pubkey) if pubkey else 0
    consumed = await _consumed_tokens(identity)
    used = await _minute_usage(identity)
    quota = recip.quota_decision(authenticated, contributed, consumed, used)
    if not quota["allow"]:
        try:
            METRIC_RECIPROCITY_SOFT_QUOTA.labels(_metric_auth_label(authenticated)).inc()
        except Exception:
            pass
        return JSONResponse(
            {"error": "soft_quota", "message": "reciprocity soft quota exceeded; contribute capacity or slow down", **quota},
            status_code=429,
            headers={"Retry-After": str(quota["retry_after_sec"]), "X-Prometeu-Standing": str(quota["standing"])},
        )

    # Route by requested model. No model -> master. Named model with no serving
    # peer -> explicit 503 (no silent fallback to whatever is loaded).
    requested_model = _extract_model_from_body(body)
    try:
        target = await _resolve_target(requested_model)
    except LookupError:
        return JSONResponse(
            {
                "error": "model_not_served",
                "message": f"No online peer is serving '{requested_model}'. "
                           f"Request a pool start via POST /api/pools/request, "
                           f"then contribute capacity by joining the pool.",
                "model": requested_model,
            },
            status_code=503,
        )
    base_url = target["base_url"]
    served_by_headers = {
        "X-Prometeu-Served-By": str(target["served_by"]),
        "X-Prometeu-Node-Id": str(target["node_id"]),
    }

    # Detecta streaming SSE
    is_stream = b'"stream":true' in body or b'"stream": true' in body

    # Pra streaming, força include_usage no body se for chat/completions ou completions
    # llama-server respeita OpenAI stream_options.include_usage; sem isso, SSE termina sem `usage`.
    if is_stream and rest in ("chat/completions", "completions"):
        try:
            obj = json.loads(body)
            so = obj.get("stream_options")
            if not isinstance(so, dict):
                so = {}
            if not so.get("include_usage"):
                so["include_usage"] = True
                obj["stream_options"] = so
                body = json.dumps(obj).encode()
        except Exception:
            pass

    if is_stream:
        async def gen():
            buf = bytearray()
            agen = _proxy_stream(method, path, body, fwd_headers, base_url)
            first = await agen.__anext__()
            assert first[0] == "HEADERS"
            async for kind, *rest_ in agen:
                if kind == "DATA":
                    chunk = rest_[0]
                    buf.extend(chunk)
                    yield chunk
            usage = _extract_usage_from_sse_chunk(bytes(buf))
            if usage:
                await _credit_usage(usage)
                try:
                    _t = int(usage.get("total_tokens") or 0)
                    await _bump_consumed(identity, _t)
                    await _bump_consumption_metrics(authenticated, _t)
                except Exception:
                    pass
        return StreamingResponse(gen(), media_type="text/event-stream", headers=served_by_headers)

    timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, f"{base_url}{path}", content=body, headers=fwd_headers)
        is_json = r.headers.get("content-type", "").startswith("application/json")
        payload = r.json() if is_json else {"raw": r.text}
        if is_json and isinstance(payload, dict):
            usage = payload.get("usage")
            if isinstance(usage, dict):
                await _credit_usage(usage)
                try:
                    _t = int(usage.get("total_tokens") or 0)
                    await _bump_consumed(identity, _t)
                    await _bump_consumption_metrics(authenticated, _t)
                except Exception:
                    pass
        return JSONResponse(content=payload, status_code=r.status_code, headers=served_by_headers)


@app.get("/api/served")
async def api_served():
    """Which models are actually being served right now, by ready-peer count.

    The master's built-in model is always listed (LLAMA_URL is a peer)."""
    nodes = await _list_registry_nodes()
    served = list_served_models(nodes)
    # Ensure the master model is represented even if the master's node-agent
    # doesn't advertise it through inference.models (legacy llama-server).
    if MASTER_MODEL not in served:
        master_ok = False
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                rr = await c.get(f"{LLAMA_URL}/health")
                master_ok = rr.status_code == 200
        except Exception:
            master_ok = False
        if master_ok:
            served[MASTER_MODEL] = {
                "model_id": MASTER_MODEL,
                "ready_peers": 1,
                "total_peers": 1,
                "node_ids": ["master"],
            }
    return {"total_models": len(served), "models": list(served.values())}


async def _peers_available_ram_mb() -> list[int]:
    """Available RAM (MB) of each online peer, for pool sizing."""
    nodes = await _list_registry_nodes()
    out: list[int] = []
    for n in nodes:
        if not n.get("online"):
            continue
        hw = n.get("hardware") or {}
        tel = hw.get("telemetry") or {}
        ram = tel.get("ram_available_mb") or hw.get("ram_available_mb")
        if ram:
            # Respect declared limits: a peer offering 50%/2048MB shouldn't be
            # counted for its full free RAM. Use min(free, limit) when present.
            limit = (n.get("limits") or {}).get("ram_mb")
            out.append(int(min(ram, limit)) if limit else int(ram))
    return out


@app.get("/api/catalog/size/{model_id:path}")
async def api_catalog_size(model_id: str, source: str = "hf", context: int = 2048):
    """Estimate RAM/peers/tok-s to run a GGUF model across the current pool.

    model_id for source=hf must be 'owner/repo/filename.gguf'.
    Includes a pool plan computed against the RAM available on online peers.
    """
    peers = await _peers_available_ram_mb()
    try:
        report = await asyncio.to_thread(
            size_model, model_id, source, context, peers or None
        )
        return report
    except NotImplementedError as e:
        return JSONResponse({"error": "unsupported_source", "message": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "sizing_failed", "message": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# Pool orchestration (Fase 4)
# ---------------------------------------------------------------------------
REDIS_POOL_PREFIX = "prometeu:pools:"
REDIS_POOL_INDEX = "prometeu:pools:index"
POOL_TTL_SEC = int(os.getenv("PROMETEU_POOL_TTL_SEC", "3600"))


def _pool_key(pool_id: str) -> str:
    return f"{REDIS_POOL_PREFIX}{pool_id}"


async def _save_pool(pool: poolmod.Pool) -> None:
    r = _redis()
    await r.set(_pool_key(pool.pool_id), json.dumps(pool.to_dict()), ex=POOL_TTL_SEC)
    await r.sadd(REDIS_POOL_INDEX, pool.pool_id)


async def _load_pool(pool_id: str) -> poolmod.Pool | None:
    r = _redis()
    raw = await r.get(_pool_key(pool_id))
    if not raw:
        return None
    try:
        return poolmod.Pool.from_dict(json.loads(raw))
    except Exception:
        return None


async def _list_pools() -> list[poolmod.Pool]:
    r = _redis()
    ids = sorted(await r.smembers(REDIS_POOL_INDEX))
    if not ids:
        return []
    vals = await r.mget([_pool_key(p) for p in ids])
    out, stale = [], []
    for pid, raw in zip(ids, vals):
        if raw is None:
            stale.append(pid)
            continue
        try:
            out.append(poolmod.Pool.from_dict(json.loads(raw)))
        except Exception:
            stale.append(pid)
    if stale:
        await r.srem(REDIS_POOL_INDEX, *stale)
    return out


async def _ready_node_ids_for(model_id: str) -> set[str]:
    """node_ids whose heartbeat reports `model_id` as ready."""
    nodes = await _list_registry_nodes()
    ready: set[str] = set()
    for n in nodes:
        if not n.get("online"):
            continue
        inf = n.get("inference") or {}
        for m in inf.get("models", []):
            if m.get("ready") and _model_matches(model_id, m.get("model_id", "")):
                ready.add(n.get("node_id"))
    return ready


async def _node_inference_endpoint(node_id: str) -> str | None:
    """Resolve a peer's node-agent control endpoint (for load/unload)."""
    nodes = await _list_registry_nodes()
    for n in nodes:
        if n.get("node_id") == node_id:
            # node-agent exposes its API on its dashboard host:8787
            host = (n.get("hardware") or {}).get("host") or n.get("host")
            dash = n.get("dashboard_url")
            if dash:
                return dash.rstrip("/")
            if host:
                return f"http://{host}:8787"
    return None


async def _instruct_peer_load(node_id: str, pool: poolmod.Pool) -> bool:
    ep = await _node_inference_endpoint(node_id)
    if not ep or not pool.gguf_url or not pool.sha256:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{ep}/api/node/load", json={
                "model_id": pool.model_id,
                "gguf_url": pool.gguf_url,
                "sha256": pool.sha256,
                "ctx_size": pool.context,
            })
            return r.status_code < 400
    except Exception:
        return False


async def _instruct_peer_unload(node_id: str, model_id: str) -> bool:
    ep = await _node_inference_endpoint(node_id)
    if not ep:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{ep}/api/node/unload", json={"model_id": model_id})
            return r.status_code < 400
    except Exception:
        return False


class PoolRequest(BaseModel):
    model_id: str
    source: str = "hf"
    context: int = 2048
    sha256: str | None = None


@app.post("/api/pools/request")
async def api_pool_request(req: PoolRequest):
    """Request that a model be served by a pool. Sizes the model, picks peers
    with enough RAM, instructs them to load, and tracks warming to quorum."""
    # 0. Allowlist gate (Fase 6 hardening). Only curated, hash-pinned models may
    # be loaded — primary defense against poisoned-model attacks. No fallback:
    # off-list or sha-mismatch is a hard 403.
    gate = allowmod.check_model(req.model_id, req.source, req.sha256)
    if not gate["allowed"]:
        return JSONResponse(
            {"error": "model_not_allowed", "message": gate["reason"], "model": req.model_id},
            status_code=403,
        )
    effective_sha = gate["sha256"]

    # 1. Size the model against current peer RAM to get min_peers + per-peer RAM.
    peers_ram = await _peers_available_ram_mb()
    try:
        report = await asyncio.to_thread(size_model, req.model_id, req.source, req.context, peers_ram or None)
    except NotImplementedError as e:
        return JSONResponse({"error": "unsupported_source", "message": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "sizing_failed", "message": str(e)}, status_code=502)

    plan = report.get("pool") or {}
    min_peers = int(plan.get("min_peers") or 1)
    ram_per_peer = int(plan.get("ram_per_peer_mb") or report["ram"]["total_ram_mb"])

    pool_id = poolmod.make_pool_id(req.model_id, req.source)
    existing = await _load_pool(pool_id)
    if existing and existing.state not in poolmod.TERMINAL:
        return {"pool": existing.to_dict(), "note": "pool already active"}

    pool = poolmod.Pool(
        pool_id=pool_id, model_id=req.model_id, source=req.source,
        context=req.context, min_peers=min_peers,
        gguf_url=report.get("url"), sha256=effective_sha,
        ram_per_peer_mb=ram_per_peer, state=poolmod.REQUESTED,
    )

    # 2. Pick candidates and instruct them to load.
    nodes = await _list_registry_nodes()
    cands = poolmod.select_warm_candidates(nodes, ram_per_peer, min_peers)
    if len(cands) < min_peers:
        pool.state = poolmod.FAILED
        pool.last_error = f"only {len(cands)} peers meet {ram_per_peer}MB RAM; need {min_peers}"
        await _save_pool(pool)
        return JSONResponse({"pool": pool.to_dict(), "error": "insufficient_capacity"}, status_code=409)

    pool.members = [n["node_id"] for n in cands]
    pool.state = poolmod.WARMING
    await _save_pool(pool)

    if pool.sha256:
        results = await asyncio.gather(*[_instruct_peer_load(nid, pool) for nid in pool.members])
        if not any(results):
            pool.last_error = "no peer accepted the load instruction"
    else:
        pool.last_error = "no sha256 provided; peers will not load until one is supplied (no unverified downloads)"
    await _save_pool(pool)
    return {"pool": pool.to_dict(), "sizing": report}


@app.get("/api/pools")
async def api_pools_list():
    pools = await _list_pools()
    # Reconcile each against live readiness before returning.
    out = []
    for p in pools:
        ready = await _ready_node_ids_for(p.model_id)
        poolmod.reconcile(p, ready)
        await _save_pool(p)
        out.append(p.to_dict())
    _update_pool_metrics(pools)
    return {"total": len(out), "pools": out}


@app.get("/api/pools/{pool_id}")
async def api_pool_get(pool_id: str):
    p = await _load_pool(pool_id)
    if not p:
        return JSONResponse({"error": "not_found", "pool_id": pool_id}, status_code=404)
    ready = await _ready_node_ids_for(p.model_id)
    poolmod.reconcile(p, ready)
    await _save_pool(p)
    return p.to_dict()


@app.post("/api/pools/{pool_id}/stop")
async def api_pool_stop(pool_id: str):
    p = await _load_pool(pool_id)
    if not p:
        return JSONResponse({"error": "not_found", "pool_id": pool_id}, status_code=404)
    p.state = poolmod.DRAINING
    p.updated_at = time.time()
    await _save_pool(p)
    await asyncio.gather(*[_instruct_peer_unload(nid, p.model_id) for nid in p.members])
    ready = await _ready_node_ids_for(p.model_id)
    poolmod.reconcile(p, ready)
    await _save_pool(p)
    return p.to_dict()


# ---------------------------------------------------------------------------
# Reciprocity & signed-challenge auth (Fase 5)
# ---------------------------------------------------------------------------
REDIS_CHALLENGE_PREFIX = "prometeu:auth:challenge:"
REDIS_TOKEN_PREFIX = "prometeu:auth:token:"
REDIS_CONSUMED_PREFIX = "prometeu:recip:consumed:"   # per-pubkey consumed tokens
REDIS_RPM_PREFIX = "prometeu:recip:rpm:"             # per-identity minute bucket


async def _contributed_tokens(node_pubkey: str) -> int:
    """Tokens this identity has served, summed from the signed-receipt ledger.

    Receipts are keyed by node_id; we map pubkey->node_id via the registry.
    """
    r = _redis()
    nodes = await _list_registry_nodes()
    total = 0
    for n in nodes:
        if n.get("public_key") == node_pubkey:
            raw = await r.hget(_receipts_key(n["node_id"]), "tokens_served")
            if raw:
                try:
                    total += int(raw)
                except (TypeError, ValueError):
                    pass
    return total


async def _consumed_tokens(identity: str) -> int:
    r = _redis()
    raw = await r.get(f"{REDIS_CONSUMED_PREFIX}{identity}")
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def _bump_consumed(identity: str, tokens: int) -> None:
    if tokens <= 0:
        return
    r = _redis()
    await r.incrby(f"{REDIS_CONSUMED_PREFIX}{identity}", tokens)


async def _minute_usage(identity: str) -> int:
    r = _redis()
    bucket = int(time.time() // 60)
    key = f"{REDIS_RPM_PREFIX}{identity}:{bucket}"
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, 120)
    return int(n)


async def _resolve_identity(request: Request) -> tuple[str, bool, str | None]:
    """Returns (identity, authenticated, public_key).

    Bearer token (from /api/auth/verify) -> authenticated identity = pubkey.
    Otherwise anonymous, identity = client IP.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        r = _redis()
        pk = await r.get(f"{REDIS_TOKEN_PREFIX}{token}")
        if pk:
            pk = pk.decode() if isinstance(pk, bytes) else pk
            return (pk, True, pk)
    return (get_remote_address(request), False, None)


class ChallengeRequest(BaseModel):
    public_key: str


class VerifyRequest(BaseModel):
    public_key: str
    nonce: str
    signature: str


@app.post("/api/auth/challenge")
async def api_auth_challenge(req: ChallengeRequest):
    """Issue a one-time nonce the caller must sign with its Ed25519 key."""
    nonce = recip.make_nonce()
    r = _redis()
    await r.set(f"{REDIS_CHALLENGE_PREFIX}{req.public_key}", nonce, ex=recip.CHALLENGE_TTL_SEC)
    return {"nonce": nonce, "ttl_sec": recip.CHALLENGE_TTL_SEC}


@app.post("/api/auth/verify")
async def api_auth_verify(req: VerifyRequest):
    """Verify the signed nonce; on success issue a short-lived bearer token."""
    r = _redis()
    stored = await r.get(f"{REDIS_CHALLENGE_PREFIX}{req.public_key}")
    if not stored:
        return JSONResponse({"error": "no_challenge", "message": "request a challenge first"}, status_code=400)
    stored = stored.decode() if isinstance(stored, bytes) else stored
    if stored != req.nonce:
        return JSONResponse({"error": "nonce_mismatch"}, status_code=400)
    try:
        ok = recip.verify_signature(req.public_key, req.nonce, req.signature)
    except RuntimeError as e:
        return JSONResponse({"error": "auth_unavailable", "message": str(e)}, status_code=503)
    if not ok:
        return JSONResponse({"error": "bad_signature"}, status_code=401)
    # Single-use: delete the challenge.
    await r.delete(f"{REDIS_CHALLENGE_PREFIX}{req.public_key}")
    token = recip.make_token()
    await r.set(f"{REDIS_TOKEN_PREFIX}{token}", req.public_key, ex=recip.TOKEN_TTL_SEC)
    return {"token": token, "token_type": "bearer", "expires_in": recip.TOKEN_TTL_SEC}


@app.get("/api/reciprocity/standing")
async def api_reciprocity_standing(request: Request):
    identity, authed, pk = await _resolve_identity(request)
    contributed = await _contributed_tokens(pk) if pk else 0
    consumed = await _consumed_tokens(identity)
    used = 0  # don't consume a minute slot just for a status check
    decision = recip.quota_decision(authed, contributed, consumed, used)
    decision.update({"identity": identity if authed else "anonymous",
                     "contributed_tokens": contributed,
                     "consumed_tokens": consumed})
    return decision


@app.get("/api/catalog/allowlist")
async def api_catalog_allowlist():
    """Curated models a client may request via /api/pools/request.

    Public discovery endpoint. Exposes model_id, display name, size, ctx and
    whether the sha256 is pinned (verified) — but lets the operator keep the
    file as the single source of truth.
    """
    data = allowmod.load_allowlist()
    models = [
        {
            "model_id": m.get("model_id"),
            "source": m.get("source", "hf"),
            "display_name": m.get("display_name"),
            "params_b": m.get("params_b"),
            "ctx_max": m.get("ctx_max"),
            "pinned": bool((m.get("sha256") or "").strip()),
        }
        for m in data.get("models", [])
    ]
    return {"schema": data.get("schema", "prometeu/allowlist/1"),
            "updated_at": data.get("updated_at"),
            "models": models, "total": len(models)}


# Frontend estático
app.mount("/", StaticFiles(directory=str(_find_web_dir()), html=True), name="web")
