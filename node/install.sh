#!/usr/bin/env bash
# install.sh — install Prometeu public node daemon.
#
# Usage:
#   sudo bash node/install.sh [coordinator_url]
#
# This Sprint 2A daemon only registers capacity and exposes a local dashboard.
# It does NOT yet serve public inference traffic.

set -euo pipefail

COORDINATOR_URL="${1:-https://prometeu.mx3dev.com}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="/opt/prometeu-node"
CONFIG_DIR="/etc/prometeu-node"

apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip

mkdir -p "$PREFIX/prometeu_node" "$PREFIX/web" "$PREFIX/scripts" "$CONFIG_DIR"
install -m 0644 "$REPO_DIR/node/prometeu_node/main.py" "$PREFIX/prometeu_node/main.py"
install -m 0644 "$REPO_DIR/node/prometeu_node/__init__.py" "$PREFIX/prometeu_node/__init__.py"
install -m 0644 "$REPO_DIR/node/requirements.txt" "$PREFIX/requirements.txt"
install -m 0755 "$REPO_DIR/node/scripts/prometeu-node-apply-limits" /usr/local/bin/prometeu-node-apply-limits
cp -r "$REPO_DIR/node/web/." "$PREFIX/web/"

if [[ ! -d "$PREFIX/venv" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
    NODE_ID="pn-$(cat /etc/machine-id 2>/dev/null | cut -c1-12 || hostname)"
    cat > "$CONFIG_DIR/config.json" <<EOF
{
  "node_id": "$NODE_ID",
  "display_name": "$(hostname)",
  "mode": "public",
  "coordinator_url": "$COORDINATOR_URL",
  "models": ["qwen2.5-1.5b-q4"],
  "active_model": "qwen2.5-1.5b-q4",
  "limits": {"cpu_percent": 50, "ram_mb": 1024, "bandwidth_mbps": 20},
  "schedule": {"enabled": false, "start": "00:00", "end": "06:00"},
  "status": "available",
  "rpc_endpoint": null,
  "dashboard_url": null,
  "heartbeat_sec": 15
}
EOF
fi

install -m 0644 "$REPO_DIR/node/systemd/prometeu-node.service" /etc/systemd/system/prometeu-node.service
systemctl daemon-reload
systemctl enable --now prometeu-node
/usr/local/bin/prometeu-node-apply-limits
sleep 2
systemctl status prometeu-node --no-pager -n 10

echo
echo "Prometeu node installed."
echo "Dashboard: http://localhost:8787"
echo "Config:    $CONFIG_DIR/config.json"
