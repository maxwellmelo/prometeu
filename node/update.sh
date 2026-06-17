#!/usr/bin/env bash
# update.sh — update an installed Prometeu public node to the latest code.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxwellmelo/prometeu/main/node/update.sh | sudo bash
#   # or from a checkout:
#   sudo bash node/update.sh
#
# What it does:
#   - pulls the latest node package (main.py, inference.py, web dashboard)
#   - refreshes the llama-server / rpc-server binaries
#   - upgrades Python deps inside the existing venv
#   - re-applies resource limits and RESTARTS the daemon (so new code runs)
#   - PRESERVES your config (/etc/prometeu-node/config.json) and downloaded models
#
# No fallbacks: if a required source file or the venv is missing it stops and
# tells you to run install.sh, instead of half-updating.

set -euo pipefail

PREFIX="/opt/prometeu-node"
CONFIG_DIR="/etc/prometeu-node"
LLAMA_BIN="/usr/local/bin/llama-server"
LLAMA_RPC_BIN="/usr/local/bin/rpc-server"
RELEASE_BASE="${PROMETEU_RELEASE_BASE:-https://github.com/maxwellmelo/prometeu/releases/latest/download}"

log() { echo -e "\033[1;36m[prometeu]\033[0m $*"; }
err() { echo -e "\033[1;31m[prometeu]\033[0m $*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then
    err "must run as root (use sudo)"; exit 1
fi

# ---------------------------------------------------------------------------
# 0. Sanity: node must already be installed
# ---------------------------------------------------------------------------
if [[ ! -d "$PREFIX" || ! -f "$CONFIG_DIR/config.json" ]]; then
    err "no existing install found at $PREFIX / $CONFIG_DIR."
    err "this is the UPDATE script — run install.sh first."
    exit 1
fi
if [[ ! -d "$PREFIX/venv" ]]; then
    err "venv missing at $PREFIX/venv — install looks broken; re-run install.sh."
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Get latest source (checkout if piped)
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo /nonexistent)"
CLONED=""
if [[ ! -f "$REPO_DIR/node/prometeu_node/main.py" ]]; then
    log "no local checkout; fetching latest from GitHub..."
    REPO_DIR="$(mktemp -d)/prometeu"
    git clone --depth 1 https://github.com/maxwellmelo/prometeu.git "$REPO_DIR"
    CLONED="$REPO_DIR"
fi

for f in node/prometeu_node/main.py node/prometeu_node/inference.py node/prometeu_node/__init__.py node/requirements.txt; do
    if [[ ! -f "$REPO_DIR/$f" ]]; then
        err "source file missing in repo: $f — aborting (no half-update)."
        [[ -n "$CLONED" ]] && rm -rf "$(dirname "$CLONED")"
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 2. Record current version for the user
# ---------------------------------------------------------------------------
OLD_REV="$(cat "$PREFIX/.git-rev" 2>/dev/null || echo unknown)"
NEW_REV="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
log "updating node code: $OLD_REV -> $NEW_REV"

# ---------------------------------------------------------------------------
# 3. Replace app code + dashboard (config + models untouched)
# ---------------------------------------------------------------------------
install -m 0644 "$REPO_DIR/node/prometeu_node/main.py"      "$PREFIX/prometeu_node/main.py"
install -m 0644 "$REPO_DIR/node/prometeu_node/inference.py" "$PREFIX/prometeu_node/inference.py"
install -m 0644 "$REPO_DIR/node/prometeu_node/__init__.py"  "$PREFIX/prometeu_node/__init__.py"
install -m 0644 "$REPO_DIR/node/requirements.txt"           "$PREFIX/requirements.txt"
install -m 0755 "$REPO_DIR/node/scripts/prometeu-node-apply-limits" /usr/local/bin/prometeu-node-apply-limits
cp -r "$REPO_DIR/node/web/." "$PREFIX/web/"
echo "$NEW_REV" > "$PREFIX/.git-rev"

