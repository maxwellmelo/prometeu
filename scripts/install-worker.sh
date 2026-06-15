#!/usr/bin/env bash
# install-worker.sh — instala rpc-server systemd em uma máquina worker
#
# Pré-requisito: llama.cpp compilado em /opt/llama.cpp (use build-llama-cpp.sh)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

install -m 0644 "$REPO_DIR/scripts/systemd/llama-rpc.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now llama-rpc

sleep 2
systemctl status llama-rpc --no-pager -n 10
