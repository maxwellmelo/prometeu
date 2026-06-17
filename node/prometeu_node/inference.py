"""
Prometeu node — inference lifecycle.

Manages a sandboxed llama-server child process per model_id. Downloads GGUF
from HF resolve URLs (or other allowlisted catalog sources), verifies SHA256,
spawns llama-server with CPU/RAM cgroup limits via systemd-run, exposes the
HTTP endpoint via the heartbeat so the gateway can route to it.

Design constraints (per Maxwell, Fase 1 do roadmap):
- No fallbacks. If sandbox cannot be applied, refuse load and report blocker.
- No silent failures. Every failure mode is surfaced in /api/node/status.
- Catalog allowlist only (defense in depth — pitfall against poisoned models).
  This module trusts the caller (the gateway) to have validated model_id against
  the curated catalog. We additionally verify sha256 here.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock
from typing import Any

MODELS_DIR = Path(os.getenv("PROMETEU_NODE_MODELS_DIR", "/var/lib/prometeu-node/models"))
STATE_FILE = Path(os.getenv("PROMETEU_NODE_INFERENCE_STATE", "/var/lib/prometeu-node/inference.json"))
LLAMA_SERVER_BIN = os.getenv("PROMETEU_LLAMA_SERVER_BIN", "/usr/local/bin/llama-server")
SANDBOX_USER = os.getenv("PROMETEU_NODE_SANDBOX_USER", "prometeu-inf")
PORT_RANGE = (
    int(os.getenv("PROMETEU_INF_PORT_MIN", "18080")),
    int(os.getenv("PROMETEU_INF_PORT_MAX", "18099")),
)

_lock = Lock()


@dataclass
class LoadedModel:
    model_id: str
    gguf_path: str
    sha256: str
    port: int
    pid: int | None = None
    scope_name: str | None = None
    started_at: float = field(default_factory=time.time)
    ctx_size: int = 2048
    rpc_peers: list[str] = field(default_factory=list)
    ready: bool = False
    last_check: float = 0.0
    error: str | None = None


def _ensure_dirs() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict[str, LoadedModel]:
    _ensure_dirs()
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except Exception:
        return {}
    out: dict[str, LoadedModel] = {}
    for mid, data in raw.items():
        try:
            out[mid] = LoadedModel(**data)
        except Exception:
            continue
    return out


def _save_state(state: dict[str, LoadedModel]) -> None:
    _ensure_dirs()
    serial = {mid: asdict(m) for mid, m in state.items()}
    STATE_FILE.write_text(json.dumps(serial, indent=2) + "\n")


def _pick_port(state: dict[str, LoadedModel]) -> int:
    used = {m.port for m in state.values()}
    for p in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        if p in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in {PORT_RANGE}")


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download_gguf(url: str, sha256: str, dest_dir: Path = MODELS_DIR) -> Path:
    """Download GGUF with resume + sha256 verification.

    Raises RuntimeError on hash mismatch (file is removed). No silent fallback.
    """
    _ensure_dirs()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    fname = Path(parsed.path).name or f"{sha256[:16]}.gguf"
    if not fname.endswith(".gguf"):
        fname = f"{sha256[:16]}.gguf"
    dest = dest_dir / f"{sha256}.gguf"
    if dest.exists():
        actual = _sha256_file(dest)
        if actual == sha256:
            return dest
        dest.unlink()

    tmp = dest.with_suffix(".gguf.part")
    start = tmp.stat().st_size if tmp.exists() else 0
    headers = {}
    if start > 0:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("ab") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.rename(dest)
    actual = _sha256_file(dest)
    if actual != sha256:
        dest.unlink()
        raise RuntimeError(f"sha256 mismatch: expected {sha256}, got {actual}")
    return dest


def _systemd_available() -> bool:
    return bool(shutil.which("systemd-run")) and Path("/run/systemd/system").exists()


def _sandbox_user_exists() -> bool:
    try:
        import pwd
        pwd.getpwnam(SANDBOX_USER)
        return True
    except (KeyError, ImportError):
        return False


def _spawn_llama_server(model: LoadedModel, cpu_quota: int, mem_mb: int) -> LoadedModel:
    if not Path(LLAMA_SERVER_BIN).exists():
        raise RuntimeError(f"llama-server binary missing at {LLAMA_SERVER_BIN}")
    if not _systemd_available():
        raise RuntimeError("systemd-run not available; sandbox required, refusing to spawn")
    if not _sandbox_user_exists():
        raise RuntimeError(
            f"sandbox user {SANDBOX_USER!r} missing; create it via install script before loading models"
        )

    scope = f"prometeu-inf-{model.model_id.replace('/', '_').replace(':', '_')}"
    cmd = [
        "systemd-run",
        "--unit", scope,
        "--scope" if os.getenv("PROMETEU_USE_SCOPE", "0") == "1" else "--service-type=exec",
        "--collect",
        "--quiet",
        f"--uid={SANDBOX_USER}",
        f"--gid={SANDBOX_USER}",
        f"-p", f"CPUQuota={cpu_quota}%",
        f"-p", f"MemoryMax={mem_mb}M",
        "-p", "ProtectSystem=strict",
        "-p", "ProtectHome=true",
        "-p", "PrivateTmp=true",
        "-p", "NoNewPrivileges=true",
        "-p", f"ReadWritePaths={MODELS_DIR}",
        "--",
        LLAMA_SERVER_BIN,
        "-m", model.gguf_path,
        "--host", "0.0.0.0",
        "--port", str(model.port),
        "-c", str(model.ctx_size),
    ]
    for peer in model.rpc_peers:
        cmd.extend(["--rpc", peer])

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"systemd-run failed: {e.stderr.strip() or e.stdout.strip() or str(e)}")
    model.scope_name = scope
    # Resolve PID via systemctl show
    try:
        out = subprocess.check_output(
            ["systemctl", "show", scope + ".service", "-p", "MainPID"],
            text=True, timeout=3,
        ).strip()
        if "=" in out:
            pid = int(out.split("=", 1)[1])
            model.pid = pid if pid > 0 else None
    except Exception:
        pass
    return model


def _stop_scope(scope: str) -> None:
    subprocess.run(["systemctl", "stop", scope + ".service"], capture_output=True, timeout=10)


def health_check(port: int, timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
            return body.get("status") in ("ok", "loading-ready", "ready")
    except Exception:
        return False


def list_models() -> dict[str, dict[str, Any]]:
    with _lock:
        state = _load_state()
        out = {}
        for mid, m in state.items():
            m.ready = health_check(m.port) if m.pid else False
            m.last_check = time.time()
            out[mid] = asdict(m)
        _save_state(state)
        return out


def load_model(
    model_id: str,
    gguf_url: str,
    sha256: str,
    ctx_size: int = 2048,
    rpc_peers: list[str] | None = None,
    cpu_quota: int = 50,
    mem_mb: int = 1024,
) -> dict[str, Any]:
    with _lock:
        state = _load_state()
        if model_id in state:
            existing = state[model_id]
            if health_check(existing.port):
                return {"ok": True, "already_loaded": True, "model": asdict(existing)}
            # Stale entry; clean it up.
            if existing.scope_name:
                _stop_scope(existing.scope_name)
            del state[model_id]

        gguf_path = download_gguf(gguf_url, sha256)
        port = _pick_port(state)
        model = LoadedModel(
            model_id=model_id,
            gguf_path=str(gguf_path),
            sha256=sha256,
            port=port,
            ctx_size=ctx_size,
            rpc_peers=rpc_peers or [],
        )
        model = _spawn_llama_server(model, cpu_quota=cpu_quota, mem_mb=mem_mb)

        # Wait up to 90s for /health to go green (cold start can be slow).
        deadline = time.time() + 90
        while time.time() < deadline:
            if health_check(model.port):
                model.ready = True
                break
            time.sleep(2)
        if not model.ready:
            if model.scope_name:
                _stop_scope(model.scope_name)
            raise RuntimeError("llama-server failed to become healthy within 90s")

        state[model_id] = model
        _save_state(state)
        return {"ok": True, "model": asdict(model)}


def unload_model(model_id: str) -> dict[str, Any]:
    with _lock:
        state = _load_state()
        if model_id not in state:
            return {"ok": False, "err": "not loaded"}
        model = state[model_id]
        if model.scope_name:
            _stop_scope(model.scope_name)
        del state[model_id]
        _save_state(state)
        return {"ok": True, "stopped": asdict(model)}


def sandbox_preflight() -> dict[str, Any]:
    """Reports whether the host can run inference workloads safely."""
    _ensure_dirs()
    llama_server_bin = Path(LLAMA_SERVER_BIN).exists()
    systemd_run = _systemd_available()
    sandbox_user = _sandbox_user_exists()
    models_dir_writable = os.access(MODELS_DIR, os.W_OK)
    # No-fallback rule: every mandatory prerequisite must hold or the node
    # refuses to serve. can_serve is the single canonical readiness signal
    # consumed by the gateway and the public install validator.
    can_serve = bool(
        llama_server_bin and systemd_run and sandbox_user and models_dir_writable
    )
    return {
        "llama_server_bin": llama_server_bin,
        "systemd_run": systemd_run,
        "sandbox_user": sandbox_user,
        "models_dir": str(MODELS_DIR),
        "models_dir_writable": models_dir_writable,
        "port_range": list(PORT_RANGE),
        "can_serve": can_serve,
    }
