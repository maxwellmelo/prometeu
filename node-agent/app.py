"""
Prometeu Node Agent

Lightweight telemetry agent for each node participating in a Prometeu cluster.
It exposes CPU/RAM/network/process metrics so the gateway can prove that
inference work is really hitting worker nodes during requests.

Env vars:
  PROMETEU_NODE_ID           stable id (default: hostname)
  PROMETEU_NODE_ROLE         master|worker (default: worker)
  PROMETEU_NODE_MODEL        served model label
  PROMETEU_NODE_LAYERS       human layer assignment, e.g. "9-18"
  PROMETEU_RPC_PORT          rpc-server port (default: 50052)
  PROMETEU_PROCESS_NAME      process to track (default: rpc-server)
  PROMETEU_SAMPLE_INTERVAL   seconds between samples (default: 1)
"""
import os
import socket
import time
from contextlib import asynccontextmanager
from typing import Any

import psutil
from fastapi import FastAPI

NODE_ID = os.getenv("PROMETEU_NODE_ID") or socket.gethostname()
ROLE = os.getenv("PROMETEU_NODE_ROLE", "worker")
MODEL = os.getenv("PROMETEU_NODE_MODEL", "unknown")
LAYERS = os.getenv("PROMETEU_NODE_LAYERS", "unknown")
RPC_PORT = int(os.getenv("PROMETEU_RPC_PORT", "50052"))
PROCESS_NAME = os.getenv("PROMETEU_PROCESS_NAME", "rpc-server")
SAMPLE_INTERVAL = float(os.getenv("PROMETEU_SAMPLE_INTERVAL", "1"))

_boot_time = time.time()
_prev_net = None
_prev_t = None
_last = {
    "rx_bps": 0.0,
    "tx_bps": 0.0,
    "rx_mbps": 0.0,
    "tx_mbps": 0.0,
}


def _iface_bytes() -> tuple[int, int]:
    """Return aggregate non-loopback bytes recv/sent."""
    rx = tx = 0
    for name, stats in psutil.net_io_counters(pernic=True).items():
        if name == "lo" or name.startswith("docker") or name.startswith("veth"):
            continue
        rx += stats.bytes_recv
        tx += stats.bytes_sent
    return rx, tx


def _processes() -> list[dict[str, Any]]:
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_info", "create_time"]):
        try:
            info = p.info
            cmd = " ".join(info.get("cmdline") or [])
            name = info.get("name") or ""
            if PROCESS_NAME not in name and PROCESS_NAME not in cmd:
                continue
            mem = info.get("memory_info")
            out.append({
                "pid": info["pid"],
                "name": name,
                "cmdline": cmd[:500],
                "cpu_percent": info.get("cpu_percent") or 0.0,
                "rss_mb": round((mem.rss if mem else 0) / 1024 / 1024, 1),
                "uptime_sec": round(time.time() - (info.get("create_time") or time.time()), 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _tcp_connections() -> dict[str, int]:
    listening = established = 0
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == RPC_PORT:
                if c.status == psutil.CONN_LISTEN:
                    listening += 1
                elif c.status == psutil.CONN_ESTABLISHED:
                    established += 1
    except psutil.AccessDenied:
        pass
    return {"listening": listening, "established": established}


def collect() -> dict[str, Any]:
    global _prev_net, _prev_t, _last

    now = time.time()
    rx, tx = _iface_bytes()
    if _prev_net is not None and _prev_t is not None:
        dt = max(now - _prev_t, 0.001)
        rx_bps = max((rx - _prev_net[0]) / dt, 0)
        tx_bps = max((tx - _prev_net[1]) / dt, 0)
        _last = {
            "rx_bps": round(rx_bps, 1),
            "tx_bps": round(tx_bps, 1),
            "rx_mbps": round((rx_bps * 8) / 1_000_000, 3),
            "tx_mbps": round((tx_bps * 8) / 1_000_000, 3),
        }
    _prev_net = (rx, tx)
    _prev_t = now

    vm = psutil.virtual_memory()
    procs = _processes()
    conns = _tcp_connections()
    cpu = psutil.cpu_percent(interval=None)

    # Heuristic: node active if rpc-server process exists and CPU or network is non-trivial.
    active_now = bool(procs) and (cpu >= 15 or _last["rx_mbps"] >= 0.5 or _last["tx_mbps"] >= 0.5)

    return {
        "node_id": NODE_ID,
        "hostname": socket.gethostname(),
        "role": ROLE,
        "model": MODEL,
        "layers": LAYERS,
        "rpc_port": RPC_PORT,
        "process_name": PROCESS_NAME,
        "process_alive": bool(procs),
        "processes": procs,
        "tcp": conns,
        "cpu_percent": cpu,
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_total_mb": round(vm.total / 1024 / 1024, 1),
        "ram_used_mb": round(vm.used / 1024 / 1024, 1),
        "ram_percent": vm.percent,
        "net_total_rx_mb": round(rx / 1024 / 1024, 1),
        "net_total_tx_mb": round(tx / 1024 / 1024, 1),
        **_last,
        "active_now": active_now,
        "uptime_sec": round(now - _boot_time, 1),
        "checked_at": now,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # prime cpu/net baselines
    psutil.cpu_percent(interval=None)
    collect()
    yield


app = FastAPI(title="Prometeu Node Agent", lifespan=lifespan)


@app.get("/status")
def status():
    return collect()


@app.get("/health")
def health():
    d = collect()
    return {"status": "ok" if d["process_alive"] else "degraded", "node_id": NODE_ID}


@app.get("/metrics")
def metrics():
    d = collect()
    lines = [
        f'prometeu_node_cpu_percent{{node="{NODE_ID}"}} {d["cpu_percent"]}',
        f'prometeu_node_ram_percent{{node="{NODE_ID}"}} {d["ram_percent"]}',
        f'prometeu_node_rx_mbps{{node="{NODE_ID}"}} {d["rx_mbps"]}',
        f'prometeu_node_tx_mbps{{node="{NODE_ID}"}} {d["tx_mbps"]}',
        f'prometeu_node_process_alive{{node="{NODE_ID}"}} {1 if d["process_alive"] else 0}',
        f'prometeu_node_tcp_established{{node="{NODE_ID}"}} {d["tcp"]["established"]}',
    ]
    return "\n".join(lines) + "\n"