# ---------------------------------------------------------------------------
# 4. Refresh llama binaries (force re-download — install.sh skips if present)
# ---------------------------------------------------------------------------
detect_target() {
    local arch flags
    arch="$(uname -m)"
    if [[ "$arch" == "aarch64" || "$arch" == "arm64" ]]; then echo "linux-aarch64"; return; fi
    if command -v nvidia-smi >/dev/null 2>&1; then echo "linux-x86_64-cuda12"; return; fi
    flags="$(grep -m1 '^flags' /proc/cpuinfo || true)"
    if echo "$flags" | grep -qw avx512f; then echo "linux-x86_64-avx512"
    elif echo "$flags" | grep -qw avx2; then echo "linux-x86_64-avx2"
    else echo "linux-x86_64-sandybridge"; fi
}
if [[ "${PROMETEU_SKIP_BINARIES:-0}" != "1" ]]; then
    target="$(detect_target)"
    tarball="prometeu-llama-${target}.tar.gz"
    url="${RELEASE_BASE}/${tarball}"
    tmp="$(mktemp -d)"
    log "refreshing llama binaries ($target)..."
    if curl -fSL "$url" -o "$tmp/$tarball"; then
        tar -xzf "$tmp/$tarball" -C "$tmp"
        payload="$(find "$tmp" -maxdepth 1 -type d -name 'prometeu-llama-*' | head -1)"
        [[ -z "$payload" ]] && payload="$tmp"
        if [[ -d "$payload/bin" ]] && ls "$payload/bin"/*.so* >/dev/null 2>&1; then
            mkdir -p /opt/llama.cpp/build/bin
            cp -a "$payload/bin"/*.so* /opt/llama.cpp/build/bin/
            install -m 0755 "$payload/bin/llama-server" /opt/llama.cpp/build/bin/llama-server
            install -m 0755 "$payload/bin/rpc-server"   /opt/llama.cpp/build/bin/rpc-server
            ln -sf /opt/llama.cpp/build/bin/llama-server "$LLAMA_BIN"
            ln -sf /opt/llama.cpp/build/bin/rpc-server   "$LLAMA_RPC_BIN"
        else
            install -m 0755 "$payload"/llama-server "$LLAMA_BIN" 2>/dev/null || true
            install -m 0755 "$payload"/rpc-server "$LLAMA_RPC_BIN" 2>/dev/null || true
        fi
        if "$LLAMA_BIN" --version >/dev/null 2>&1; then
            log "llama binaries refreshed + verified"
        else
            err "refreshed binaries fail to run (missing shared libs) — keeping was impossible; report incomplete tarball."
        fi
    else
        err "could not refresh binaries for $target (keeping existing ones)."
        err "set PROMETEU_SKIP_BINARIES=1 to silence, or build manually (see node/README)."
    fi
    rm -rf "$tmp"
fi

# ---------------------------------------------------------------------------
# 5. Upgrade Python deps
# ---------------------------------------------------------------------------
log "upgrading Python deps..."
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade -r "$PREFIX/requirements.txt"

# ---------------------------------------------------------------------------
# 6. Re-apply limits + RESTART (so new code is actually running)
# ---------------------------------------------------------------------------
systemctl daemon-reload
/usr/local/bin/prometeu-node-apply-limits || err "limit re-apply failed (non-fatal)"
log "restarting prometeu-node..."
systemctl restart prometeu-node
sleep 2

[[ -n "$CLONED" ]] && rm -rf "$(dirname "$CLONED")"

# ---------------------------------------------------------------------------
# 7. Verify
# ---------------------------------------------------------------------------
echo
if systemctl is-active --quiet prometeu-node; then
    log "update complete — daemon is active (rev $NEW_REV)."
else
    err "daemon is NOT active after restart. Check: journalctl -u prometeu-node -n 50"
    exit 1
fi
log "Preflight (can_serve must be true to serve inference):"
curl -fsS http://localhost:8787/api/node/preflight 2>/dev/null | python3 -m json.tool \
    || echo "(daemon still starting; retry 'curl localhost:8787/api/node/preflight' shortly)"
