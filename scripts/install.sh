#!/usr/bin/env bash
# install.sh — instala Prometeu em uma máquina master (Debian/Ubuntu)
#
# Assume:
#   - llama.cpp já compilado (use scripts/build-llama-cpp.sh)
#   - rpc-server.service já configurado nos workers
#   - Modelo GGUF copiado pra /opt/models/<modelo>.gguf
#
# Uso:  bash install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="/opt/prometeu"

apt-get update
apt-get install -y --no-install-recommends python3-venv python3-pip

mkdir -p "$PREFIX/web" /etc/prometeu

# Código
install -m 0644 "$REPO_DIR/gateway/app.py" "$PREFIX/app.py"
mkdir -p "$PREFIX/gateway"
install -m 0644 "$REPO_DIR/gateway/app.py" "$PREFIX/gateway/app.py"
touch "$PREFIX/gateway/__init__.py"
install -m 0644 "$REPO_DIR/gateway/requirements.txt" "$PREFIX/requirements.txt"

# Config (se não existir)
if [[ ! -f "$PREFIX/config.json" ]]; then
    install -m 0644 "$REPO_DIR/gateway/config.example.json" "$PREFIX/config.json"
    echo "Config criada em $PREFIX/config.json — edite com os IPs reais dos nós."
fi

# Frontend
cp -r "$REPO_DIR/web/." "$PREFIX/web/"

# Venv + deps
if [[ ! -d "$PREFIX/venv" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

# Systemd
install -m 0644 "$REPO_DIR/scripts/systemd/prometeu-gateway.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/scripts/systemd/llama-server.service"    /etc/systemd/system/
systemctl daemon-reload

echo
echo "Instalado. Próximos passos:"
echo "  1. Edite /opt/prometeu/config.json com os IPs dos seus nós"
echo "  2. Edite /etc/systemd/system/llama-server.service apontando pro seu modelo .gguf"
echo "  3. systemctl enable --now llama-server prometeu-gateway"
echo "  4. curl http://localhost:3000/api/health"
