#!/usr/bin/env bash
# install-node-agent.sh — installs Prometeu Node Agent telemetry service.
#
# Env vars for configuration:
#   NODE_ID, NODE_ROLE, NODE_MODEL, NODE_LAYERS, RPC_PORT, PROCESS_NAME

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="/opt/prometeu-node-agent"

apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip

mkdir -p "$PREFIX" /etc/prometeu
install -m 0644 "$REPO_DIR/node-agent/app.py" "$PREFIX/app.py"
install -m 0644 "$REPO_DIR/node-agent/requirements.txt" "$PREFIX/requirements.txt"

if [[ ! -d "$PREFIX/venv" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

cat > /etc/prometeu/node-agent.env <<EOF
PROMETEU_NODE_ID=${NODE_ID:-$(hostname)}
PROMETEU_NODE_ROLE=${NODE_ROLE:-worker}
PROMETEU_NODE_MODEL=${NODE_MODEL:-unknown}
PROMETEU_NODE_LAYERS=${NODE_LAYERS:-unknown}
PROMETEU_RPC_PORT=${RPC_PORT:-50052}
PROMETEU_PROCESS_NAME=${PROCESS_NAME:-rpc-server}
PROMETEU_SAMPLE_INTERVAL=1
EOF

install -m 0644 "$REPO_DIR/scripts/systemd/prometeu-node-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prometeu-node-agent
sleep 2
systemctl status prometeu-node-agent --no-pager -n 10
