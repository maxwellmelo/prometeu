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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


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

node_status_cache: dict[str, Any] = {"checked_at": 0, "nodes": []}


async def refresh_nodes() -> list[dict]:
    """
    llama-server (master) tem RPC ativo com workers — se /health=ok, todos os nós
    estão produtivos por definição (RPC é sincronia, falha de qualquer worker
    mataria o servidor inteiro). Probe TCP direto dos workers é inviável porque
    rpc-server tem backlog=1 e enfileira conexões durante inferência ativa.
    """
    master_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{LLAMA_URL}/health")
            master_ok = r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        master_ok = False

    out = []
    for n in NODES:
        out.append({
            "id": n["id"],
            "host": n["host"],
            "port": n["port"],
            "role": n["role"],
            "alive": master_ok,
        })
    node_status_cache["nodes"] = out
    node_status_cache["checked_at"] = time.time()
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
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
