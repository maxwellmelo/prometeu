#!/usr/bin/env bash
# uninstall.sh — cleanly remove a Prometeu public node from this machine.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxwellmelo/prometeu/main/node/uninstall.sh | sudo bash
#   # or from a checkout:
#   sudo bash node/uninstall.sh [--purge] [--yes]
#
# Flags:
#   --purge   also delete downloaded GGUF models (/var/lib/prometeu-node).
#             Default KEEPS them so a reinstall doesn't re-download GBs.
#   --yes     skip the confirmation prompt (for automation).
#
# What it does (in order):
#   1. tells the coordinator this node is leaving (POST /api/registry/leave)
#   2. stops any sandboxed llama-server children (prometeu-inf-* scopes)
#   3. stops + disables the prometeu-node service
#   4. removes app dir, systemd unit + drop-ins, helper binary
#   5. removes the sandbox user
#   6. optionally removes models + the llama.cpp binaries
#
# It does NOT touch the llama-server binaries by default (other tools may use
# them); pass --purge to remove those too.

set -uo pipefail   # not -e: we want best-effort cleanup, reporting each step

PREFIX="/opt/prometeu-node"
CONFIG_DIR="/etc/prometeu-node"
DATA_DIR="/var/lib/prometeu-node"
SANDBOX_USER="prometeu-inf"
UNIT="prometeu-node.service"
APPLY_LIMITS_BIN="/usr/local/bin/prometeu-node-apply-limits"
LLAMA_BIN="/usr/local/bin/llama-server"
LLAMA_RPC_BIN="/usr/local/bin/rpc-server"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 1 ;;
    esac
done

log() { echo -e "\033[1;36m[prometeu]\033[0m $*"; }
ok()  { echo -e "\033[1;32m[prometeu]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[prometeu]\033[0m $*"; }
err() { echo -e "\033[1;31m[prometeu]\033[0m $*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then
    err "must run as root (use sudo)"; exit 1
fi

# ---------------------------------------------------------------------------
# Confirm (destructive)
# ---------------------------------------------------------------------------
echo
warn "This will remove the Prometeu node from this machine:"
echo "    service:   $UNIT (stop + disable)"
echo "    app dir:   $PREFIX"
echo "    config:    $CONFIG_DIR"
echo "    helper:    $APPLY_LIMITS_BIN"
echo "    user:      $SANDBOX_USER"
if [[ "$PURGE" -eq 1 ]]; then
    echo "    models:    $DATA_DIR  (PURGE — deleted)"
    echo "    binaries:  $LLAMA_BIN, $LLAMA_RPC_BIN  (PURGE — deleted)"
else
    echo "    models:    $DATA_DIR  (KEPT — pass --purge to delete)"
    echo "    binaries:  llama-server/rpc-server  (KEPT — pass --purge to delete)"
fi
echo
if [[ "$ASSUME_YES" -ne 1 ]]; then
    read -r -p "Proceed? [y/N] " reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) log "aborted; nothing changed."; exit 0 ;;
    esac
fi

# ---------------------------------------------------------------------------
# 1. Tell the coordinator we're leaving (best effort, 5s budget)
# ---------------------------------------------------------------------------
if [[ -f "$CONFIG_DIR/config.json" ]]; then
    NODE_ID="$(python3 - "$CONFIG_DIR/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    c = json.load(open(sys.argv[1]))
    print(c.get("node_id", ""))
except Exception:
    pass
PY
)"
    COORD="$(python3 - "$CONFIG_DIR/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    c = json.load(open(sys.argv[1]))
    print((c.get("coordinator_url") or "https://prometeu.mx3dev.com").rstrip("/"))
except Exception:
    print("https://prometeu.mx3dev.com")
PY
)"
    if [[ -n "${NODE_ID:-}" ]]; then
        log "deregistering node '$NODE_ID' from coordinator..."
        if curl -fsS --max-time 5 -X POST "$COORD/api/registry/leave" \
            -H 'Content-Type: application/json' \
            -d "{\"node_id\":\"$NODE_ID\"}" >/dev/null 2>&1; then
            ok "coordinator acknowledged leave"
        else
            warn "could not reach coordinator (it will drop us via heartbeat TTL ~120s anyway)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 2. Stop sandboxed llama-server children
# ---------------------------------------------------------------------------
mapfile -t SCOPES < <(systemctl list-units 'prometeu-inf-*' --all --no-legend 2>/dev/null | awk '{print $1}')
if [[ "${#SCOPES[@]}" -gt 0 ]]; then
    log "stopping ${#SCOPES[@]} inference child unit(s)..."
    for s in "${SCOPES[@]}"; do
        [[ -z "$s" ]] && continue
        systemctl stop "$s" 2>/dev/null && ok "stopped $s" || warn "could not stop $s"
    done
fi
# belt-and-suspenders: kill any stray llama-server owned by the sandbox user
if id "$SANDBOX_USER" >/dev/null 2>&1; then
    pkill -u "$SANDBOX_USER" -f llama-server 2>/dev/null && log "killed stray sandbox llama-server" || true
fi

# ---------------------------------------------------------------------------
# 3. Stop + disable the node service
# ---------------------------------------------------------------------------
if systemctl list-unit-files "$UNIT" >/dev/null 2>&1; then
    log "stopping + disabling $UNIT..."
    systemctl disable --now "$UNIT" 2>/dev/null || warn "service was not active"
fi

# ---------------------------------------------------------------------------
# 4. Remove unit + drop-ins, app dir, config, helper binary
# ---------------------------------------------------------------------------
rm -f  "/etc/systemd/system/$UNIT"
rm -rf "/etc/systemd/system/${UNIT}.d"        # resource-limit drop-ins
systemctl daemon-reload
systemctl reset-failed "$UNIT" 2>/dev/null || true
rm -rf "$PREFIX" "$CONFIG_DIR"
rm -f  "$APPLY_LIMITS_BIN"
ok "removed service, app dir, config, helper"

# ---------------------------------------------------------------------------
# 5. Remove sandbox user
# ---------------------------------------------------------------------------
if id "$SANDBOX_USER" >/dev/null 2>&1; then
    userdel "$SANDBOX_USER" 2>/dev/null && ok "removed user $SANDBOX_USER" \
        || warn "could not remove user $SANDBOX_USER (maybe owns running procs)"
fi

# ---------------------------------------------------------------------------
# 6. Purge models + binaries (optional)
# ---------------------------------------------------------------------------
if [[ "$PURGE" -eq 1 ]]; then
    rm -rf "$DATA_DIR"
    rm -f  "$LLAMA_BIN" "$LLAMA_RPC_BIN"
    rm -rf /opt/llama.cpp
    ok "purged models ($DATA_DIR), llama binaries + shared libs (/opt/llama.cpp)"
else
    if [[ -d "$DATA_DIR" ]]; then
        warn "kept models at $DATA_DIR ($(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)) — 'sudo rm -rf $DATA_DIR' to reclaim"
    fi
fi

echo
ok "Prometeu node uninstalled."
