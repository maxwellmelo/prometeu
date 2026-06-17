#!/usr/bin/env bash
# install.sh — install Prometeu public node daemon (inference-capable, v0.6.0).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxwellmelo/prometeu/main/node/install.sh | sudo bash -s -- [coordinator_url]
#   # or from a checkout:
#   sudo bash node/install.sh [coordinator_url]
#
# This node:
#   - registers capacity + telemetry with the coordinator (heartbeats)
#   - exposes a local dashboard on :8787
#   - on request from the coordinator, downloads a GGUF, verifies its sha256,
#     and serves it via a SANDBOXED llama-server (systemd-run cgroup limits)
#
# No fallbacks: if sandbox prerequisites are missing the node refuses to serve
# inference and reports the blocker via /api/node/preflight.

set -euo pipefail

COORDINATOR_URL="${1:-https://prometeu.mx3dev.com}"
PREFIX="/opt/prometeu-node"
CONFIG_DIR="/etc/prometeu-node"
MODELS_DIR="/var/lib/prometeu-node/models"
SANDBOX_USER="prometeu-inf"
LLAMA_BIN="/usr/local/bin/llama-server"
LLAMA_RPC_BIN="/usr/local/bin/rpc-server"
RELEASE_BASE="${PROMETEU_RELEASE_BASE:-https://github.com/maxwellmelo/prometeu/releases/latest/download}"

