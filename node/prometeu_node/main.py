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
import re
import shutil
import socket
import subprocess
import threading
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

try:
    from prometeu_node import inference
except ImportError:  # flat layout fallback (prod deploy)
    import inference  # type: ignore

VERSION = "0.6.0"
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
        # active_model: the specific LLM this node is currently hosting/serving.
        # Used by the coordinator to rank LLMs in /api/catalog/active. Should be
        # one of the entries in `models`. Settable via /api/config or dashboard.
        "active_model": "qwen2.5-1.5b-q4",
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



def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except Exception:
        return None


def detect_gpus() -> list[dict[str, Any]]:
    """Best-effort GPU/VRAM detection. No silent fake values.

    Order:
    1. NVIDIA via nvidia-smi (accurate VRAM, no Python deps).
    2. AMD via rocm-smi (accurate VRAM when ROCm present).
    3. DRM/sysfs presence scan (vendor/model only; VRAM unknown).
    """
    gpus: list[dict[str, Any]] = []

    if shutil.which("nvidia-smi"):
        out = _run([
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version,compute_cap",
            "--format=csv,noheader,nounits",
        ])
        if out:
            for idx, line in enumerate(out.splitlines()):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    gpus.append({
                        "index": idx,
                        "vendor": "nvidia",
                        "model": parts[0],
                        "vram_mb": int(float(parts[1])) if parts[1].replace(".", "", 1).isdigit() else None,
                        "driver": parts[2] if len(parts) > 2 else None,
                        "compute_capability": parts[3] if len(parts) > 3 else None,
                        "method": "nvidia-smi",
                    })
            if gpus:
                return gpus

    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram"], timeout=4.0)
        if out:
            cards: dict[int, dict[str, Any]] = {}
            for line in out.splitlines():
                m = re.search(r"GPU\[(\d+)\].*?:\s*(.*)$", line)
                if not m:
                    continue
                idx = int(m.group(1)); val = m.group(2).strip()
                card = cards.setdefault(idx, {"index": idx, "vendor": "amd", "model": None, "vram_mb": None, "method": "rocm-smi"})
                if "Card series" in line or "Card model" in line:
                    card["model"] = val
                mm = re.search(r"([0-9]+)\s*(?:MiB|MB)", val)
                if mm and ("Total" in line or "VRAM" in line):
                    card["vram_mb"] = int(mm.group(1))
            if cards:
                return list(cards.values())

    vendor_map = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        dev = card / "device"
        vendor = (dev / "vendor").read_text().strip().lower() if (dev / "vendor").exists() else None
        if not vendor:
            continue
        # render-only integrated GPUs are still useful to report, but VRAM is unknown.
        model = None
        for name_file in (dev / "product", dev / "subsystem_device"):
            if name_file.exists():
                try:
                    model = name_file.read_text().strip()
                    break
                except Exception:
                    pass
        gpus.append({
            "index": len(gpus),
            "vendor": vendor_map.get(vendor, vendor),
            "model": model or card.name,
            "vram_mb": None,
            "driver": None,
            "compute_capability": None,
            "method": "drm-sysfs",
        })
    return gpus



def apply_resource_limits() -> dict[str, Any]:
    script = shutil.which("prometeu-node-apply-limits")
    if not script:
        return {"ok": False, "err": "prometeu-node-apply-limits not installed; resource limits not enforced"}
    try:
        out = subprocess.check_output([script], text=True, stderr=subprocess.STDOUT, timeout=10)
        return {"ok": True, "output": out.strip()}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "err": e.output.strip() or str(e)}
    except Exception as e:
        return {"ok": False, "err": str(e)}

