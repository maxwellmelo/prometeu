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
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


LLAMA_URL = os.getenv("PROMETEU_LLAMA_URL", "http://127.0.0.1:8080")


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
REGISTRY_DB = Path(os.getenv("PROMETEU_REGISTRY_DB", "/opt/prometeu/registry.db"))
REGISTRY_TTL_SEC = int(os.getenv("PROMETEU_REGISTRY_TTL_SEC", "120"))

node_status_cache: dict[str, Any] = {"checked_at": 0, "nodes": []}


class NodeJoin(BaseModel):
    node_id: str = Field(min_length=3, max_length=128)
    display_name: str | None = None
    version: str = "unknown"
    public_key: str | None = None
    mode: str = "public"
    models: list[str] = []
    limits: dict[str, Any] = {}
    hardware: dict[str, Any] = {}
    status: str = "available"
    dashboard_url: str | None = None
    rpc_endpoint: str | None = None


class NodeHeartbeat(NodeJoin):
    pass


def _db() -> sqlite3.Connection:
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(REGISTRY_DB)
    con.row_factory = sqlite3.Row
    return con


def init_registry() -> None:
    with _db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS public_nodes (
                node_id TEXT PRIMARY KEY,
                display_name TEXT,
                version TEXT,
                public_key TEXT,
                mode TEXT,
                models_json TEXT,
                limits_json TEXT,
                hardware_json TEXT,
                status TEXT,
                dashboard_url TEXT,
                rpc_endpoint TEXT,
                first_seen REAL,
                last_seen REAL
            )
            """
        )
        con.commit()


def _upsert_node(payload: NodeJoin) -> dict[str, Any]:
    now = time.time()
    with _db() as con:
        old = con.execute("SELECT first_seen FROM public_nodes WHERE node_id=?", (payload.node_id,)).fetchone()
        first_seen = float(old["first_seen"]) if old else now
        con.execute(
            """
            INSERT INTO public_nodes (
                node_id, display_name, version, public_key, mode, models_json,
                limits_json, hardware_json, status, dashboard_url, rpc_endpoint,
                first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                display_name=excluded.display_name,
                version=excluded.version,
                public_key=excluded.public_key,
                mode=excluded.mode,
                models_json=excluded.models_json,
                limits_json=excluded.limits_json,
                hardware_json=excluded.hardware_json,
                status=excluded.status,
                dashboard_url=excluded.dashboard_url,
                rpc_endpoint=excluded.rpc_endpoint,
                last_seen=excluded.last_seen
            """,
            (
                payload.node_id, payload.display_name, payload.version,
                payload.public_key, payload.mode, json.dumps(payload.models),
                json.dumps(payload.limits), json.dumps(payload.hardware),
                payload.status, payload.dashboard_url, payload.rpc_endpoint,
                first_seen, now,
            ),
        )
        con.commit()
    return {"ok": True, "node_id": payload.node_id, "first_seen": first_seen, "last_seen": now}


def _rows_to_nodes(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    now = time.time()
    nodes = []
    for r in rows:
        last_seen = float(r["last_seen"] or 0)
        nodes.append({
            "node_id": r["node_id"],
            "display_name": r["display_name"],
            "version": r["version"],
            "mode": r["mode"],
            "models": json.loads(r["models_json"] or "[]"),
            "limits": json.loads(r["limits_json"] or "{}"),
            "hardware": json.loads(r["hardware_json"] or "{}"),
            "status": r["status"],
            "dashboard_url": r["dashboard_url"],
            "rpc_endpoint": r["rpc_endpoint"],
            "first_seen": r["first_seen"],
            "last_seen": last_seen,
            "age_sec": round(now - last_seen, 1),
            "online": (now - last_seen) <= REGISTRY_TTL_SEC,
        })
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
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_registry()
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
    return _upsert_node(payload)


@app.post("/api/registry/heartbeat")
async def registry_heartbeat(payload: NodeHeartbeat):
    return _upsert_node(payload)


@app.post("/api/registry/leave")
async def registry_leave(payload: dict[str, Any]):
    node_id = str(payload.get("node_id", ""))
    if not node_id:
        return JSONResponse({"ok": False, "error": "node_id required"}, status_code=400)
    with _db() as con:
        con.execute("UPDATE public_nodes SET status=?, last_seen=? WHERE node_id=?", ("offline", time.time(), node_id))
        con.commit()
    return {"ok": True, "node_id": node_id}


@app.get("/api/registry/nodes")
async def registry_nodes():
    init_registry()
    with _db() as con:
        rows = con.execute("SELECT * FROM public_nodes ORDER BY last_seen DESC").fetchall()
    nodes = _rows_to_nodes(rows)
    return {
        "nodes": nodes,
        "total": len(nodes),
        "online": sum(1 for n in nodes if n["online"] and n["status"] != "offline"),
        "ttl_sec": REGISTRY_TTL_SEC,
    }


# Proxy genérico /v1/* pra llama-server (OpenAI-compatible)
async def _proxy_stream(method: str, path: str, body: bytes, headers: dict):
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(method, f"{LLAMA_URL}{path}", content=body, headers=headers) as r:
            yield ("HEADERS", r.status_code, dict(r.headers))
            async for chunk in r.aiter_bytes():
                yield ("DATA", chunk)


@app.api_route("/v1/{rest:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy_v1(rest: str, request: Request):
    body = await request.body()
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    method = request.method
    path = f"/v1/{rest}"

    # Detecta streaming SSE
    is_stream = b'"stream":true' in body or b'"stream": true' in body

    if is_stream:
        async def gen():
            agen = _proxy_stream(method, path, body, fwd_headers)
            first = await agen.__anext__()
            assert first[0] == "HEADERS"
            async for kind, *rest_ in agen:
                if kind == "DATA":
                    yield rest_[0]
        return StreamingResponse(gen(), media_type="text/event-stream")

    timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, f"{LLAMA_URL}{path}", content=body, headers=fwd_headers)
        is_json = r.headers.get("content-type", "").startswith("application/json")
        return JSONResponse(
            content=r.json() if is_json else {"raw": r.text},
            status_code=r.status_code,
        )


# Frontend estático
app.mount("/", StaticFiles(directory=str(_find_web_dir()), html=True), name="web")