log() { echo -e "\033[1;36m[prometeu]\033[0m $*"; }
err() { echo -e "\033[1;31m[prometeu]\033[0m $*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then
    err "must run as root (use sudo)"; exit 1
fi

# ---------------------------------------------------------------------------
# 1. Base deps
# ---------------------------------------------------------------------------
log "installing base packages..."
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3-venv python3-pip curl ca-certificates git
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip curl ca-certificates git
else
    err "unsupported distro (need apt or dnf)"; exit 1
fi

# ---------------------------------------------------------------------------
# 2. Detect CPU/GPU target and pick the llama.cpp binary bundle
# ---------------------------------------------------------------------------
detect_target() {
    local arch flags
    arch="$(uname -m)"
    if [[ "$arch" == "aarch64" || "$arch" == "arm64" ]]; then
        echo "linux-aarch64"; return
    fi
    if command -v nvidia-smi >/dev/null 2>&1; then
        echo "linux-x86_64-cuda12"; return
    fi
    flags="$(grep -m1 '^flags' /proc/cpuinfo || true)"
    if echo "$flags" | grep -qw avx512f; then
        echo "linux-x86_64-avx512"
    elif echo "$flags" | grep -qw avx2; then
        echo "linux-x86_64-avx2"
    else
        echo "linux-x86_64-sandybridge"
    fi
}

install_llama_binaries() {
    if [[ -x "$LLAMA_BIN" && -x "$LLAMA_RPC_BIN" ]]; then
        log "llama binaries already present, skipping download"
        return
    fi
    local target tarball url tmp
    target="$(detect_target)"
    log "detected build target: $target"
    tarball="prometeu-llama-${target}.tar.gz"
    url="${RELEASE_BASE}/${tarball}"
    tmp="$(mktemp -d)"
    log "downloading $url ..."
    if ! curl -fSL "$url" -o "$tmp/$tarball"; then
        err "could not download prebuilt binary for $target."
        err "build llama.cpp manually (see node/README) and place llama-server + rpc-server in /usr/local/bin, then re-run."
        rm -rf "$tmp"
        return 1
    fi
    tar -xzf "$tmp/$tarball" -C "$tmp"
    local payload
    payload="$(find "$tmp" -maxdepth 1 -type d -name 'prometeu-llama-*' | head -1)"
    [[ -z "$payload" ]] && payload="$tmp"
    # The llama-server/rpc-server are thin stubs with RUNPATH=/opt/llama.cpp/build/bin;
    # they dlopen libllama-server-impl.so etc from there. Install the shared libs to
    # that exact path (preserving symlinks) so the stubs resolve them, then symlink
    # the binaries into /usr/local/bin. No patchelf dependency, portable anywhere.
    if [[ -d "$payload/bin" ]] && ls "$payload/bin"/*.so* >/dev/null 2>&1; then
        mkdir -p /opt/llama.cpp/build/bin
        cp -a "$payload/bin"/*.so* /opt/llama.cpp/build/bin/
        install -m 0755 "$payload/bin/llama-server" /opt/llama.cpp/build/bin/llama-server
        install -m 0755 "$payload/bin/rpc-server"   /opt/llama.cpp/build/bin/rpc-server
        ln -sf /opt/llama.cpp/build/bin/llama-server "$LLAMA_BIN"
        ln -sf /opt/llama.cpp/build/bin/rpc-server   "$LLAMA_RPC_BIN"
    else
        # Legacy tarball without shared libs — copy binaries directly (may fail at
        # runtime if they need libllama-server-impl.so; that is a packaging bug).
        install -m 0755 "$payload"/llama-server "$LLAMA_BIN"
        install -m 0755 "$payload"/rpc-server "$LLAMA_RPC_BIN"
    fi
    rm -rf "$tmp"
    # Verify the binary actually runs (catches missing shared libs — the #1 packaging bug)
    if ! "$LLAMA_BIN" --version >/dev/null 2>&1; then
        err "llama-server installed but fails to run (missing shared libraries)."
        err "the release tarball for $target is incomplete; report this. Node cannot serve."
        return 1
    fi
    log "installed + verified llama-server + rpc-server ($target)"
}

# ---------------------------------------------------------------------------
# 3. Sandbox user + models dir
# ---------------------------------------------------------------------------
log "setting up sandbox user '$SANDBOX_USER'..."
if ! id "$SANDBOX_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SANDBOX_USER"
fi
mkdir -p "$MODELS_DIR"
chown -R "$SANDBOX_USER:$SANDBOX_USER" /var/lib/prometeu-node

# ---------------------------------------------------------------------------
# 4. Python app
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo /tmp/prometeu-checkout)"
# If piped (no checkout), fetch the node package from the repo.
if [[ ! -f "$REPO_DIR/node/prometeu_node/main.py" ]]; then
    log "no local checkout; fetching node package from GitHub..."
    REPO_DIR="$(mktemp -d)/prometeu"
    git clone --depth 1 https://github.com/maxwellmelo/prometeu.git "$REPO_DIR"
fi

mkdir -p "$PREFIX/prometeu_node" "$PREFIX/web" "$PREFIX/scripts" "$CONFIG_DIR"
install -m 0644 "$REPO_DIR/node/prometeu_node/main.py" "$PREFIX/prometeu_node/main.py"
install -m 0644 "$REPO_DIR/node/prometeu_node/inference.py" "$PREFIX/prometeu_node/inference.py"
install -m 0644 "$REPO_DIR/node/prometeu_node/__init__.py" "$PREFIX/prometeu_node/__init__.py"
install -m 0644 "$REPO_DIR/node/requirements.txt" "$PREFIX/requirements.txt"
install -m 0755 "$REPO_DIR/node/scripts/prometeu-node-apply-limits" /usr/local/bin/prometeu-node-apply-limits
cp -r "$REPO_DIR/node/web/." "$PREFIX/web/"
git -C "$REPO_DIR" rev-parse --short HEAD > "$PREFIX/.git-rev" 2>/dev/null || echo unknown > "$PREFIX/.git-rev"

if [[ ! -d "$PREFIX/venv" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

# ---------------------------------------------------------------------------
# 5. Sandbox sudoers — daemon (root) uses systemd-run directly; no extra grant
#    needed because the daemon runs as root. Document the boundary.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. Config
# ---------------------------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    NODE_ID="pn-$(cat /etc/machine-id 2>/dev/null | cut -c1-12 || hostname)"
    cat > "$CONFIG_DIR/config.json" <<EOF
{
  "node_id": "$NODE_ID",
  "display_name": "$(hostname)",
  "mode": "public",
  "coordinator_url": "$COORDINATOR_URL",
  "models": [],
  "active_model": null,
  "limits": {"cpu_percent": 50, "ram_mb": 2048, "bandwidth_mbps": 20},
  "schedule": {"enabled": false, "start": "00:00", "end": "06:00"},
  "status": "available",
  "rpc_endpoint": null,
  "dashboard_url": null,
  "heartbeat_sec": 15
}
EOF
fi

# ---------------------------------------------------------------------------
# 7. llama binaries (hard requirement for serving; no fallback)
# ---------------------------------------------------------------------------
install_llama_binaries

# ---------------------------------------------------------------------------
# 8. systemd unit
# ---------------------------------------------------------------------------
install -m 0644 "$REPO_DIR/node/systemd/prometeu-node.service" /etc/systemd/system/prometeu-node.service
systemctl daemon-reload
systemctl enable --now prometeu-node
# If it was already running (re-run / in-place upgrade), enable --now is a no-op
# and would leave stale code in memory — force a restart so new code runs.
systemctl restart prometeu-node
/usr/local/bin/prometeu-node-apply-limits
sleep 2
systemctl status prometeu-node --no-pager -n 10 || true

echo
log "Prometeu node installed (v0.6.0, inference-capable)."
echo "Dashboard:  http://localhost:8787"
echo "Config:     $CONFIG_DIR/config.json"
echo "Models dir: $MODELS_DIR"
echo
log "Preflight check (must be true before serving):"
curl -fsS http://localhost:8787/api/node/preflight 2>/dev/null | python3 -m json.tool || echo "(daemon still starting; check 'curl localhost:8787/api/node/preflight' in a few seconds)"
log "Resource limit check (resource_limits.applied must be true):"
curl -fsS http://localhost:8787/api/status 2>/dev/null | python3 - <<'PY' || echo "(daemon still starting; check 'curl localhost:8787/api/status' in a few seconds)"
import json, sys
try:
    data = json.load(sys.stdin)
    print(json.dumps({"resource_limits.applied": data.get("resource_limits", {}).get("applied")}, indent=2))
except Exception as e:
    raise SystemExit(str(e))
PY
