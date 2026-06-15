"""
Prometeu public node daemon.

This is the participant-facing daemon for Sprint 2A. It does not yet serve
public inference. It detects local capacity, exposes a local dashboard on :8787,
and registers/heartbeats with a Prometeu coordinator so the public pool can be
observed before we enable layer assignment over an overlay network.
"""
import asyncio
import json
import os
import platform
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import psutil
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = "0.2.0"
DEFAULT_CONFIG_PATH = Path(os.getenv("PROMETEU_NODE_CONFIG", "/etc/prometeu-node/config.json"))
DEFAULT_WEB_DIR = Path(os.getenv("PROMETEU_NODE_WEB_DIR", "/opt/prometeu-node/web"))


def _machine_id() -> str:
    for p in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            v = p.read_text().strip()
            if v:
                return v
        except Exception:
            pass
    return str(uuid.getnode())


def default_config() -> dict[str, Any]:
    node_id = "pn-" + _machine_id()[:12]
    return {
        "node_id": node_id,
        "display_name": socket.gethostname(),
        "mode": "public",
        "coordinator_url": "https://prometeu.mx3dev.com",
        "models": ["qwen2.5-1.5b-q4"],
        "limits": {
            "cpu_percent": 50,
            "ram_mb": 1024,
            "bandwidth_mbps": 20,
        },
        "schedule": {
            "enabled": False,
            "start": "00:00",
            "end": "06:00",
        },
        "status": "available",
        "rpc_endpoint": None,
        "dashboard_url": None,
        "heartbeat_sec": 15,
    }


def load_config() -> dict[str, Any]:
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_CONFIG_PATH.exists():
        DEFAULT_CONFIG_PATH.write_text(json.dumps(default_config(), indent=2) + "\n")
    cfg = default_config()
    cfg.update(json.loads(DEFAULT_CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def hardware() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_freq = psutil.cpu_freq()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_freq_mhz": round(cpu_freq.current, 1) if cpu_freq else None,
        "ram_total_mb": round(vm.total / 1024 / 1024),
        "ram_available_mb": round(vm.available / 1024 / 1024),
        "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
    }


def telemetry() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        "ram_used_mb": round(vm.used / 1024 / 1024),
        "ram_available_mb": round(vm.available / 1024 / 1024),
        "net_total_rx_mb": round(net.bytes_recv / 1024 / 1024, 1),
        "net_total_tx_mb": round(net.bytes_sent / 1024 / 1024, 1),
        "time": time.time(),
    }


def payload() -> dict[str, Any]:
    cfg = load_config()
    return {
        "node_id": cfg["node_id"],
        "display_name": cfg.get("display_name") or socket.gethostname(),
        "version": VERSION,
        "public_key": cfg.get("public_key"),
        "mode": cfg.get("mode", "public"),
        "models": cfg.get("models", []),
        "limits": cfg.get("limits", {}),
        "hardware": {**hardware(), "telemetry": telemetry()},
        "status": cfg.get("status", "available"),
        "dashboard_url": cfg.get("dashboard_url"),
        "rpc_endpoint": cfg.get("rpc_endpoint"),
    }


last_heartbeat: dict[str, Any] = {"ok": False, "at": 0, "err": None}


async def send_heartbeat(endpoint: str = "/api/registry/heartbeat") -> dict[str, Any]:
    cfg = load_config()
    url = cfg["coordinator_url"].rstrip("/") + endpoint
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(url, json=payload())
        r.raise_for_status()
        return r.json()


@asynccontextmanager
async def lifespan(app: FastAPI):
    psutil.cpu_percent(interval=None)
    try:
        last_heartbeat.update({"ok": True, "at": time.time(), "err": None, "result": await send_heartbeat("/api/registry/join")})
    except Exception as e:
        last_heartbeat.update({"ok": False, "at": time.time(), "err": str(e)})

    async def loop():
        while True:
            cfg = load_config()
            await asyncio.sleep(float(cfg.get("heartbeat_sec", 15)))
            try:
                result = await send_heartbeat()
                last_heartbeat.update({"ok": True, "at": time.time(), "err": None, "result": result})
            except Exception as e:
                last_heartbeat.update({"ok": False, "at": time.time(), "err": str(e)})

    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="Prometeu Node", lifespan=lifespan)


class ConfigUpdate(BaseModel):
    display_name: str | None = None
    mode: str | None = None
    models: list[str] | None = None
    limits: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    status: str | None = None
    coordinator_url: str | None = None


@app.get("/api/status")
def api_status():
    cfg = load_config()
    return {
        "version": VERSION,
        "config": cfg,
        "hardware": hardware(),
        "telemetry": telemetry(),
        "heartbeat": last_heartbeat,
    }


@app.post("/api/config")
def api_config(update: ConfigUpdate):
    cfg = load_config()
    data = update.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            cfg[k] = v
    save_config(cfg)
    return {"ok": True, "config": cfg}


@app.post("/api/heartbeat")
async def api_heartbeat():
    try:
        result = await send_heartbeat()
        last_heartbeat.update({"ok": True, "at": time.time(), "err": None, "result": result})
        return last_heartbeat
    except Exception as e:
        last_heartbeat.update({"ok": False, "at": time.time(), "err": str(e)})
        return JSONResponse(last_heartbeat, status_code=502)


if DEFAULT_WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(DEFAULT_WEB_DIR), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    def index():
        return "<h1>Prometeu Node</h1><p>Web assets missing.</p>"