def resource_limit_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    limits = cfg.get("limits", {})
    wanted_cpu = limits.get("cpu_percent")
    wanted_ram = limits.get("ram_mb")
    svc = os.getenv("PROMETEU_NODE_SYSTEMD_UNIT", "prometeu-node.service")
    status = {"unit": svc, "wanted": {"cpu_percent": wanted_cpu, "ram_mb": wanted_ram}, "applied": False, "method": "systemd", "err": None}
    if not shutil.which("systemctl"):
        status.update({"method": None, "err": "systemctl not found; resource limits not enforced"})
        return status
    out = _run(["systemctl", "show", svc, "-p", "CPUQuotaPerSecUSec", "-p", "MemoryMax"], timeout=2.0)
    if out is None:
        status["err"] = f"cannot inspect {svc}; resource limits not enforced"
        return status
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    status["current"] = props
    cpu_us = props.get("CPUQuotaPerSecUSec", "infinity")
    mem = props.get("MemoryMax", "infinity")
    cpu_ok = cpu_us != "infinity" if wanted_cpu else True
    mem_ok = mem != "infinity" if wanted_ram else True
    status["applied"] = bool(cpu_ok and mem_ok)
    if not status["applied"]:
        status["err"] = "limits configured but not enforced; run /usr/local/bin/prometeu-node-apply-limits or reinstall node"
    return status

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
        "gpus": detect_gpus(),
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
        "active_model": cfg.get("active_model") or (cfg.get("models") or [None])[0],
        "limits": cfg.get("limits", {}),
        "hardware": {**hardware(), "telemetry": telemetry()},
        "status": cfg.get("status", "available"),
        # Advertise a control endpoint the coordinator can reach for load/unload.
        # Falls back to this node's LAN IP on the agent port so a fresh install
        # is reachable without manual config (no silent localhost-only bug).
        "dashboard_url": cfg.get("dashboard_url") or f"http://{_self_lan_ip()}:{cfg.get('agent_port', 8787)}",
        "rpc_endpoint": cfg.get("rpc_endpoint"),
        "inference": _inference_summary(),
    }


def _inference_summary() -> dict[str, Any]:
    """Compact view of locally-served models for the heartbeat."""
    try:
        loaded = inference.list_models()
    except Exception as e:
        return {"serving": False, "models": [], "err": str(e)}
    served = []
    for mid, m in loaded.items():
        served.append({
            "model_id": mid,
            "port": m.get("port"),
            "ready": m.get("ready"),
            "endpoint": f"http://{_self_lan_ip()}:{m.get('port')}",
        })
    return {"serving": any(s["ready"] for s in served), "models": served}


def _self_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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
    active_model: str | None = None
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
        "resource_limits": resource_limit_status(cfg),
    }


@app.post("/api/config")
def api_config(update: ConfigUpdate):
    cfg = load_config()
    data = update.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            cfg[k] = v
    save_config(cfg)
    limit_apply = apply_resource_limits() if "limits" in data else None
    return {"ok": True, "config": cfg, "limit_apply": limit_apply}


@app.post("/api/heartbeat")
async def api_heartbeat():
    try:
        result = await send_heartbeat()
        last_heartbeat.update({"ok": True, "at": time.time(), "err": None, "result": result})
        return last_heartbeat
    except Exception as e:
        last_heartbeat.update({"ok": False, "at": time.time(), "err": str(e)})
        return JSONResponse(last_heartbeat, status_code=502)


class LoadRequest(BaseModel):
    model_id: str
    gguf_url: str
    sha256: str
    ctx_size: int = 2048
    rpc_peers: list[str] | None = None
    cpu_quota: int | None = None
    mem_mb: int | None = None


class UnloadRequest(BaseModel):
    model_id: str


@app.get("/api/node/preflight")
def api_node_preflight():
    return inference.sandbox_preflight()


@app.get("/api/node/models")
def api_node_models():
    return {"models": inference.list_models()}


@app.post("/api/node/load")
def api_node_load(req: LoadRequest):
    cfg = load_config()
    limits = cfg.get("limits", {})
    cpu = req.cpu_quota if req.cpu_quota is not None else limits.get("cpu_percent", 50)
    mem = req.mem_mb if req.mem_mb is not None else limits.get("ram_mb", 1024)

    # Loading a model means downloading a (possibly large) GGUF + a cold start
    # that can take well over a coordinator's request timeout. Run it in a
    # background thread and return 202 immediately; the coordinator learns the
    # node is READY from the heartbeat's inference summary, not this response.
    def _bg_load():
        try:
            result = inference.load_model(
                model_id=req.model_id,
                gguf_url=req.gguf_url,
                sha256=req.sha256,
                ctx_size=req.ctx_size,
                rpc_peers=req.rpc_peers,
                cpu_quota=int(cpu),
                mem_mb=int(mem),
            )
            if result.get("ok"):
                c = load_config()
                c["active_model"] = req.model_id
                models = set(c.get("models", []))
                models.add(req.model_id)
                c["models"] = sorted(models)
                save_config(c)
        except Exception as e:  # pragma: no cover - logged via stderr
            print(f"[prometeu-node] background load failed for {req.model_id}: {e}", flush=True)

    threading.Thread(target=_bg_load, name="prometeu-load", daemon=True).start()
    return JSONResponse({"ok": True, "status": "loading", "model_id": req.model_id}, status_code=202)


@app.post("/api/node/unload")
def api_node_unload(req: UnloadRequest):
    try:
        return inference.unload_model(req.model_id)
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


if DEFAULT_WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(DEFAULT_WEB_DIR), html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse)
    def index():
        return "<h1>Prometeu Node</h1><p>Web assets missing.</p>"
